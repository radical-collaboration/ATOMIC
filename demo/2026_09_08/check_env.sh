#!/usr/bin/env bash
#
# demo/2026_09_08/check_env.sh -- verify the demo's prerequisites.
#
# Starts nothing, changes nothing, installs nothing.  Run it before
# up.sh (Monday morning, or on a new machine) to find out whether the
# demo *can* come up:
#
#   demo/2026_09_08/check_env.sh
#
# Exit code 0 = ready, 1 = at least one FAIL.  WARNs are things the demo
# survives (a missing node for the UI tests, an unrelated broker running).

set -euo pipefail

# shellcheck source=demo/2026_09_08/env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)/env.sh"

FAILED=0
WARNED=0

# --------------------------------------------------------------------------
ok()   { printf '  ok   %s\n' "$*"; }
warn() { printf '  WARN %s\n' "$*"; WARNED=$((WARNED + 1)); }
bad()  { printf '  FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

section() { printf '\n%s\n' "$*"; }

# --------------------------------------------------------------------------
check_repos() {
    section 'repositories'

    if [ -d "$ORBIT_SRC/src/radical/orbit" ]; then
        local branch=''
        branch="$(git -C "$ORBIT_SRC" rev-parse --abbrev-ref HEAD \
                  2> /dev/null || true)"
        ok "radical.orbit at $ORBIT_SRC (branch: ${branch:-unknown})"
        [ "$branch" = 'feature/atomic-federation' ] \
            || warn "expected branch feature/atomic-federation, on ${branch:-?}"
    else
        bad "no radical.orbit source at $ORBIT_SRC (set \$ORBIT_SRC)"
    fi

    if [ -f "$ATOMIC_SRC/pyproject.toml" ]; then
        local branch=''
        branch="$(git -C "$ATOMIC_SRC" rev-parse --abbrev-ref HEAD \
                  2> /dev/null || true)"
        ok "atomic at $ATOMIC_SRC (branch: ${branch:-unknown})"
    else
        bad "no atomic checkout at $ATOMIC_SRC"
    fi

    [ -f "$ATOMIC_SRC/examples/workflow_vacancy.json" ] \
        && ok  'examples/workflow_vacancy.json present' \
        || bad 'examples/workflow_vacancy.json missing (smoke.py needs it)'
}

# --------------------------------------------------------------------------
check_venv() {
    section 'orbit venv'

    if [ -x "$VE/bin/python" ]; then
        ok "python: $("$VE/bin/python" -V 2>&1) ($VE/bin/python)"
    else
        bad "no python in $VE/bin -- create/point \$VE at the orbit venv"
        return 0
    fi

    [ -x "$VE/bin/pip" ] && ok 'pip present' || bad "no pip in $VE/bin"

    "$VE/bin/python" -c 'import requests' 2> /dev/null \
        && ok  'requests importable (the CLIs need it)' \
        || warn 'requests not importable -- up.sh installs atomic-wm[cli]'

    # the console scripts only exist after up.sh has installed atomic-wm
    local tool missing=''
    for tool in atomic-join atomic-leave atomic-resources atomic-campaign \
                atomic-fake-md atomic-fake-train; do
        [ -x "$VE/bin/$tool" ] || missing="$missing $tool"
    done

    [ -z "$missing" ] \
        && ok   'atomic console scripts installed' \
        || warn "console scripts not installed yet:$missing (up.sh does it)"

    local bin
    for bin in radical-orbit-broker.py radical-orbit-endpoint.py; do
        [ -f "$ORBIT_SRC/bin/$bin" ] \
            && ok  "$bin present" \
            || bad "$ORBIT_SRC/bin/$bin missing"
    done

    # psij launches the wrapper by name -- it must be on $PATH, which
    # env.sh arranges by prepending $VE/bin
    local wrapper=''
    wrapper="$(command -v radical-orbit-endpoint-wrapper.sh 2> /dev/null \
               || true)"
    if [ -n "$wrapper" ]; then
        ok "endpoint wrapper on \$PATH: $wrapper"
    else
        warn 'radical-orbit-endpoint-wrapper.sh not on $PATH -- up.sh'\
             'installs it into '"$VE/bin"
    fi
}

# --------------------------------------------------------------------------
check_tls() {
    section 'TLS (mandatory even for a --no-auth broker)'

    if [ -r "$RADICAL_ORBIT_BROKER_CERT" ]; then
        ok "cert: $RADICAL_ORBIT_BROKER_CERT"
    else
        bad "cert missing or unreadable: $RADICAL_ORBIT_BROKER_CERT"
    fi

    if [ -r "$ATOMIC_DEMO_BROKER_KEY" ]; then
        local mode=''
        mode="$(stat -c '%a' "$ATOMIC_DEMO_BROKER_KEY" 2> /dev/null || true)"
        if [ "$mode" = '600' ] || [ "$mode" = '400' ]; then
            ok "key : $ATOMIC_DEMO_BROKER_KEY (mode $mode)"
        else
            bad "key $ATOMIC_DEMO_BROKER_KEY has mode ${mode:-?};"\
                'the broker refuses anything looser than 0600'
        fi
    else
        bad "key missing or unreadable: $ATOMIC_DEMO_BROKER_KEY"
    fi
}

# --------------------------------------------------------------------------
port_free() {
    local port="$1"

    "$VE/bin/python" - "$port" <<'PY' 2> /dev/null
import socket
import sys

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(('127.0.0.1', int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

check_ports() {
    section 'ports'

    if port_free "$ATOMIC_DEMO_BROKER_PORT"; then
        ok "127.0.0.1:$ATOMIC_DEMO_BROKER_PORT free (the demo broker)"
    else
        bad "127.0.0.1:$ATOMIC_DEMO_BROKER_PORT is in use --"\
            'run demo/2026_09_08/down.sh, or set $ATOMIC_DEMO_BROKER_PORT'
    fi
}

# --------------------------------------------------------------------------
check_processes() {
    section 'running processes'

    local listing
    listing="$(pgrep -af '[r]adical-orbit' 2> /dev/null || true)"

    if [ -z "$listing" ]; then
        ok 'no radical-orbit process running'
        return 0
    fi

    printf '%s\n' "$listing" | while IFS= read -r line; do
        printf '       %s\n' "$line"
    done

    warn 'orbit processes are already running (see above); the demo only'\
         'cares if they hold its port or its resource names'
}

# --------------------------------------------------------------------------
check_writable() {
    section 'directories'

    local dir
    for dir in "$ATOMIC_DEMO_TMP" "$RUN_DIR"; do
        if mkdir -p "$dir" 2> /dev/null && [ -w "$dir" ]; then
            ok "writable: $dir"
        else
            bad "not writable: $dir"
        fi
    done

    local dstate="$ATOMIC_DEMO_DISPATCHER_STATE"
    if [ -d "$dstate" ] && [ -n "$(ls -A "$dstate" 2> /dev/null || true)" ]; then
        warn "dispatcher state is not empty ($dstate) -- up.sh backs it up"\
             'into $RUN_DIR and down.sh restores it'
    else
        ok "dispatcher state empty or absent ($dstate)"
    fi
}

# --------------------------------------------------------------------------
check_tools() {
    section 'tools'

    command -v curl > /dev/null 2>&1 \
        && ok  "curl: $(command -v curl)" \
        || bad 'curl missing (up.sh waits for the broker with it)'

    command -v pgrep > /dev/null 2>&1 \
        && ok  'pgrep present' \
        || bad 'pgrep missing (down.sh needs it)'

    command -v timeout > /dev/null 2>&1 \
        && ok  'timeout present' \
        || bad 'timeout missing (coreutils; up.sh/down.sh guard with it)'

    if command -v node > /dev/null 2>&1; then
        ok "node: $(node -v) (the UI module's tests are node-based)"
    else
        warn 'node missing -- the demo runs, but the UI unit tests will not'
    fi
}

# --------------------------------------------------------------------------
check_env_vars() {
    section 'environment (from env.sh)'

    ok "broker url : $RADICAL_ORBIT_BROKER_URL"
    ok "plugins    : $ATOMIC_DEMO_PLUGINS"
    ok "scratch    : $ATOMIC_DEMO_TMP"
    ok "run dir    : $RUN_DIR"
    ok "rhapsody   : $RADICAL_ORBIT_RHAPSODY_BACKEND"
    ok "tool prefix: $ATOMIC_TOOL_PREFIX"

    if [ -z "${RADICAL_ORBIT_TOKEN:-}" ] \
       && [ -z "${RADICAL_ORBIT_BROKER_TOKEN:-}" ]; then
        ok 'no ingress token set (the demo broker runs --no-auth)'
    else
        bad 'a token is set -- it would be sent to a --no-auth broker'
    fi

    [ -z "${RADICAL_LOG_LVL:-}" ] \
        && ok  'RADICAL_LOG_LVL unset (endpoints reject DEBUG_9)' \
        || bad "RADICAL_LOG_LVL is set to '${RADICAL_LOG_LVL}'"

    [ -z "${RADICAL_ORBIT_LOG_FILE:-}" ] \
        && ok  'RADICAL_ORBIT_LOG_FILE unset (pilots would inherit it)' \
        || bad "RADICAL_ORBIT_LOG_FILE is set to '${RADICAL_ORBIT_LOG_FILE}'"

    case ":$PATH:" in
        *":$VE/bin:"*) ok "\$PATH carries $VE/bin" ;;
        *)             bad "\$PATH does not carry $VE/bin" ;;
    esac

    case ":${PYTHONPATH:-}:" in
        *":$ORBIT_SRC/src:"*) ok "\$PYTHONPATH carries $ORBIT_SRC/src" ;;
        *)                    bad "\$PYTHONPATH lacks $ORBIT_SRC/src" ;;
    esac
}

# --------------------------------------------------------------------------
main() {
    printf 'ATOMIC WM demo -- environment check (nothing is started)\n'

    check_repos
    check_venv
    check_tls
    check_ports
    check_processes
    check_writable
    check_tools
    check_env_vars

    printf '\n'

    if [ "$FAILED" -gt 0 ]; then
        printf '%d FAIL, %d WARN -- fix the failures before demo/2026_09_08/up.sh\n' \
               "$FAILED" "$WARNED"
        return 1
    fi

    printf '0 FAIL, %d WARN -- ready for demo/2026_09_08/up.sh\n' "$WARNED"
    return 0
}

main "$@"

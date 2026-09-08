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
#   demo/2026_09_08/check_env.sh --resource perlmutter
#
# --resource picks the host profile (default: $ATOMIC_DEMO_RESOURCE, else
# `local`); the report then describes *that* host's broker, scratch, join
# arguments and stack.
#
# Exit code 0 = ready, 1 = at least one FAIL.  WARNs are things the demo
# survives (a missing node for the UI tests, an unrelated broker running).

set -euo pipefail

# --resource is env.sh's one parameter, and env.sh must be sourced before
# parse_args can use demo_die -- so pick it out of argv here.  parse_args
# below sees (and skips) it again.
_demo_res="${ATOMIC_DEMO_RESOURCE:-local}"
_demo_argv=("$@")
_demo_i=0
while [ "$_demo_i" -lt "${#_demo_argv[@]}" ]; do
    case "${_demo_argv[$_demo_i]}" in
        --resource)   _demo_res="${_demo_argv[$((_demo_i + 1))]:-}" ;;
        --resource=*) _demo_res="${_demo_argv[$_demo_i]#--resource=}" ;;
    esac
    _demo_i=$((_demo_i + 1))
done

# shellcheck source=demo/2026_09_08/env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)/env.sh" \
       "${_demo_res:-local}"

unset _demo_res _demo_argv _demo_i

FAILED=0
WARNED=0

# --------------------------------------------------------------------------
ok()   { printf '  ok   %s\n' "$*"; }
warn() { printf '  WARN %s\n' "$*"; WARNED=$((WARNED + 1)); }
bad()  { printf '  FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

section() { printf '\n%s\n' "$*"; }

# --------------------------------------------------------------------------
check_resource() {
    section 'resource'

    ok "resource   : $ATOMIC_DEMO_RESOURCE (host $ATOMIC_DEMO_HOST, site $ATOMIC_DEMO_SITE)"
    ok "mode       : $ATOMIC_DEMO_MODE -- $ATOMIC_DEMO_MODE_WHY"
    ok "joinable   : ${ATOMIC_DEMO_RESOURCES[*]}"
    case "$ATOMIC_DEMO_BROKER_HOST" in
        *'TODO('*) bad "broker host is still a placeholder:"\
                       "$ATOMIC_DEMO_BROKER_HOST" ;;
        *)         ok  "broker     : $RADICAL_ORBIT_BROKER_URL" ;;
    esac

    case "$ATOMIC_DEMO_SCRATCH_BASE" in
        *'TODO('*) bad "scratch base is still a placeholder:"\
                       "$ATOMIC_DEMO_SCRATCH_BASE -- set"\
                       '$ATOMIC_DEMO_SCRATCH_BASE' ;;
        *)         ok  "scratch    : $ATOMIC_DEMO_SCRATCH_BASE" ;;
    esac

    # the join arguments of every resource this host can join
    local name
    for name in "${ATOMIC_DEMO_RESOURCES[@]}"; do
        if ! demo_join_args "$name" > /dev/null 2>&1; then
            bad "no join arguments for '$name'"
            continue
        fi
        if demo_join_args_todo; then
            bad "join.sh $name would refuse: $DEMO_JOIN_TODO"
        else
            ok  "join.sh $name is ready (no TODO placeholders)"
        fi
    done
}

check_pins() {
    section 'the pinned stack (what the demo installs, everywhere)'

    ok "radical.orbit : $ATOMIC_DEMO_ORBIT_REF"
    ok "                $ATOMIC_DEMO_ORBIT_REPO"
    ok "atomic-wm     : $ATOMIC_DEMO_ATOMIC_REF"
    ok "                $ATOMIC_DEMO_ATOMIC_REPO"
    ok "venv          : $VE"
    ok "clones        : $ATOMIC_DEMO_SRC"
    ok "pip pins      : ${ATOMIC_DEMO_PIP_PINS:-<none>}"

    if [ "${ATOMIC_DEMO_FORCE_CLONE:-0}" = '1' ]; then
        ok 'ATOMIC_DEMO_FORCE_CLONE=1 -- local checkouts are ignored'
    fi
}

# reachable REPO REF LABEL VARNAME
reachable() {
    local repo="$1" ref="$2" label="$3" var="$4"
    local url="${repo#git+}" sha=''

    sha="$(timeout 30 git ls-remote "$url" "$ref" 2> /dev/null \
           | awk 'NR == 1 {print $1}')"

    if [ -n "$sha" ]; then
        ok "$label $ref reachable at ${sha:0:12}"
        return 0
    fi

    case "$url" in
        ssh://*|git@*)
            bad "$label: cannot reach $url ($ref) -- no ssh key on this"\
                "host?  Retry with $var=https://github.com/radical-cybertools/…" ;;
        *)  bad "$label: cannot reach $url ($ref) -- network? wrong ref?" ;;
    esac
}

check_reachable() {
    section 'pinned refs (git ls-remote)'

    reachable "$ATOMIC_DEMO_ORBIT_REPO"  "$ATOMIC_DEMO_ORBIT_REF" \
              'radical.orbit' 'ATOMIC_DEMO_ORBIT_REPO'
    reachable "$ATOMIC_DEMO_ATOMIC_REPO" "$ATOMIC_DEMO_ATOMIC_REF" \
              'atomic-wm'     'ATOMIC_DEMO_ATOMIC_REPO'
}

# checkout_state PATH REF LABEL -- is this checkout an install source?
checkout_state() {
    local path="$1" ref="$2" label="$3" branch='' head=''

    if ! git -C "$path" rev-parse --git-dir > /dev/null 2>&1; then
        warn "$label: no git checkout at $path -- ensure_stack will clone"
        return 0
    fi

    branch="$(git -C "$path" rev-parse --abbrev-ref HEAD 2> /dev/null || true)"
    head="$(  git -C "$path" rev-parse HEAD             2> /dev/null || true)"

    if [ "$branch" != "$ref" ]; then
        warn "$label: $path is on '${branch:-?}', the demo pins '$ref' --"\
             'ensure_stack will install from its own clone instead'
        return 0
    fi

    if [ -n "$(git -C "$path" status --porcelain --untracked-files=no \
               2> /dev/null)" ]; then
        warn "$label: $path is on $ref but has uncommitted changes --"\
             'ensure_stack will install from its own clone instead'
        return 0
    fi

    ok "$label: $path on $ref @ ${head:0:12}, clean (ensure_stack installs"\
       'from here)'
}

# clone_state DIR REF LABEL -- the ensure_stack clone, if there is one
clone_state() {
    local dir="$1" ref="$2" label="$3" branch='' head='' pulled=''

    if ! git -C "$dir" rev-parse --git-dir > /dev/null 2>&1; then
        ok "$label clone: none yet ($dir) -- the first run clones it"
        return 0
    fi

    branch="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2> /dev/null || true)"
    head="$(  git -C "$dir" rev-parse HEAD             2> /dev/null || true)"

    if [ -f "$dir/.git/FETCH_HEAD" ]; then
        pulled="$(date -r "$dir/.git/FETCH_HEAD" '+%Y-%m-%d %H:%M' \
                  2> /dev/null || true)"
    fi

    ok "$label clone: $dir on ${branch:-?} @ ${head:0:12}"\
       "(last pull: ${pulled:-never since clone})"

    [ "$branch" = "$ref" ] \
        || warn "$label clone is on '${branch:-?}', not '$ref' --"\
                'ensure_stack re-clones it'
}

check_sources() {
    section 'install sources'

    checkout_state "$ORBIT_SRC"  "$ATOMIC_DEMO_ORBIT_REF"  'radical.orbit'
    checkout_state "$ATOMIC_SRC" "$ATOMIC_DEMO_ATOMIC_REF" 'atomic-wm'

    clone_state "$ATOMIC_DEMO_SRC/radical.orbit" \
                "$ATOMIC_DEMO_ORBIT_REF"  'radical.orbit'
    clone_state "$ATOMIC_DEMO_SRC/ATOMIC" \
                "$ATOMIC_DEMO_ATOMIC_REF" 'atomic-wm'

    [ -f "$ATOMIC_SRC/examples/workflow_vacancy.json" ] \
        && ok  'examples/workflow_vacancy.json present' \
        || bad 'examples/workflow_vacancy.json missing (smoke.py needs it)'
}

# stamped PKG REF -- compare $VE/atomic-demo.stamp against the pin
stamped() {
    local pkg="$1" ref="$2" got_ref='' got_commit='' got_src='' got_when=''

    got_ref="$(   demo_stamp_get "$pkg" ref)"
    got_commit="$(demo_stamp_get "$pkg" commit)"
    got_src="$(   demo_stamp_get "$pkg" source)"
    got_when="$(  demo_stamp_get "$pkg" stamped)"

    if [ -z "$got_ref" ]; then
        warn "$pkg: nothing stamped -- broker.sh has not installed here yet"
        return 0
    fi

    if [ "$got_ref" != "$ref" ]; then
        bad "$pkg: the venv holds '$got_ref', the demo pins '$ref' --"\
            'run broker.sh (without --skip-install)'
        return 0
    fi

    ok "$pkg: $got_ref @ ${got_commit:0:12} from $got_src ($got_when)"
}

check_stamp() {
    section "install stamp ($ATOMIC_DEMO_STAMP)"

    if [ ! -f "$ATOMIC_DEMO_STAMP" ]; then
        warn "no stamp yet -- broker.sh writes it (venv: $VE)"
        return 0
    fi

    stamped 'radical.orbit' "$ATOMIC_DEMO_ORBIT_REF"
    stamped 'atomic-wm'     "$ATOMIC_DEMO_ATOMIC_REF"
    stamped 'pip-pins'      "${ATOMIC_DEMO_PIP_PINS:-}"
}

# --------------------------------------------------------------------------
check_venv() {
    section "venv ($VE)"

    if [ -x "$VE/bin/python" ]; then
        ok "python: $("$VE/bin/python" -V 2>&1) ($VE/bin/python)"
    else
        warn "no python in $VE/bin -- broker.sh creates the venv"
        printf '       (%s -V: %s)\n' "$ATOMIC_DEMO_PYTHON" \
               "$("$ATOMIC_DEMO_PYTHON" -V 2>&1 || echo 'not found')"
        return 0
    fi

    "$VE/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        && ok  'python >= 3.10' \
        || bad 'the venv python is older than 3.10'

    # the installed distributions -- this is what every role actually runs
    local ver
    for ver in radical.orbit atomic-wm; do
        local got=''
        got="$("$VE/bin/python" -c "from importlib.metadata import version
print(version('$ver'))" 2> /dev/null || true)"
        [ -n "$got" ] \
            && ok   "$ver installed: $got" \
            || warn "$ver not installed yet (broker.sh installs it)"
    done

    "$VE/bin/python" -c 'import requests' 2> /dev/null \
        && ok  'requests importable (the CLIs need it)' \
        || warn 'requests not importable -- broker.sh installs atomic-wm[cli]'

    # the entry points -- all of them come out of the venv, never out of a
    # checkout: that is the whole point of the pin
    local tool missing=''
    for tool in atomic-join atomic-leave atomic-resources atomic-campaign \
                atomic-fake-md atomic-fake-train; do
        [ -x "$VE/bin/$tool" ] || missing="$missing $tool"
    done

    [ -z "$missing" ] \
        && ok   'atomic console scripts installed' \
        || warn "console scripts not installed yet:$missing (broker.sh does it)"

    local bin
    for bin in radical-orbit-broker.py radical-orbit-endpoint.py; do
        [ -f "$VE/bin/$bin" ] \
            && ok   "$bin installed in $VE/bin" \
            || warn "$VE/bin/$bin missing (broker.sh installs it)"
    done

    # psij launches the wrapper by name -- it must be on $PATH, which
    # env.sh arranges by prepending $VE/bin
    local wrapper=''
    wrapper="$(command -v radical-orbit-endpoint-wrapper.sh 2> /dev/null \
               || true)"
    if [ -n "$wrapper" ]; then
        ok "endpoint wrapper on \$PATH: $wrapper"
    else
        warn 'radical-orbit-endpoint-wrapper.sh not on $PATH -- broker.sh'\
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
    local port="$1" python="$VE/bin/python"

    # the venv may not exist yet on a fresh host
    [ -x "$python" ] || python="$ATOMIC_DEMO_PYTHON"

    "$python" - "$port" <<'PY' 2> /dev/null
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

    # the *opposite* of the old rule: every role runs the installed
    # packages, so a checkout on $PYTHONPATH would shadow the pinned one
    case ":${PYTHONPATH:-}:" in
        *":$ORBIT_SRC/src:"*|*":$ATOMIC_SRC/src:"*)
            bad "\$PYTHONPATH carries a source tree (${PYTHONPATH}) --"\
                'it would shadow the installed, pinned packages' ;;
        *)  ok "\$PYTHONPATH carries no demo source tree" ;;
    esac
}

# --------------------------------------------------------------------------
main() {
    printf 'ATOMIC WM demo -- environment check (nothing is started)\n'

    check_resource
    check_pins
    check_reachable
    check_sources
    check_venv
    check_stamp
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

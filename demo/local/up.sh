#!/usr/bin/env bash
#
# demo/local/up.sh -- bring up the ATOMIC WM demo on localhost.
#
#   1. install radical.orbit and atomic-wm into ve3 (non-editable) so the
#      pilots, the endpoint wrapper, the entry points and the console
#      scripts are all current,
#   2. isolate state: federation / campaign / store go under
#      $ATOMIC_DEMO_TMP; the dispatcher has no env override, so its state
#      dir is backed up and cleared (down.sh restores it),
#   3. start the broker (background, log + pidfile) and wait for
#      GET /endpoints,
#   4. join three local resources with `atomic-join --detach`,
#   5. wait until all three are federated and the two allocation-mode ones
#      report a live pilot, then print the Explorer URL and the table.
#
# Then:  ve3/bin/python demo/local/smoke.py  and  demo/local/down.sh
#
# See demo/local/README.md.

set -euo pipefail

# shellcheck source=demo/local/env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)/env.sh"

DO_INSTALL=1
DO_JOIN=1

# --------------------------------------------------------------------------
usage() {
    cat <<EOF
usage: up.sh [options]

  --skip-install     do not re-install radical.orbit / atomic-wm into ve3
                     (fast iteration; the first run of the day must install)
  --no-join          start the broker only, join no resources
                     (useful while the federation plugin is being written)
  --plugins LIST     broker-hosted plugins (default: \$ATOMIC_DEMO_PLUGINS,
                     currently '$ATOMIC_DEMO_PLUGINS').  A subset such as
                     'task_dispatcher' starts a broker without the demo
                     plugins -- combine with --no-join.
  -h, --help         this text

environment (see env.sh): ATOMIC_DEMO_BROKER_PORT, ATOMIC_DEMO_TMP,
ATOMIC_DEMO_RESOURCE_WAIT, ORBIT_SRC, VE
EOF
}

# --------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --skip-install) DO_INSTALL=0            ; shift   ;;
            --no-join)      DO_JOIN=0               ; shift   ;;
            --plugins)      [ $# -ge 2 ] || demo_die '--plugins needs a value'
                            ATOMIC_DEMO_PLUGINS="$2"; shift 2 ;;
            -h|--help)      usage; exit 0                     ;;
            *)              usage >&2
                            demo_die "unknown argument: $1"    ;;
        esac
    done
}

# --------------------------------------------------------------------------
# fail MSG [LOGFILE...] -- die, naming the logs worth reading
fail() {
    local msg="$1"; shift

    printf '\n'
    demo_warn "$msg"

    for log in "$@"; do
        [ -f "$log" ] || continue
        printf '\n--- last 30 lines of %s ---\n' "$log" >&2
        tail -n 30 "$log" >&2 || true
    done

    printf '\n' >&2
    demo_die "$msg -- up.sh gives up; run demo/local/down.sh before retrying"
}

# --------------------------------------------------------------------------
step_prepare() {
    demo_log "run dir  : $RUN_DIR"
    demo_log "scratch  : $ATOMIC_DEMO_TMP"
    demo_log "broker   : $RADICAL_ORBIT_BROKER_URL"
    demo_log "plugins  : $ATOMIC_DEMO_PLUGINS"

    mkdir -p "$RUN_DIR"
    mkdir -p "$RADICAL_ORBIT_FEDERATION_STATE" \
             "$ATOMIC_CAMPAIGN_STATE"          \
             "$ATOMIC_STORE_ROOT"              \
             "$ATOMIC_WM_STATE"

    local name
    for name in "${ATOMIC_DEMO_RESOURCES[@]}"; do
        mkdir -p "$ATOMIC_DEMO_TMP/$name"
    done

    [ -x "$VE/bin/python" ] \
        || demo_die "no python in $VE/bin -- is ORBIT_SRC=$ORBIT_SRC right?"

    [ -r "$RADICAL_ORBIT_BROKER_CERT" ] \
        || demo_die "broker cert missing: $RADICAL_ORBIT_BROKER_CERT"
    [ -r "$ATOMIC_DEMO_BROKER_KEY" ] \
        || demo_die "broker key missing: $ATOMIC_DEMO_BROKER_KEY"

    if [ -f "$ATOMIC_DEMO_BROKER_PID" ]; then
        local old
        old="$(cat "$ATOMIC_DEMO_BROKER_PID" 2> /dev/null || true)"
        if [ -n "$old" ] && kill -0 "$old" 2> /dev/null; then
            demo_die "a demo broker is still running (pid $old) -- run" \
                     "demo/local/down.sh first"
        fi
        rm -f "$ATOMIC_DEMO_BROKER_PID"
    fi
}

# --------------------------------------------------------------------------
# ve3 currently holds a stale radical.orbit (and a pre-#121 endpoint
# wrapper).  Pilots run the *installed* code, not $PYTHONPATH, so both
# repos have to be installed -- non-editable, per the ground rules.
step_install() {
    if [ "$DO_INSTALL" -eq 0 ]; then
        demo_log 'install : skipped (--skip-install)'
        return 0
    fi

    demo_log "install : radical.orbit from $ORBIT_SRC  (log: $ATOMIC_DEMO_PIP_LOG)"
    : > "$ATOMIC_DEMO_PIP_LOG"

    "$VE/bin/pip" install --quiet --no-input "$ORBIT_SRC" \
        >> "$ATOMIC_DEMO_PIP_LOG" 2>&1 \
        || fail 'pip install radical.orbit failed' "$ATOMIC_DEMO_PIP_LOG"

    demo_log "install : atomic-wm[cli] from $ATOMIC_SRC"

    "$VE/bin/pip" install --quiet --no-input "$ATOMIC_SRC[cli]" \
        >> "$ATOMIC_DEMO_PIP_LOG" 2>&1 \
        || fail 'pip install atomic-wm[cli] failed' "$ATOMIC_DEMO_PIP_LOG"

    local missing=''
    local tool
    for tool in atomic-join atomic-leave atomic-resources atomic-campaign \
                atomic-fake-md atomic-fake-train; do
        [ -x "$VE/bin/$tool" ] || missing="$missing $tool"
    done

    [ -z "$missing" ] || fail "console scripts missing after install:$missing" \
                              "$ATOMIC_DEMO_PIP_LOG"
}

# --------------------------------------------------------------------------
# The task dispatcher's state root is a module constant (no env override)
# and stale sessions are replayed at broker start -- so move it aside.
step_isolate_state() {
    local src="$ATOMIC_DEMO_DISPATCHER_STATE"

    if [ ! -d "$src" ]; then
        demo_log 'state   : no dispatcher state to back up'
        mkdir -p "$src"
        return 0
    fi

    if [ -z "$(ls -A "$src" 2> /dev/null || true)" ]; then
        demo_log 'state   : dispatcher state already empty'
        return 0
    fi

    local bak="$RUN_DIR/state.bak-$(date '+%Y%m%d-%H%M%S')"

    mv "$src" "$bak"
    mkdir -p "$src"
    printf '%s\n' "$bak" > "$ATOMIC_DEMO_STATE_BAK"

    demo_log "state   : dispatcher state moved to $bak (down.sh restores it)"
}

# --------------------------------------------------------------------------
step_start_broker() {
    if demo_broker_alive; then
        demo_die "something already answers on $RADICAL_ORBIT_BROKER_URL --" \
                 "run demo/local/down.sh, or set ATOMIC_DEMO_BROKER_PORT"
    fi

    demo_log "broker  : starting (log: $ATOMIC_DEMO_BROKER_LOG)"

    : > "$ATOMIC_DEMO_BROKER_LOG"

    "$VE/bin/python" "$ORBIT_SRC/bin/radical-orbit-broker.py" \
        --host    "$ATOMIC_DEMO_BROKER_HOST"                  \
        --port    "$ATOMIC_DEMO_BROKER_PORT"                  \
        --no-auth                                             \
        --plugins "$ATOMIC_DEMO_PLUGINS"                      \
        --cert    "$RADICAL_ORBIT_BROKER_CERT"                \
        --key     "$ATOMIC_DEMO_BROKER_KEY"                   \
        >> "$ATOMIC_DEMO_BROKER_LOG" 2>&1 &

    local pid=$!
    printf '%s\n' "$pid" > "$ATOMIC_DEMO_BROKER_PID"

    local deadline=$(( $(date +%s) + ATOMIC_DEMO_BROKER_WAIT ))

    while true; do

        if ! kill -0 "$pid" 2> /dev/null; then
            rm -f "$ATOMIC_DEMO_BROKER_PID"
            fail "broker (pid $pid) died during startup" \
                 "$ATOMIC_DEMO_BROKER_LOG"
        fi

        if demo_broker_alive; then
            demo_log "broker  : up (pid $pid), GET /endpoints answers 200"
            return 0
        fi

        if [ "$(date +%s)" -ge "$deadline" ]; then
            fail "broker did not answer GET /endpoints within" \
                 "${ATOMIC_DEMO_BROKER_WAIT}s" "$ATOMIC_DEMO_BROKER_LOG"
        fi

        sleep "$ATOMIC_DEMO_POLL"
    done
}

# --------------------------------------------------------------------------
step_join() {
    if [ "$DO_JOIN" -eq 0 ]; then
        demo_log 'join    : skipped (--no-join)'
        return 0
    fi

    local name log
    for name in "${ATOMIC_DEMO_RESOURCES[@]}"; do

        demo_join_args "$name"

        log="$RUN_DIR/join-$name.log"

        demo_log "join    : $name (log: $log)"

        # `timeout` guards against atomic-join hanging on a broker that
        # accepts the connection but never answers
        if ! timeout "$ATOMIC_DEMO_JOIN_WAIT" \
                 "$VE/bin/atomic-join" "${DEMO_JOIN_ARGS[@]}" --detach \
                 > "$log" 2>&1; then
            fail "atomic-join $name failed" "$log" \
                 "$ATOMIC_WM_STATE/$name/endpoint.log" \
                 "$ATOMIC_DEMO_BROKER_LOG"
        fi
    done
}

# --------------------------------------------------------------------------
# readiness FILE EXPECT PILOTS -- FILE holds `atomic-resources --json`
# output, EXPECT/PILOTS are comma separated resource names.  Prints one
# status line; exits 0 when everything the demo needs is there.
#
# (the script itself arrives on stdin, so the listing has to be a file)
readiness() {
    "$VE/bin/python" - "$1" "$2" "$3" <<'PY'
import json
import sys

path   = sys.argv[1]
expect = [n for n in sys.argv[2].split(',') if n]
pilots = [n for n in sys.argv[3].split(',') if n]

try:
    with open(path, encoding='utf-8') as fin:
        data = json.load(fin)
except (OSError, ValueError) as e:
    print('unreadable resource listing: %s' % e)
    sys.exit(2)

# `atomic-resources --json` prints a bare list; the federation route
# wraps it -- accept both.
if isinstance(data, dict):
    data = data.get('resources') or []

if not isinstance(data, list):
    print('unexpected resource listing: %r' % type(data).__name__)
    sys.exit(2)

seen = {}
for rec in data:
    if isinstance(rec, dict) and rec.get('name'):
        seen[rec['name']] = rec

waiting = []

for name in expect:
    if name not in seen:
        waiting.append('%s: not joined' % name)

for name in pilots:
    rec = seen.get(name)
    if rec is None:
        continue
    usage = rec.get('usage') or {}
    active = usage.get('pilots_active')
    if not isinstance(active, int) or active < 1:
        waiting.append('%s: pilots_active=%s' % (name, active))

if waiting:
    print('waiting for ' + ', '.join(waiting))
    sys.exit(1)

print('%d resources federated, pilots up on %s'
      % (len(expect), ', '.join(pilots) or '-'))
PY
}

# --------------------------------------------------------------------------
step_wait_resources() {
    if [ "$DO_JOIN" -eq 0 ]; then
        return 0
    fi

    local expect pilots json err deadline status rc

    expect="$(IFS=,; printf '%s' "${ATOMIC_DEMO_RESOURCES[*]}")"
    pilots="$(IFS=,; printf '%s' "${ATOMIC_DEMO_PILOT_RESOURCES[*]}")"

    json="$RUN_DIR/resources.json"
    err="$RUN_DIR/resources.err"

    deadline=$(( $(date +%s) + ATOMIC_DEMO_RESOURCE_WAIT ))

    demo_log "wait    : resources + pilots (budget ${ATOMIC_DEMO_RESOURCE_WAIT}s)"

    while true; do

        status='atomic-resources --json failed'
        rc=1

        if "$VE/bin/atomic-resources" --json > "$json" 2> "$err"; then
            set +e
            status="$(readiness "$json" "$expect" "$pilots")"
            rc=$?
            set -e
        fi

        if [ "$rc" -eq 0 ]; then
            demo_log "wait    : $status"
            return 0
        fi

        if [ "$(date +%s)" -ge "$deadline" ]; then
            demo_warn "still $status"
            fail "resources not ready after ${ATOMIC_DEMO_RESOURCE_WAIT}s" \
                 "$err" "$ATOMIC_DEMO_BROKER_LOG"
        fi

        demo_log "wait    : $status"
        sleep "$ATOMIC_DEMO_POLL"
    done
}

# --------------------------------------------------------------------------
step_report() {
    printf '\n'
    demo_log "Explorer : $RADICAL_ORBIT_BROKER_URL/"
    demo_log "           (self-signed cert -- accept the browser warning)"
    demo_log "logs     : $RUN_DIR/  and  $ATOMIC_WM_STATE/<resource>/endpoint.log"
    printf '\n'

    if [ "$DO_JOIN" -eq 1 ]; then
        "$VE/bin/atomic-resources" || demo_warn 'atomic-resources failed'
        printf '\n'
    fi

    demo_log 'next     : ve3/bin/python demo/local/smoke.py'
    demo_log 'teardown : demo/local/down.sh'
}

# --------------------------------------------------------------------------
main() {
    parse_args "$@"

    step_prepare
    step_install
    step_isolate_state
    step_start_broker
    step_join
    step_wait_resources
    step_report
}


# run unless sourced (sourcing gives access to the single steps, which is
# how the wait-loop helpers are unit tested)
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi

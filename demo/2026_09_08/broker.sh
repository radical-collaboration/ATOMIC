#!/usr/bin/env bash
#
# demo/2026_09_08/broker.sh -- the *broker* role of the ATOMIC WM demo.
#
# One role, one terminal (xGFabric style).  This is terminal 1:
#
#   demo/2026_09_08/broker.sh                # install, isolate state, start
#   demo/2026_09_08/broker.sh --skip-install # fast iteration
#
# It does three things, in this order:
#
#   1. install radical.orbit and atomic-wm into ve3 (non-editable) so the
#      pilots, the endpoint wrapper, the entry points and the console
#      scripts are all current,
#   2. isolate state: federation / campaign / store go under
#      $ATOMIC_DEMO_TMP; the dispatcher has no env override, so its state
#      dir is backed up and cleared (down.sh restores it),
#   3. start the broker (background, log + pidfile) and wait until
#      GET /endpoints answers 200.
#
# The broker keeps running after this script returns -- it is a detached
# background process with a pidfile, and `demo/2026_09_08/down.sh` stops it.
# Running the script again while that broker is alive is refused.
#
# Next: demo/2026_09_08/join.sh <resource>, then demo/2026_09_08/submit.sh.
#
# See demo/2026_09_08/README.md.

set -euo pipefail

# shellcheck source=demo/2026_09_08/env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)/env.sh"

DEMO_TOOL='broker.sh'

DO_INSTALL=1

# --------------------------------------------------------------------------
usage() {
    cat <<EOF
usage: broker.sh [options]

  --skip-install     do not re-install radical.orbit / atomic-wm into ve3
                     (fast iteration; the first run of the day must install)
  --plugins LIST     broker-hosted plugins (default: \$ATOMIC_DEMO_PLUGINS,
                     currently '$ATOMIC_DEMO_PLUGINS').  A subset such as
                     'task_dispatcher' starts a broker without the demo
                     plugins.
  -h, --help         this text

environment (see env.sh): ATOMIC_DEMO_BROKER_PORT, ATOMIC_DEMO_TMP,
ATOMIC_DEMO_BROKER_WAIT, ORBIT_SRC, VE
EOF
}

# --------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --skip-install) DO_INSTALL=0            ; shift   ;;
            --plugins)      [ $# -ge 2 ] || demo_die '--plugins needs a value'
                            ATOMIC_DEMO_PLUGINS="$2"; shift 2 ;;
            -h|--help)      usage; exit 0                     ;;
            *)              usage >&2
                            demo_die "unknown argument: $1"    ;;
        esac
    done

    export ATOMIC_DEMO_PLUGINS
}

# --------------------------------------------------------------------------
step_prepare() {
    demo_log "run dir  : $RUN_DIR"
    demo_log "scratch  : $ATOMIC_DEMO_TMP"
    demo_log "broker   : $RADICAL_ORBIT_BROKER_URL"
    demo_log "plugins  : $ATOMIC_DEMO_PLUGINS"

    demo_mkdirs

    [ -x "$VE/bin/python" ] \
        || demo_die "no python in $VE/bin -- is ORBIT_SRC=$ORBIT_SRC right?"

    [ -r "$RADICAL_ORBIT_BROKER_CERT" ] \
        || demo_die "broker cert missing: $RADICAL_ORBIT_BROKER_CERT"
    [ -r "$ATOMIC_DEMO_BROKER_KEY" ] \
        || demo_die "broker key missing: $ATOMIC_DEMO_BROKER_KEY"

    # idempotence: a live pidfile means a broker of ours is already up
    local old=''
    if old="$(demo_broker_pid)"; then
        demo_die "a demo broker is still running (pid $old) -- run" \
                 "demo/2026_09_08/down.sh first"
    fi

    rm -f "$ATOMIC_DEMO_BROKER_PID"
}

# --------------------------------------------------------------------------
# ve3 may hold a stale radical.orbit (and a pre-#121 endpoint wrapper).
# Pilots run the *installed* code, not $PYTHONPATH, so both repos have to
# be installed -- non-editable, per the ground rules.
step_install() {
    if [ "$DO_INSTALL" -eq 0 ]; then
        demo_log 'install : skipped (--skip-install)'
        return 0
    fi

    demo_log "install : radical.orbit from $ORBIT_SRC  (log: $ATOMIC_DEMO_PIP_LOG)"
    : > "$ATOMIC_DEMO_PIP_LOG"

    "$VE/bin/pip" install --quiet --no-input "$ORBIT_SRC" \
        >> "$ATOMIC_DEMO_PIP_LOG" 2>&1 \
        || demo_fail 'pip install radical.orbit failed' "$ATOMIC_DEMO_PIP_LOG"

    demo_log "install : atomic-wm[cli] from $ATOMIC_SRC"

    "$VE/bin/pip" install --quiet --no-input "$ATOMIC_SRC[cli]" \
        >> "$ATOMIC_DEMO_PIP_LOG" 2>&1 \
        || demo_fail 'pip install atomic-wm[cli] failed' "$ATOMIC_DEMO_PIP_LOG"

    local missing=''
    local tool
    for tool in atomic-join atomic-leave atomic-resources atomic-campaign \
                atomic-fake-md atomic-fake-train; do
        [ -x "$VE/bin/$tool" ] || missing="$missing $tool"
    done

    [ -z "$missing" ] \
        || demo_fail "console scripts missing after install:$missing" \
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
                 "run demo/2026_09_08/down.sh, or set ATOMIC_DEMO_BROKER_PORT"
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
            demo_fail "broker (pid $pid) died during startup" \
                      "$ATOMIC_DEMO_BROKER_LOG"
        fi

        if demo_broker_alive; then
            demo_log "broker  : up (pid $pid), GET /endpoints answers 200"
            return 0
        fi

        if [ "$(date +%s)" -ge "$deadline" ]; then
            demo_fail "broker did not answer GET /endpoints within" \
                      "${ATOMIC_DEMO_BROKER_WAIT}s" "$ATOMIC_DEMO_BROKER_LOG"
        fi

        sleep "$ATOMIC_DEMO_POLL"
    done
}

# --------------------------------------------------------------------------
step_report() {
    demo_hint "Explorer: $RADICAL_ORBIT_BROKER_URL/ (self-signed cert)"
    demo_hint "logs    : $ATOMIC_DEMO_BROKER_LOG"
    demo_hint "next    : demo/2026_09_08/join.sh RESOURCE" \
              "(${ATOMIC_DEMO_RESOURCES[*]})"
    demo_hint "teardown: demo/2026_09_08/down.sh"
}

# --------------------------------------------------------------------------
main() {
    parse_args "$@"

    step_prepare
    step_install
    step_isolate_state
    step_start_broker
    step_report
}


# run unless sourced (sourcing gives access to the single steps)
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi

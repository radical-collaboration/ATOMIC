#!/usr/bin/env bash
#
# demo/2026_09_08/broker.sh -- the *broker* role of the ATOMIC WM demo.
#
# One role, one terminal (xGFabric style).  This is terminal 1:
#
#   demo/2026_09_08/broker.sh                # install, isolate state, start
#   demo/2026_09_08/broker.sh --skip-install # fast iteration
#   demo/2026_09_08/broker.sh --reinstall    # force pip even on a match
#
# It does three things, in this order:
#
#   1. `ensure_stack` (env.sh): put the *pinned* radical.orbit and
#      atomic-wm into $ATOMIC_DEMO_VE, non-editable, so the broker, the
#      endpoints, the pilots, the endpoint wrapper and the console scripts
#      all run the same code -- on this host and on every other one,
#   2. isolate state: federation / campaign / store go under
#      $ATOMIC_DEMO_TMP; the dispatcher has no env override, so its state
#      dir is backed up and cleared (down.sh restores it),
#   3. start the broker (background, log + pidfile) and wait until
#      GET /endpoints answers 200.
#
# The broker it starts is the *installed* one ($VE/bin), never a script
# out of a checkout: on 2026-09-08 the broker host had radical.orbit on
# `devel` and the broker came up without the `federation` plugin.
#
# The broker keeps running after this script returns -- it is a detached
# background process with a pidfile, and `demo/2026_09_08/down.sh` stops it.
# Running the script again while that broker is alive is refused.
#
# Next: demo/2026_09_08/join.sh <resource>, then demo/2026_09_08/submit.sh.
#
# See demo/2026_09_08/README.md.

set -euo pipefail

# --resource is env.sh's one parameter, and env.sh must be sourced before
# parse_args can use demo_die -- so pick it out of argv here.  parse_args
# below sees (and skips) it again.
_demo_res="${ATOMIC_DEMO_RESOURCE:-}"
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
       "${_demo_res:-}"

unset _demo_res _demo_argv _demo_i

DEMO_TOOL='broker.sh'

# remember this host's resource for the scripts that take none (down.sh)
mkdir -p "$RUN_DIR" && printf '%s\n' "$ATOMIC_DEMO_RESOURCE" > "$RUN_DIR/resource"


# a broker bound to loopback is reachable on loopback only: dial it there,
# whatever the default broker host is (radical.3 for every resource)
if [ "$ATOMIC_DEMO_BROKER_BIND" = '127.0.0.1' ] \
        && [ "$ATOMIC_DEMO_BROKER_HOST" != '127.0.0.1' ]; then
    ATOMIC_DEMO_BROKER_HOST='127.0.0.1'
    export ATOMIC_DEMO_BROKER_HOST
    export RADICAL_ORBIT_BROKER_URL="https://127.0.0.1:$ATOMIC_DEMO_BROKER_PORT"
fi

# --------------------------------------------------------------------------
usage() {
    cat <<EOF
usage: broker.sh [--resource local|r3] [options]

  --resource NAME    which host this broker is for (default: \$ATOMIC_DEMO_RESOURCE
                     or 'local').  'local' = the laptop (127.0.0.1:8010),
                     'r3' = the distributed run's broker host.

  --keep-state       keep the federation, campaign and results state of
                     the previous run (default: a fresh broker starts with
                     an empty federation -- resources re-join).
  --skip-install     do not touch the venv at all (fast iteration; the
                     first run on a host must install)
  --reinstall        pip install both packages even when the venv's stamp
                     already matches the pinned refs
  --plugins LIST     broker-hosted plugins (default: \$ATOMIC_DEMO_PLUGINS,
                     currently '$ATOMIC_DEMO_PLUGINS').  A subset such as
                     'task_dispatcher' starts a broker without the demo
                     plugins.
  -h, --help         this text

The stack is pinned: \$ATOMIC_DEMO_ORBIT_REPO@\$ATOMIC_DEMO_ORBIT_REF
($ATOMIC_DEMO_ORBIT_REF) and \$ATOMIC_DEMO_ATOMIC_REPO@\$ATOMIC_DEMO_ATOMIC_REF
($ATOMIC_DEMO_ATOMIC_REF), installed into \$ATOMIC_DEMO_VE ($ATOMIC_DEMO_VE).
See "How the stack is pinned and installed" in the README.

environment (see env.sh): ATOMIC_DEMO_BROKER_PORT, ATOMIC_DEMO_TMP,
ATOMIC_DEMO_BROKER_WAIT, ATOMIC_DEMO_VE, ATOMIC_DEMO_SRC,
ATOMIC_DEMO_FORCE_CLONE, ORBIT_SRC
EOF
}

# --------------------------------------------------------------------------
KEEP_STATE=0

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --resource|--resource=*)
                            # already consumed, before env.sh was sourced
                            case "$1" in --resource) shift 2 ;; *) shift ;; esac
                            ;;
            --keep-state)   KEEP_STATE=1; shift ;;
            --skip-install) ATOMIC_DEMO_SKIP_INSTALL=1; shift ;;
            --reinstall)    ATOMIC_DEMO_REINSTALL=1   ; shift ;;
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
    demo_log "broker   : $RADICAL_ORBIT_BROKER_URL (binds $ATOMIC_DEMO_BROKER_BIND:$ATOMIC_DEMO_BROKER_PORT)"
    demo_log "plugins  : $ATOMIC_DEMO_PLUGINS"
    demo_log "venv     : $VE"
    demo_log "pins     : radical.orbit@$ATOMIC_DEMO_ORBIT_REF," \
             "atomic-wm@$ATOMIC_DEMO_ATOMIC_REF"

    demo_mkdirs

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
# The venv may hold a stale radical.orbit (or one built from a checkout on
# the wrong branch, which is what killed the 2026-09-08 broker host).
# ensure_stack resolves both packages against the pins, installs only what
# moved, and logs to $RUN_DIR/install.log.  Nothing runs from a source
# tree: broker, endpoints, pilots and CLIs all use $VE.
step_install() {

    ensure_stack

    [ -x "$VE/bin/python" ] \
        || demo_die "no python in $VE/bin -- run without --skip-install," \
                    'or point $ATOMIC_DEMO_VE at a prepared venv'

    demo_report_stack
}

# --------------------------------------------------------------------------
# The task dispatcher's state root is a module constant (no env override)
# and stale sessions are replayed at broker start -- so move it aside.
step_isolate_state() {
    local src="$ATOMIC_DEMO_DISPATCHER_STATE"
    local bak="$RUN_DIR/state.bak-$(date '+%Y%m%d-%H%M%S')"

    if [ -d "$src" ] && [ -n "$(ls -A "$src" 2> /dev/null || true)" ]; then
        mv "$src" "$bak"
        mkdir -p "$src"
        printf '%s\n' "$bak" > "$ATOMIC_DEMO_STATE_BAK"
        demo_log "state   : dispatcher state moved to $bak (down.sh restores it)"
    else
        demo_log 'state   : no dispatcher state to back up'
        mkdir -p "$src"
    fi

    # The demo's own state -- federation records, campaigns, results --
    # persists across broker restarts by design (restart replay).  For a
    # demo that is the wrong default: a resource whose endpoint died with
    # the old broker comes back as 'lost' and a campaign list full of
    # yesterday's runs is not a clean screen.  Moved aside, kept for
    # forensics, never restored.  --keep-state opts out.
    if [ "$KEEP_STATE" -eq 1 ]; then
        demo_log 'state   : keeping federation/campaign/results state (--keep-state)'
        return 0
    fi

    local d moved=0
    for d in "$RADICAL_ORBIT_FEDERATION_STATE" "$ATOMIC_CAMPAIGN_STATE" \
             "$ATOMIC_STORE_ROOT"; do
        [ -d "$d" ] && [ -n "$(ls -A "$d" 2> /dev/null || true)" ] || continue
        mkdir -p "$bak/demo"
        mv "$d" "$bak/demo/$(basename "$d")"
        moved=1
    done
    mkdir -p "$RADICAL_ORBIT_FEDERATION_STATE" "$ATOMIC_CAMPAIGN_STATE" \
             "$ATOMIC_STORE_ROOT"
    if [ "$moved" -eq 1 ]; then
        demo_log "state   : federation/campaign/results of the previous run moved to $bak/demo (fresh federation; --keep-state keeps it)"
    fi
}

# --------------------------------------------------------------------------
step_start_broker() {
    if demo_broker_alive; then
        demo_die "something already answers on $RADICAL_ORBIT_BROKER_URL --" \
                 "run demo/2026_09_08/down.sh, or set ATOMIC_DEMO_BROKER_PORT"
    fi

    demo_log "broker  : starting (log: $ATOMIC_DEMO_BROKER_LOG)"

    : > "$ATOMIC_DEMO_BROKER_LOG"

    "$VE/bin/python" "$VE/bin/radical-orbit-broker.py"        \
        --host    "$ATOMIC_DEMO_BROKER_BIND"                  \
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
            if [ "$ATOMIC_DEMO_BROKER_BIND" != '127.0.0.1' ]; then
                demo_log "remote  : on Perlmutter / Odo, before join.sh:"
                demo_log "remote  :   export ATOMIC_DEMO_BROKER_HOST=$ATOMIC_DEMO_BROKER_HOST"
                demo_log "remote  :   scp $ATOMIC_DEMO_BROKER_HOST:.radical/orbit/broker_cert.pem ~/.radical/orbit/"
                demo_log "remote  : (the broker advertises itself as:" \
                         "$(grep -o 'on https://[^ ]*' "$ATOMIC_DEMO_BROKER_LOG" | sed 's/^on //' | grep -v '0\.0\.0\.0' | tr '\n' ' '))"
            fi
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
    if [ "$ATOMIC_DEMO_BROKER_BIND" = '127.0.0.1' ]; then
        demo_hint "next    : ATOMIC_DEMO_BROKER_HOST=127.0.0.1" \
                  "demo/2026_09_08/join.sh RESOURCE (${ATOMIC_DEMO_RESOURCES[*]})"
        demo_hint "          (the default broker is radical.3; this one is loopback-only)"
    else
        demo_hint "next    : demo/2026_09_08/join.sh RESOURCE" \
                  "(${ATOMIC_DEMO_RESOURCES[*]})"
    fi
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

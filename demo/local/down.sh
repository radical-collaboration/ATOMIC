#!/usr/bin/env bash
#
# demo/local/down.sh -- tear the ATOMIC WM localhost demo down again.
#
#   1. `atomic-leave` for every joined resource (removes it from the
#      federation, stops its endpoint and any surviving pilot children),
#   2. stop the broker via its pidfile,
#   3. sweep up endpoints/pilots that outlived their parent -- psij's
#      `local` executor cancels only the wrapper job, so a pilot endpoint
#      can survive and keep redialing the broker,
#   4. verify no orbit process is left,
#   5. restore the dispatcher state dir up.sh moved aside.
#
# Logs are kept on purpose (demo/local/run/); scratch and plugin state
# under $ATOMIC_DEMO_TMP are kept too unless --wipe is given.
#
# Idempotent: running it twice, or on a machine where nothing is up, is
# fine and exits 0.

set -euo pipefail

# shellcheck source=demo/local/env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)/env.sh"

KILL_ALL_ENDPOINTS=0
WIPE=0
RC=0

# --------------------------------------------------------------------------
usage() {
    cat <<EOF
usage: down.sh [options]

  --all-endpoints    kill *every* radical-orbit-endpoint process, not just
                     this demo's.  Off by default: the machine may host
                     unrelated endpoints of the user's own brokers.
  --wipe             also remove $ATOMIC_DEMO_TMP (scratch, federation and
                     campaign state, the result store).  Logs in
                     $RUN_DIR are kept either way.
  -h, --help         this text
EOF
}

# --------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --all-endpoints) KILL_ALL_ENDPOINTS=1; shift ;;
            --wipe)          WIPE=1              ; shift ;;
            -h|--help)       usage; exit 0               ;;
            *)               usage >&2
                             demo_die "unknown argument: $1" ;;
        esac
    done
}

# --------------------------------------------------------------------------
# stop_pid PID LABEL -- SIGTERM, wait, SIGKILL.  0 if it is gone afterwards.
stop_pid() {
    local pid="$1" label="$2" deadline

    kill -0 "$pid" 2> /dev/null || return 0

    kill -TERM "$pid" 2> /dev/null || true

    deadline=$(( $(date +%s) + ATOMIC_DEMO_STOP_WAIT ))

    while kill -0 "$pid" 2> /dev/null; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
            demo_warn "$label (pid $pid) ignored SIGTERM -- killing"
            kill -KILL "$pid" 2> /dev/null || true
            sleep 1
            break
        fi
        sleep 1
    done

    if kill -0 "$pid" 2> /dev/null; then
        demo_warn "$label (pid $pid) is still alive"
        return 1
    fi

    return 0
}

# --------------------------------------------------------------------------
step_leave() {
    local name

    for name in "${ATOMIC_DEMO_RESOURCES[@]}"; do

        demo_log "leave   : $name"

        # atomic-leave talks to the broker, stops the endpoint child and
        # kills pilots carrying this resource's pool prefix.  A broker
        # that is already gone is not an error here -- the process
        # teardown below still has to happen.
        if ! timeout "$ATOMIC_DEMO_STOP_WAIT" \
                 "$VE/bin/atomic-leave" "$name" \
                 >> "$RUN_DIR/leave.log" 2>&1; then
            demo_warn "atomic-leave $name reported a problem" \
                      "(see $RUN_DIR/leave.log)"
        fi
    done
}

# --------------------------------------------------------------------------
step_stop_broker() {
    local pid=''

    if [ -f "$ATOMIC_DEMO_BROKER_PID" ]; then
        pid="$(cat "$ATOMIC_DEMO_BROKER_PID" 2> /dev/null || true)"
    fi

    if [ -z "$pid" ]; then
        demo_log 'broker  : no pidfile, nothing to stop'
    else
        demo_log "broker  : stopping (pid $pid)"
        stop_pid "$pid" 'broker' || RC=1
        rm -f "$ATOMIC_DEMO_BROKER_PID"
    fi

    if demo_broker_alive; then
        demo_warn "something still answers on $RADICAL_ORBIT_BROKER_URL" \
                  '-- not started by this demo?'
        RC=1
    fi
}

# --------------------------------------------------------------------------
# Pilots and endpoints that outlived their parents.  Scoped to this
# demo's names by default: `ep_<name>` is the joined endpoint,
# `fed-<name>_<pid>` the dispatcher's child endpoint for a pilot.
step_sweep_endpoints() {
    local pattern names

    if [ "$KILL_ALL_ENDPOINTS" -eq 1 ]; then
        pattern='radical-orbit-endpoint'
    else
        names="$(IFS='|'; printf '%s' "${ATOMIC_DEMO_RESOURCES[*]}")"
        pattern="radical-orbit-endpoint.*(ep_|fed-)($names)"
    fi

    local sig
    for sig in TERM KILL; do

        local pids
        pids="$(pgrep -f "$pattern" 2> /dev/null || true)"

        [ -n "$pids" ] || break

        demo_log "sweep   : SIG$sig to surviving endpoints/pilots:" \
                 "$(printf '%s' "$pids" | tr '\n' ' ')"

        # shellcheck disable=SC2086
        kill -"$sig" $pids 2> /dev/null || true

        sleep 3
    done
}

# --------------------------------------------------------------------------
# The demo must leave nothing behind.  Other orbit processes may well be
# the user's own (their brokers live on 8000/8003) -- those are reported
# but do not fail the teardown.  A process counts as ours if its command
# line carries the demo broker port or one of the demo resource names.
step_verify() {
    local listing mine=0 line

    listing="$(pgrep -af '[r]adical-orbit' 2> /dev/null || true)"

    if [ -z "$listing" ]; then
        demo_log 'verify  : no radical-orbit process left'
        return 0
    fi

    local names
    names="$(IFS='|'; printf '%s' "${ATOMIC_DEMO_RESOURCES[*]}")"

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        if printf '%s' "$line" \
               | grep -Eq ":$ATOMIC_DEMO_BROKER_PORT|--port +$ATOMIC_DEMO_BROKER_PORT|($names)"
        then
            demo_warn "demo process survived teardown: $line"
            mine=1
        else
            demo_log  "verify  : unrelated orbit process (left alone): $line"
        fi
    done <<< "$listing"

    if [ "$mine" -eq 1 ]; then
        demo_warn 'retry, or re-run with --all-endpoints'
        RC=1
    else
        demo_log 'verify  : no demo process left'
    fi
}

# --------------------------------------------------------------------------
step_restore_state() {
    local bak src="$ATOMIC_DEMO_DISPATCHER_STATE"

    if [ ! -f "$ATOMIC_DEMO_STATE_BAK" ]; then
        demo_log 'state   : no dispatcher backup to restore'
        return 0
    fi

    bak="$(cat "$ATOMIC_DEMO_STATE_BAK" 2> /dev/null || true)"

    if [ -z "$bak" ] || [ ! -d "$bak" ]; then
        demo_warn "recorded dispatcher backup '$bak' is gone -- not restoring"
        rm -f "$ATOMIC_DEMO_STATE_BAK"
        return 0
    fi

    # the demo's own dispatcher state goes away with it
    if [ -d "$src" ]; then
        rm -rf "$src.demo" 2> /dev/null || true
        mv "$src" "$src.demo"
    fi

    mkdir -p "$(dirname "$src")"
    mv "$bak" "$src"
    rm -rf "$src.demo" 2> /dev/null || true
    rm -f "$ATOMIC_DEMO_STATE_BAK"

    demo_log "state   : dispatcher state restored from $bak"
}

# --------------------------------------------------------------------------
step_wipe() {
    if [ "$WIPE" -eq 0 ]; then
        demo_log "kept    : $ATOMIC_DEMO_TMP (use --wipe to remove)"
        return 0
    fi

    case "$ATOMIC_DEMO_TMP" in
        /tmp/*) rm -rf "$ATOMIC_DEMO_TMP"
                demo_log "wiped   : $ATOMIC_DEMO_TMP" ;;
        *)      demo_warn "refusing to wipe '$ATOMIC_DEMO_TMP'" \
                          '-- not under /tmp' ;;
    esac
}

# --------------------------------------------------------------------------
main() {
    parse_args "$@"

    mkdir -p "$RUN_DIR"

    step_leave
    step_stop_broker
    step_sweep_endpoints
    step_verify
    step_restore_state
    step_wipe

    printf '\n'
    demo_log "logs kept in $RUN_DIR/"

    if [ "$RC" -ne 0 ]; then
        demo_warn 'teardown was not clean -- see the warnings above'
    fi

    return "$RC"
}


# run unless sourced (see up.sh)
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi

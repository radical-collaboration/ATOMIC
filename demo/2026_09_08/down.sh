#!/usr/bin/env bash
#
# demo/2026_09_08/down.sh -- tear the ATOMIC WM localhost demo down again.
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
# Logs are kept on purpose (demo/2026_09_08/run/); scratch and plugin state
# under $ATOMIC_DEMO_TMP are kept too unless --wipe is given.
#
# Idempotent: running it twice, or on a machine where nothing is up, is
# fine and exits 0.

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

KILL_ALL_ENDPOINTS=0
WIPE=0
RC=0

# --------------------------------------------------------------------------
usage() {
    cat <<EOF
usage: down.sh [--resource NAME] [options]

  --resource NAME    which host to tear down (default: \$ATOMIC_DEMO_RESOURCE
                     or 'local'); one of $ATOMIC_DEMO_KNOWN

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
            --resource|--resource=*)
                             # already consumed, before env.sh was sourced
                             case "$1" in --resource) shift 2 ;; *) shift ;; esac
                             ;;
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
        # kills the pilots of this resource's members.  A broker that is
        # already gone is not an error here -- the process teardown below
        # still has to happen.
        #
        # --cancel-tasks: leaving a class pool does NOT cancel the
        # resource's queued tasks by default (another member could still
        # run them).  This is a full teardown, so nothing should be left
        # queued for a federation that is about to disappear.
        if ! timeout "$ATOMIC_DEMO_STOP_WAIT" \
                 "$VE/bin/atomic-leave" "$name" --cancel-tasks \
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
# Pilots and endpoints that outlived their parents.  A process is the
# demo's when its command line carries
#   - `ep_<name>` for ANY demo resource name (not only the one this
#     shell was sourced for: `down.sh` without --resource on r3 must
#     still take r3's endpoint down),
#   - `fed-<class>_<name>.<member>_p.<id>`, the dispatcher's child
#     endpoint for a pilot -- the `fed-` prefix is the federation's alone,
#     so every `-n fed-…` is ours (that covers the psij launch wrapper and
#     the dragon launcher lines too),
#   - or the demo broker's port in its --url.
# The user's own brokers (8000/8003) and their endpoints match none of
# these.  --all-endpoints drops the scoping.
demo_process_pattern() {
    local names
    names="$(printf '%s' "$ATOMIC_DEMO_KNOWN" | tr ' ' '|')"
    printf '%s' "radical-orbit-endpoint.*(ep_($names)( |$)|-n +fed-|--url +https://[^ ]*:$ATOMIC_DEMO_BROKER_PORT)"
}

step_sweep_endpoints() {
    local pattern

    if [ "$KILL_ALL_ENDPOINTS" -eq 1 ]; then
        pattern='radical-orbit-endpoint'
    else
        pattern="$(demo_process_pattern)"
    fi

    # SIGTERM first, so the endpoints get to tell the broker; whatever is
    # still there after the grace period is killed outright (-9), and a
    # last pass catches anything a dying parent re-spawned meanwhile.
    local sig
    for sig in TERM KILL KILL; do

        local pids
        pids="$(pgrep -f "$pattern" 2> /dev/null || true)"

        [ -n "$pids" ] || break

        demo_log "sweep   : SIG$sig to surviving endpoints/pilots:" \
                 "$(printf '%s' "$pids" | tr '\n' ' ')"

        # shellcheck disable=SC2086
        kill -"$sig" $pids 2> /dev/null || true

        [ "$sig" = TERM ] && sleep 3 || sleep 1
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

    local pattern
    pattern="$(demo_process_pattern)"

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        if printf '%s' "$line" \
               | grep -Eq "--port +$ATOMIC_DEMO_BROKER_PORT|$pattern"
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
    local entry

    if [ "$WIPE" -eq 0 ]; then
        demo_log "kept    : $ATOMIC_DEMO_TMP (use --wipe to remove)"
        return 0
    fi

    case "$ATOMIC_DEMO_TMP" in
        /tmp/*) ;;
        *)      demo_warn "refusing to wipe '$ATOMIC_DEMO_TMP'" \
                          '-- not under /tmp'
                return 0 ;;
    esac

    # $ATOMIC_DEMO_SRC (the clones of the pinned refs) defaults to a
    # subdirectory of $ATOMIC_DEMO_TMP, and it is a *cache*, not state:
    # wiping it would cost a full re-clone on the next run for nothing.
    for entry in "$ATOMIC_DEMO_TMP"/* "$ATOMIC_DEMO_TMP"/.[!.]*; do
        [ -e "$entry" ]                    || continue
        [ "$entry" = "$ATOMIC_DEMO_SRC" ]  && continue
        rm -rf "$entry"
    done

    rmdir "$ATOMIC_DEMO_TMP" 2> /dev/null || true

    if [ -d "$ATOMIC_DEMO_SRC" ]; then
        demo_log "wiped   : $ATOMIC_DEMO_TMP (kept $ATOMIC_DEMO_SRC)"
    else
        demo_log "wiped   : $ATOMIC_DEMO_TMP"
    fi
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

#!/usr/bin/env bash
#
# demo/2026_09_08/join.sh -- the *resource* role of the ATOMIC WM demo.
#
# One resource, one invocation (xGFabric style).  This is terminal 2:
#
#   demo/2026_09_08/join.sh local_a          # detached: joins and returns
#   demo/2026_09_08/join.sh local_b
#   demo/2026_09_08/join.sh local_c --live   # foreground: the on-camera join
#
# and, on the real machines, exactly the same thing:
#
#   demo/2026_09_08/join.sh r3               # the Rutgers workstation
#   demo/2026_09_08/join.sh perlmutter       # from inside an salloc
#   demo/2026_09_08/join.sh odo
#
# RESOURCE is env.sh's one parameter: it selects the broker URL, the
# site, the scratch base and the join arguments (`demo_join_args`).  The
# join *mode* is detected -- inside a Slurm allocation the endpoint is
# the resource (allocation mode), on a login node it declares members
# (login mode); env.sh prints which it found.
#
# Detached (the default) is what the pre-joined resources want: the
# endpoint keeps running with a pidfile, `atomic-leave <name>` (or
# demo/2026_09_08/down.sh) stops it again.
#
# --live runs `atomic-join` in the *foreground*: the audience sees the
# join happen, the endpoint's own progress lines scroll by, and Ctrl-C
# leaves the federation again.  The process never returns while the
# resource is joined, so this script cannot print the resource table
# afterwards -- it prints where to look for it instead.
#
# See demo/2026_09_08/README.md.

set -euo pipefail

# The resource name is env.sh's one parameter, and env.sh has to be
# sourced before parse_args can use demo_die -- so pick the first
# non-option argument out of argv here and hand it straight over.
# parse_args below does the real parsing against the same argv.
_demo_res="${ATOMIC_DEMO_RESOURCE:-local}"
for _demo_arg in "$@"; do
    case "$_demo_arg" in
        -*) continue ;;
        *)  _demo_res="$_demo_arg"; break ;;
    esac
done

# shellcheck source=demo/2026_09_08/env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)/env.sh" \
       "$_demo_res"

unset _demo_res _demo_arg

DEMO_TOOL='join.sh'

NAME=''
LIVE=0

# --------------------------------------------------------------------------
usage() {
    cat <<EOF
usage: join.sh RESOURCE [--live]

  RESOURCE           the resource to join.  It is also env.sh's parameter:
                     it selects the broker URL, the site, the scratch base
                     and the join arguments (env.sh, demo_join_args).
                     Known here: ${ATOMIC_DEMO_RESOURCES[*]}
                     (all names: $ATOMIC_DEMO_KNOWN)
  --live             run atomic-join in the foreground -- the on-camera
                     join.  Ctrl-C leaves the federation again.  Without
                     it the endpoint is detached and this script returns.
  --skip-install     do not check the venv against the pinned stack
  --reinstall        pip install both packages even on a stamp match
  -h, --help         this text

The resource host runs the same pinned stack as the broker host --
radical.orbit@$ATOMIC_DEMO_ORBIT_REF, atomic-wm@$ATOMIC_DEMO_ATOMIC_REF --
installed into \$ATOMIC_DEMO_VE ($ATOMIC_DEMO_VE) by ensure_stack.

This host: resource '$ATOMIC_DEMO_RESOURCE', site $ATOMIC_DEMO_SITE,
mode $ATOMIC_DEMO_MODE ($ATOMIC_DEMO_MODE_WHY),
scratch $ATOMIC_DEMO_SCRATCH_BASE, broker $RADICAL_ORBIT_BROKER_URL.

environment (see env.sh): ATOMIC_DEMO_RESOURCE, ATOMIC_DEMO_JOIN_WAIT,
ATOMIC_DEMO_TMP, ATOMIC_DEMO_VE, ATOMIC_DEMO_SRC, ATOMIC_DEMO_FORCE_CLONE
EOF
}

# --------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --live)         LIVE=1; shift ;;
            --skip-install) ATOMIC_DEMO_SKIP_INSTALL=1; shift ;;
            --reinstall)    ATOMIC_DEMO_REINSTALL=1   ; shift ;;
            -h|--help)  usage; exit 0 ;;
            -*)         usage >&2
                        demo_die "unknown option: $1" ;;
            *)          [ -z "$NAME" ] || { usage >&2
                            demo_die "one resource at a time (got '$NAME'" \
                                     "and '$1')"; }
                        NAME="$1"; shift ;;
        esac
    done

    if [ -z "$NAME" ]; then
        usage >&2
        demo_die 'which resource?  known:' "${ATOMIC_DEMO_RESOURCES[*]}"
    fi

    if ! demo_known_resource "$NAME"; then
        usage >&2
        demo_die "unknown resource '$NAME' -- known:" \
                 "${ATOMIC_DEMO_RESOURCES[*]}"
    fi
}

# --------------------------------------------------------------------------
# The endpoint is detached and keeps running; `timeout` guards against
# atomic-join hanging on a broker that accepts the connection but never
# answers.
join_detached() {
    local log="$RUN_DIR/join-$NAME.log"

    demo_leave_stale "$NAME" \
        || demo_die "a resource named '$NAME' is already in the federation" \
                    "and alive -- atomic-leave $NAME first if it is yours"

    demo_log "join    : $NAME (log: $log)"

    if ! timeout "$ATOMIC_DEMO_JOIN_WAIT" \
             "$VE/bin/atomic-join" "${DEMO_JOIN_ARGS[@]}" --detach \
             > "$log" 2>&1; then
        demo_fail "atomic-join $NAME failed" "$log" \
                  "$ATOMIC_WM_STATE/$NAME/endpoint.log" \
                  "$ATOMIC_DEMO_BROKER_LOG"
    fi

    demo_hint "leave   : atomic-leave $NAME  (or demo/2026_09_08/down.sh)"
}

# --------------------------------------------------------------------------
# No --detach, no timeout, no log redirection: atomic-join stays in the
# foreground until Ctrl-C.  `exec` hands it the terminal (and the signals)
# outright, so this script adds nothing between the audience and the tool.
join_live() {
    demo_log "join    : $NAME (live -- Ctrl-C leaves the federation again)"
    demo_log 'hint    : in another terminal: atomic-resources'

    demo_leave_stale "$NAME" \
        || demo_die "a resource named '$NAME' is already in the federation" \
                    "and alive -- atomic-leave $NAME first if it is yours"

    exec "$VE/bin/atomic-join" "${DEMO_JOIN_ARGS[@]}"
}

# --------------------------------------------------------------------------
main() {
    parse_args "$@"

    demo_join_args "$NAME"

    # An unfilled template must fail here -- in a second, on the login
    # node -- not four minutes later inside a batch job.
    if demo_join_args_todo; then
        demo_die "the $ATOMIC_DEMO_MODE-mode join arguments for '$NAME'" \
                 "still carry a placeholder: $DEMO_JOIN_TODO --" \
                 'fill in every TODO(...) in demo_join_args' \
                 '(demo/2026_09_08/env.sh) first'
    fi

    demo_mkdirs

    ensure_stack

    demo_require_broker

    if [ "$LIVE" -eq 1 ]; then
        join_live
    else
        join_detached
    fi
}


# run unless sourced (sourcing gives access to the single steps)
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi

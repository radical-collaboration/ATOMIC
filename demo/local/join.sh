#!/usr/bin/env bash
#
# demo/local/join.sh -- the *resource* role of the ATOMIC WM demo.
#
# One resource, one invocation (xGFabric style).  This is terminal 2:
#
#   demo/local/join.sh local_a          # detached: joins and returns
#   demo/local/join.sh local_b
#   demo/local/join.sh local_c --live   # foreground: the on-camera join
#
# The join arguments come from `demo_join_args` in env.sh -- the one
# place that describes the demo's three resources and their five members,
# and the one place to edit for a cross-host run.
#
# Detached (the default) is what the pre-joined resources want: the
# endpoint keeps running with a pidfile, `atomic-leave <name>` (or
# demo/local/down.sh) stops it again.
#
# --live runs `atomic-join` in the *foreground*: the audience sees the
# join happen, the endpoint's own progress lines scroll by, and Ctrl-C
# leaves the federation again.  The process never returns while the
# resource is joined, so this script cannot print the resource table
# afterwards -- it prints where to look for it instead.
#
# See demo/local/README.md.

set -euo pipefail

# shellcheck source=demo/local/env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)/env.sh"

DEMO_TOOL='join.sh'

NAME=''
LIVE=0

# --------------------------------------------------------------------------
usage() {
    cat <<EOF
usage: join.sh RESOURCE [--live]

  RESOURCE           one of: ${ATOMIC_DEMO_RESOURCES[*]}
                     (the join arguments live in env.sh, demo_join_args)
  --live             run atomic-join in the foreground -- the on-camera
                     join.  Ctrl-C leaves the federation again.  Without
                     it the endpoint is detached and this script returns.
  -h, --help         this text

environment (see env.sh): ATOMIC_DEMO_JOIN_WAIT, ATOMIC_DEMO_TMP, VE
EOF
}

# --------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --live)     LIVE=1; shift ;;
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

    demo_log "join    : $NAME (log: $log)"

    if ! timeout "$ATOMIC_DEMO_JOIN_WAIT" \
             "$VE/bin/atomic-join" "${DEMO_JOIN_ARGS[@]}" --detach \
             > "$log" 2>&1; then
        demo_fail "atomic-join $NAME failed" "$log" \
                  "$ATOMIC_WM_STATE/$NAME/endpoint.log" \
                  "$ATOMIC_DEMO_BROKER_LOG"
    fi

    demo_hint "leave   : atomic-leave $NAME  (or demo/local/down.sh)"
}

# --------------------------------------------------------------------------
# No --detach, no timeout, no log redirection: atomic-join stays in the
# foreground until Ctrl-C.  `exec` hands it the terminal (and the signals)
# outright, so this script adds nothing between the audience and the tool.
join_live() {
    demo_log "join    : $NAME (live -- Ctrl-C leaves the federation again)"
    demo_log 'hint    : in another terminal: atomic-resources'

    exec "$VE/bin/atomic-join" "${DEMO_JOIN_ARGS[@]}"
}

# --------------------------------------------------------------------------
main() {
    parse_args "$@"

    demo_join_args "$NAME"

    demo_mkdirs
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

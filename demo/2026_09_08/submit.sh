#!/usr/bin/env bash
#
# demo/2026_09_08/submit.sh -- the *client* role of the ATOMIC WM demo.
#
# One campaign, one invocation (xGFabric style).  This is the client
# terminal, after broker.sh and the joins:
#
#   demo/2026_09_08/submit.sh                # submit and return the id
#   demo/2026_09_08/submit.sh --wait         # ... and follow it to the end
#   demo/2026_09_08/submit.sh --sweep temperature=300,600 --wait
#
# It is `atomic-campaign submit` with the demo's spec and sweep filled
# in, nothing more -- the same command a user would type, so what the
# audience sees on stage is the product, not the harness.
#
# --wait polls `atomic-campaign status` until the campaign is terminal
# and then prints `atomic-campaign results`.  The exit code is 0 only if
# the campaign reached DONE.
#
# The assertions live in smoke.py, not here: this script *runs* the
# demo, smoke.py *proves* it.
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

DEMO_TOOL='submit.sh'

SPEC="$ATOMIC_DEMO_SPEC"
SWEEP="$ATOMIC_DEMO_SWEEP"
WAIT=0
TIMEOUT="$ATOMIC_DEMO_CAMPAIGN_WAIT"
CID=''

# campaign states that end the wait (mirrors smoke.py's TERMINAL)
TERMINAL=' DONE FAILED CANCELED CANCELLED INTERRUPTED ABORTED '

# --------------------------------------------------------------------------
usage() {
    cat <<EOF
usage: submit.sh [--resource NAME] [options]

  --resource NAME    which broker to talk to (default: \$ATOMIC_DEMO_RESOURCE
                     or 'local'); one of $ATOMIC_DEMO_KNOWN

  --sweep K=V,V,…    parameter sweep (default: '$ATOMIC_DEMO_SWEEP').
                     One workflow per value.
  --spec PATH        workflow spec (default: \$ATOMIC_DEMO_SPEC,
                     currently '$ATOMIC_DEMO_SPEC')
  --wait             poll until the campaign is terminal, then print the
                     collected results.  Exit 0 only on DONE.
  --timeout SEC      how long --wait may take (default:
                     \$ATOMIC_DEMO_CAMPAIGN_WAIT, currently $TIMEOUT)
  --skip-install     do not check the venv against the pinned stack
  --reinstall        pip install both packages even on a stamp match
  -h, --help         this text

The client runs the same pinned stack as the broker --
radical.orbit@$ATOMIC_DEMO_ORBIT_REF, atomic-wm@$ATOMIC_DEMO_ATOMIC_REF --
installed into \$ATOMIC_DEMO_VE ($ATOMIC_DEMO_VE) by ensure_stack.
EOF
}

# --------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --sweep)   [ $# -ge 2 ] || demo_die '--sweep needs a value'
                       SWEEP="$2"  ; shift 2 ;;
            --spec)    [ $# -ge 2 ] || demo_die '--spec needs a value'
                       SPEC="$2"   ; shift 2 ;;
            --timeout) [ $# -ge 2 ] || demo_die '--timeout needs a value'
                       TIMEOUT="$2"; shift 2 ;;
            --wait)    WAIT=1      ; shift   ;;
            --resource|--resource=*)
                       # already consumed, before env.sh was sourced
                       case "$1" in --resource) shift 2 ;; *) shift ;; esac
                       ;;
            --skip-install) ATOMIC_DEMO_SKIP_INSTALL=1; shift ;;
            --reinstall)    ATOMIC_DEMO_REINSTALL=1   ; shift ;;
            -h|--help) usage; exit 0         ;;
            *)         usage >&2
                       demo_die "unknown argument: $1" ;;
        esac
    done

    [ -r "$SPEC" ] || demo_die "workflow spec not readable: $SPEC"
}

# --------------------------------------------------------------------------
# campaign_state CID -- the campaign's state, '?' if it cannot be read.
#
# `atomic-campaign status --json` is the contract; the one field this
# needs is dug out with python rather than with a regex over JSON.
campaign_state() {
    local cid="$1" json=''

    json="$("$VE/bin/atomic-campaign" status --json "$cid" 2> /dev/null \
            || true)"

    [ -n "$json" ] || { printf '?\n'; return 0; }

    printf '%s' "$json" | "$VE/bin/python" -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except ValueError:
    data = {}

print(str((data or {}).get("state") or "?").upper())
'
}

# --------------------------------------------------------------------------
step_submit() {
    local log="$RUN_DIR/submit.log" out='' rc=0

    demo_log "spec    : $SPEC"
    demo_log "sweep   : $SWEEP"

    set +e
    out="$("$VE/bin/atomic-campaign" submit --sweep "$SWEEP" "$SPEC" 2>&1)"
    rc=$?
    set -e

    printf '%s\n' "$out" | tee -a "$log"

    [ "$rc" -eq 0 ] || demo_fail 'atomic-campaign submit failed' \
                                 "$ATOMIC_DEMO_BROKER_LOG"

    # 'campaign cmp-b6b15c84 submitted: 3 workflow(s)'
    CID="$(printf '%s\n' "$out" \
           | sed -n 's/^campaign \([^ ]*\) submitted.*/\1/p' | head -n 1)"

    [ -n "$CID" ] || demo_fail 'no campaign id in the submit output' "$log"

    demo_log "campaign: $CID"
}

# --------------------------------------------------------------------------
step_wait() {
    local deadline state='' last=''

    deadline=$(( $(date +%s) + TIMEOUT ))

    demo_log "wait    : campaign $CID (budget ${TIMEOUT}s)"

    while true; do

        state="$(campaign_state "$CID")"

        if [ "$state" != "$last" ]; then
            demo_log "wait    : $state"
            last="$state"
        fi

        case "$TERMINAL" in
            *" $state "*) break ;;
        esac

        if [ "$(date +%s)" -ge "$deadline" ]; then
            "$VE/bin/atomic-campaign" status "$CID" || true
            demo_fail "campaign $CID still $state after ${TIMEOUT}s" \
                      "$ATOMIC_DEMO_BROKER_LOG"
        fi

        sleep "$ATOMIC_DEMO_POLL"
    done

    printf '\n'
    "$VE/bin/atomic-campaign" status "$CID" || true
    printf '\n'
    "$VE/bin/atomic-campaign" results "$CID" || true
    printf '\n'

    if [ "$state" != 'DONE' ]; then
        demo_warn "campaign $CID ended in $state"
        return 1
    fi

    demo_log "done    : campaign $CID is DONE"
}

# --------------------------------------------------------------------------
main() {
    parse_args "$@"

    demo_mkdirs

    ensure_stack

    demo_require_broker

    step_submit

    if [ "$WAIT" -eq 1 ]; then
        step_wait || return 1
        return 0
    fi

    demo_hint "follow  : atomic-campaign status $CID"
    demo_hint "results : atomic-campaign results $CID"
    demo_hint "prove   : $VE/bin/python demo/2026_09_08/smoke.py"
}


# run unless sourced (sourcing gives access to the single steps)
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi

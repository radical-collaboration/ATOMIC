#!/usr/bin/env bash
#
# demo/local/up.sh -- bring the whole ATOMIC WM demo up on localhost, in
# one command.
#
# This is the *automated* path.  It orchestrates the three per-role
# scripts, which is exactly what a human does by hand in three terminals:
#
#   demo/local/broker.sh  [--skip-install] [--plugins LIST]
#   demo/local/join.sh    local_a          (then local_b, local_c)
#   demo/local/submit.sh  --wait           <- up.sh stops before this one
#
# and then adds the two steps that only make sense once *all* resources
# are in: wait until the federation lists them and the allocation-mode
# one reports a live pilot, and print the Explorer URL and the table.
#
# Then:  ve3/bin/python demo/local/smoke.py  and  demo/local/down.sh
#
# See demo/local/README.md.

set -euo pipefail

# shellcheck source=demo/local/env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)/env.sh"

DEMO_TOOL='up.sh'

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

    export ATOMIC_DEMO_PLUGINS
}

# --------------------------------------------------------------------------
# install + state isolation + broker, in broker.sh.  The broker it starts
# is a detached background process with a pidfile, so it outlives that
# script (and this one) and down.sh stops it.
step_broker() {
    local args=(--plugins "$ATOMIC_DEMO_PLUGINS")

    [ "$DO_INSTALL" -eq 1 ] || args+=(--skip-install)

    "$ATOMIC_DEMO_DIR/broker.sh" "${args[@]}" \
        || demo_die 'demo/local/broker.sh failed -- see the output above'
}

# --------------------------------------------------------------------------
# one join.sh per resource, all detached (the live join is a stage moment,
# not something an automated bring-up does)
step_join() {
    if [ "$DO_JOIN" -eq 0 ]; then
        demo_log 'join    : skipped (--no-join)'
        return 0
    fi

    local name
    for name in "${ATOMIC_DEMO_RESOURCES[@]}"; do
        "$ATOMIC_DEMO_DIR/join.sh" "$name" \
            || demo_die "demo/local/join.sh $name failed --" \
                        'run demo/local/down.sh before retrying'
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
            demo_fail "resources not ready after ${ATOMIC_DEMO_RESOURCE_WAIT}s" \
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
    demo_log '           (or demo/local/submit.sh --wait)'
    demo_log 'teardown : demo/local/down.sh'
}

# --------------------------------------------------------------------------
main() {
    parse_args "$@"

    # the role scripts print their own "what next" hints; up.sh has a
    # summary of its own, so they stay quiet while it drives them
    export ATOMIC_DEMO_ORCHESTRATED=1

    step_broker
    step_join
    step_wait_resources
    step_report
}


# run unless sourced (sourcing gives access to the single steps, which is
# how the wait-loop helpers are unit tested)
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi

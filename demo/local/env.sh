# shellcheck shell=bash
#
# demo/local/env.sh -- environment for the ATOMIC WM localhost demo.
#
#   source demo/local/env.sh
#
# Sourced by up.sh / down.sh / check_env.sh, and meant to be sourced into
# an interactive shell as well: after that, `atomic-resources`,
# `atomic-campaign ...` and friends talk to the demo broker without any
# further flags.
#
# Everything here follows the spike findings recorded in
# plans/00-overview.md ("Spike result -- local pool path WORKS"):
#
#   * PATH must carry $VE/bin so psij-launched pilots resolve
#     `radical-orbit-endpoint-wrapper.sh` by name,
#   * PYTHONPATH must carry $ORBIT_SRC/src so broker/endpoint/clients run
#     the feature branch (it does NOT reach pilots -- they run the code
#     `up.sh` installs into ve3),
#   * RADICAL_LOG_LVL must be unset (the user's shell exports DEBUG_9,
#     which the endpoint's --log-level rejects); use RADICAL_ORBIT_LOG_LVL,
#   * RADICAL_ORBIT_LOG_FILE must not be exported (pilots would inherit it),
#   * TLS is mandatory even for a --no-auth broker.
#
# Deliberately no `set -euo pipefail` here: this file is sourced, and a
# stray non-zero return must not kill an interactive shell.

# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------

if [ -n "${BASH_SOURCE[0]:-}" ]; then
    ATOMIC_DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)"
else
    ATOMIC_DEMO_DIR="$(cd "$(dirname "$0")" > /dev/null && pwd)"
fi

# repo roots -- override ORBIT_SRC/VE before sourcing to point elsewhere
ATOMIC_SRC="$(cd "$ATOMIC_DEMO_DIR/../.." > /dev/null && pwd)"
: "${ORBIT_SRC:=/home/merzky/radical/radical.orbit}"
: "${VE:=$ORBIT_SRC/ve3}"

export ATOMIC_DEMO_DIR ATOMIC_SRC ORBIT_SRC VE

# --------------------------------------------------------------------------
# helpers (prefixed: this file lands in interactive shells)
# --------------------------------------------------------------------------

# demo_log MSG...   -- timestamped progress line on stdout
demo_log() {
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

# demo_warn MSG...  -- same, on stderr
demo_warn() {
    printf '[%s] WARNING: %s\n' "$(date '+%H:%M:%S')" "$*" >&2
}

# demo_die MSG...   -- complain and leave (exit code 1)
demo_die() {
    printf '[%s] ERROR: %s\n' "$(date '+%H:%M:%S')" "$*" >&2
    exit 1
}

# demo_prepend_path VAR VALUE -- idempotent ':'-list prepend
demo_prepend_path() {
    local var="$1" val="$2" cur=''

    eval "cur=\${$var:-}"

    case ":$cur:" in
        *":$val:"*) return 0 ;;
    esac

    if [ -z "$cur" ]; then eval "export $var=\"\$val\""
    else                   eval "export $var=\"\$val:\$cur\""
    fi
}

# demo_curl PATH [curl args...] -- GET a gateway route, body on stdout.
#
# `-k` is not optional here: the broker cert is CN=localhost.localdomain
# with no subjectAltName, so `--cacert <cert> https://127.0.0.1:8010/…`
# fails hostname verification.  A pinned cert authenticates the peer on
# its own -- which is exactly what radical.orbit's own clients rely on
# (`runtime.py::_tls_context` sets `check_hostname = False`), and what
# `atomic_wm.client.Client` does too.  curl has no such switch, so it
# gets `-k`; `--cacert` stays for intent, and starts mattering again the
# day the cert carries a matching SAN.
demo_curl() {
    local route="${1#/}"; shift

    curl -sS -k --cacert "$RADICAL_ORBIT_BROKER_CERT" \
         "$@" "$RADICAL_ORBIT_BROKER_URL/$route"
}

# demo_broker_alive -- 0 if GET /endpoints answers 200
demo_broker_alive() {
    local code

    code="$(demo_curl /endpoints -o /dev/null -w '%{http_code}' \
                      --max-time 5 2> /dev/null)" || return 1

    [ "$code" = '200' ]
}

# demo_orbit_pids -- pids of running orbit processes, one per line.
# The '[r]' keeps this function's own caller (a shell whose command line
# may contain the pattern) out of the match.
demo_orbit_pids() {
    pgrep -f '[r]adical-orbit' 2> /dev/null || true
}

# --------------------------------------------------------------------------
# interpreter / path
# --------------------------------------------------------------------------

demo_prepend_path PATH       "$VE/bin"
demo_prepend_path PYTHONPATH "$ORBIT_SRC/src"

# --------------------------------------------------------------------------
# broker
# --------------------------------------------------------------------------

# a non-default port: the user's own brokers live on 8000/8003
: "${ATOMIC_DEMO_BROKER_HOST:=127.0.0.1}"
: "${ATOMIC_DEMO_BROKER_PORT:=8010}"

# the advertised broker URL is the literal bind host -- 0.0.0.0 would
# advertise the FQDN to pilots, so bind to 127.0.0.1 and say so
export ATOMIC_DEMO_BROKER_HOST ATOMIC_DEMO_BROKER_PORT
export RADICAL_ORBIT_BROKER_URL="https://$ATOMIC_DEMO_BROKER_HOST:$ATOMIC_DEMO_BROKER_PORT"
export RADICAL_ORBIT_BROKER_CERT="$HOME/.radical/orbit/broker_cert.pem"

# the key is only ever needed by the broker process itself -- passed as
# --key, never exported (pilots have no business seeing it)
: "${ATOMIC_DEMO_BROKER_KEY:=$HOME/.radical/orbit/broker_key.pem}"

# The broker runs --no-auth.  The client reads $RADICAL_ORBIT_TOKEN and
# falls back to $RADICAL_ORBIT_BROKER_TOKEN, so both have to go: a stale
# token from the user's shell would otherwise be sent to a broker that
# never asked for one.
unset RADICAL_ORBIT_TOKEN
unset RADICAL_ORBIT_BROKER_TOKEN

# broker-hosted plugin set.  Keep this a variable: while the federation /
# campaign plugins are still being written, `up.sh --plugins
# task_dispatcher` brings up a broker with the subset that exists.
: "${ATOMIC_DEMO_PLUGINS:=task_dispatcher,federation,atomic_campaign}"
export ATOMIC_DEMO_PLUGINS

# --------------------------------------------------------------------------
# logging (spike rules)
# --------------------------------------------------------------------------

export RADICAL_ORBIT_LOG_LVL='INFO'
export RADICAL_ORBIT_RHAPSODY_BACKEND='concurrent'

# The campaign runner rewrites a bare `atomic-fake-*` argv[0] into an
# absolute path using this prefix, so a pilot whose python environment
# does not have atomic-wm installed still finds the workload tools.  The
# *broker* process needs it (that is where the task command is built),
# and it inherits it from here.
export ATOMIC_TOOL_PREFIX="$VE/bin"

# RADICAL_LOG_LVL: the user's shell exports DEBUG_9, which the endpoint's
# --log-level default rejects.  RADICAL_ORBIT_LOG_FILE: pilots inherit it
# and would all write to the same file.
unset RADICAL_LOG_LVL
unset RADICAL_ORBIT_LOG_FILE

# --------------------------------------------------------------------------
# state, scratch, logs
# --------------------------------------------------------------------------

# everything the demo creates lives in exactly two places: $ATOMIC_DEMO_TMP
# and $RUN_DIR.  The one exception is the dispatcher's state dir, which
# has no env override -- up.sh backs it up and clears it, down.sh restores.
: "${ATOMIC_DEMO_TMP:=/tmp/atomic-demo}"
export ATOMIC_DEMO_TMP

export RADICAL_ORBIT_FEDERATION_STATE="$ATOMIC_DEMO_TMP/state/federation"
export ATOMIC_CAMPAIGN_STATE="$ATOMIC_DEMO_TMP/state/campaign"
export ATOMIC_STORE_ROOT="$ATOMIC_DEMO_TMP/store"

# atomic-join's own state (endpoint.log + endpoint.pid per resource);
# without this it would be ~/.radical/orbit/atomic/<name>/
export ATOMIC_WM_STATE="$ATOMIC_DEMO_TMP/endpoints"

# logs and pidfiles of the demo run
export RUN_DIR="$ATOMIC_DEMO_DIR/run"

ATOMIC_DEMO_BROKER_LOG="$RUN_DIR/broker.log"
ATOMIC_DEMO_BROKER_PID="$RUN_DIR/broker.pid"
ATOMIC_DEMO_PIP_LOG="$RUN_DIR/pip.log"
ATOMIC_DEMO_SMOKE_LOG="$RUN_DIR/smoke.log"

# pointer file written by up.sh, read by down.sh
ATOMIC_DEMO_STATE_BAK="$RUN_DIR/state.bak.latest"

# The dispatcher's state root is a module constant -- the plugin has no
# env override, so up.sh backs this directory up and clears it.  The
# variable itself is overridable so the harness can be exercised without
# touching a live dispatcher's state.
: "${ATOMIC_DEMO_DISPATCHER_STATE:=$HOME/.radical/orbit/task_dispatcher/state}"

# --------------------------------------------------------------------------
# the three local resources and their five members
# --------------------------------------------------------------------------
#
# A dispatcher pool is a *capability class*, not a site: every member of
# every resource joins the pool for its class, `fed-cpu` or `fed-gpu`.
# local_a joins in `allocation` mode (this process *is* the resource, one
# pilot right away, exactly one implicit member); local_b and local_c join
# in `login` mode with two members each (pilots submitted on demand
# through psij's `local` executor).
#
#   fed-cpu   local_a.default  lammps            2 cores
#             local_b.cpu      lammps + pytorch  2 cores
#             local_c.cpu      pytorch           2 cores
#   fed-gpu   local_b.gpu      pytorch           1 core + 1 GPU   site NERSC
#             local_c.gpu      pytorch           1 core + 1 GPU   site PSC
#
# So `md` (lammps, no GPU) can only run on local_a.default or local_b.cpu,
# and `train` (pytorch, 1 GPU) only in fed-gpu -- whose two members sit at
# two different "sites", which is the point of the whole class-pool
# arrangement.  The GPU members declare ONE core and max_pilots=1, so one
# of them runs one train task at a time and three sweep points cannot fit
# on a single member.
#
# The "GPU" is fake (psij `local`, rhapsody backend `concurrent`): only
# the *declaration* matters for routing, and nothing reserves a GPU.

ATOMIC_DEMO_RESOURCES=(local_a local_b local_c)

# demo_join_args NAME -- fills the array DEMO_JOIN_ARGS with the
# `atomic-join` arguments for one resource (--detach is added by up.sh).
demo_join_args() {
    local name="$1"

    case "$name" in

        local_a) DEMO_JOIN_ARGS=(
                     --name       local_a
                     --mode       allocation
                     --site       Rutgers
                     --kind       workstation
                     --declare    cores=4,gpus=0,mem_gb=8
                     --software   lammps
                     --scratch    "$ATOMIC_DEMO_TMP/local_a") ;;

        local_b) DEMO_JOIN_ARGS=(
                     --name       local_b
                     --mode       login
                     --site       NERSC
                     --kind       hpc
                     --declare    mem_gb=16
                     --member     'cpu:queue=local,account=demo,nodes=1,cpus=2,walltime=1800,node_hours=2,software=lammps,pytorch,site=NERSC'
                     --member     'gpu:queue=local,account=demo,nodes=1,cpus=1,gpus=1,walltime=1800,node_hours=1,max_pilots=1,software=pytorch,site=NERSC'
                     --scratch    "$ATOMIC_DEMO_TMP/local_b") ;;

        local_c) DEMO_JOIN_ARGS=(
                     --name       local_c
                     --mode       login
                     --site       PSC
                     --kind       hpc
                     --declare    mem_gb=16
                     --member     'cpu:queue=local,account=demo,nodes=1,cpus=2,walltime=1800,node_hours=2,software=pytorch,site=PSC'
                     --member     'gpu:queue=local,account=demo,nodes=1,cpus=1,gpus=1,walltime=1800,node_hours=1,max_pilots=1,software=pytorch,site=PSC'
                     --scratch    "$ATOMIC_DEMO_TMP/local_c") ;;

        *)       demo_warn "no join arguments known for '$name'"
                 DEMO_JOIN_ARGS=()
                 return 1 ;;
    esac
}

# resources that must show a live pilot before the smoke test starts
# (allocation mode starts its pilot at join time; a login-mode member has
# min_pilots=0 and only starts one when the first task arrives, so
# local_b and local_c are not waited for)
ATOMIC_DEMO_PILOT_RESOURCES=(local_a)

# --------------------------------------------------------------------------
# timeouts (seconds) -- every wait loop in up.sh/down.sh uses one of these
# --------------------------------------------------------------------------

: "${ATOMIC_DEMO_BROKER_WAIT:=60}"     # broker answering GET /endpoints
: "${ATOMIC_DEMO_JOIN_WAIT:=120}"      # one atomic-join --detach call
: "${ATOMIC_DEMO_RESOURCE_WAIT:=90}"   # all resources listed + pilots up
: "${ATOMIC_DEMO_STOP_WAIT:=15}"       # SIGTERM grace before SIGKILL
: "${ATOMIC_DEMO_POLL:=2}"             # wait-loop poll interval

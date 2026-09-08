# shellcheck shell=bash
#
# demo/2026_09_08/env.sh -- environment for the ATOMIC WM demo.
#
#   source demo/2026_09_08/env.sh [RESOURCE]
#
# ONE positional parameter: the resource this shell is about.  Known
# names, and what each of them means:
#
#   local                the laptop broker host (the default)
#   local_a local_b local_c   the laptop demo's three fake resources
#   r3                   the Rutgers workstation -- also the broker host
#                        of the distributed run
#   perlmutter           NERSC
#   odo                  OLCF's Slurm test system
#
# Nobody is expected to source this by hand: the role scripts do it, and
# each of them takes the resource from its own command line --
# `join.sh <resource>`, `broker.sh|submit.sh|down.sh|check_env.sh
# --resource <name>` -- or from $ATOMIC_DEMO_RESOURCE, defaulting to
# `local`.  Sourcing it into an interactive shell works too, and after
# that `atomic-resources`, `atomic-campaign ...` and friends talk to that
# resource's broker without any further flags.
#
# The name decides: the broker URL, the site, the scratch base, which
# resources are joinable from this host, and the `atomic-join` arguments.
# Everything else in this file is the same everywhere.
#
# This file is the single home of the demo's environment *and* of the
# bash helpers more than one of those scripts needs; nothing below is
# copy-pasted into a role script.
#
# Everything here follows the spike findings recorded in
# plans/00-overview.md ("Spike result -- local pool path WORKS"):
#
#   * PATH must carry $VE/bin so psij-launched pilots resolve
#     `radical-orbit-endpoint-wrapper.sh` by name,
#   * RADICAL_LOG_LVL must be unset (the user's shell exports DEBUG_9,
#     which the endpoint's --log-level rejects); use RADICAL_ORBIT_LOG_LVL,
#   * RADICAL_ORBIT_LOG_FILE must not be exported (pilots would inherit it),
#   * TLS is mandatory even for a --no-auth broker.
#
# There is deliberately NO `PYTHONPATH=$ORBIT_SRC/src` any more: every
# role -- broker, endpoints, pilots, CLIs -- runs the *installed*
# packages, so all of them run the same pinned code.  See "the pinned
# software stack" below.
#
# Deliberately no `set -euo pipefail` here: this file is sourced, and a
# stray non-zero return must not kill an interactive shell.  Nothing here
# installs, clones or starts anything at source time -- `ensure_stack`
# does that, and only the role scripts call it.

# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------

if [ -n "${BASH_SOURCE[0]:-}" ]; then
    ATOMIC_DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)"
else
    ATOMIC_DEMO_DIR="$(cd "$(dirname "$0")" > /dev/null && pwd)"
fi

# The atomic checkout these scripts live in.  It is a *candidate* install
# source (see ensure_stack), never an implicit one: if it is not on the
# pinned ref, the pinned ref is cloned and installed instead.
ATOMIC_SRC="$(cd "$ATOMIC_DEMO_DIR/../.." > /dev/null && pwd)"

# likewise for radical.orbit -- override ORBIT_SRC to point elsewhere
: "${ORBIT_SRC:=/home/merzky/radical/radical.orbit}"

export ATOMIC_DEMO_DIR ATOMIC_SRC ORBIT_SRC

# --------------------------------------------------------------------------
# the pinned software stack
# --------------------------------------------------------------------------
#
# THIS is the demo stack.  The demo does not run "whatever happens to be
# checked out on this host" -- it runs these two refs, on the broker host,
# on every resource host, and on the laptop.  The demo date directory
# (demo/2026_09_08/) is the pin: when the refs move on, a later demo gets
# its own directory rather than this one silently changing meaning.
#
# On 2026-09-08 the broker host "three" had radical.orbit checked out on
# `devel`, the old broker.sh installed *that*, and the broker died with
# "No plugin matches 'federation'".  Hence: pins, and an install that
# ignores a checkout which is not on the pinned ref.
#
# Both refs are pushed.  Override the *_REPO variables to switch transport
# (no ssh key on the host -> use https):
#
#   ATOMIC_DEMO_ORBIT_REPO=https://github.com/radical-cybertools/radical.orbit.git
#   ATOMIC_DEMO_ATOMIC_REPO=https://github.com/radical-collaboration/ATOMIC.git
#
# A leading `git+` (pip's spelling) is accepted and stripped for git.

: "${ATOMIC_DEMO_ORBIT_REPO:=git+ssh://git@github.com/radical-cybertools/radical.orbit.git}"
: "${ATOMIC_DEMO_ORBIT_REF:=feature/atomic-federation}"

: "${ATOMIC_DEMO_ATOMIC_REPO:=git+ssh://git@github.com/radical-collaboration/ATOMIC.git}"
: "${ATOMIC_DEMO_ATOMIC_REF:=feature/demo-wm}"

# Pinned PyPI requirements installed on top of the two git refs.
#
# radical.orbit's requirements.txt asks for a bare `rhapsody-py`, so a
# fresh venv gets the newest release (0.5.0) -- and rhapsody does not
# declare its opentelemetry dependency at all.  Either way a pilot's
# session init then dies with "No module named 'opentelemetry'" and every
# task FAILS; the laptop venv only worked because something else had
# pulled opentelemetry in.  So both are pinned to what this demo was
# validated with.  Found on 2026-09-08, building a venv from scratch.
: "${ATOMIC_DEMO_PIP_PINS:=rhapsody-py==0.4.0 opentelemetry-sdk==1.43.0}"

# The demo's *home* on an HPC site: where its venv and clones live.  HPC
# home directories are small and quota'd (Perlmutter: venv creation died
# with "Disk quota exceeded"), so the sites use scratch, as the xGFabric
# demo did.  Decided here, before the resource block, because the venv
# and clone defaults below derive from it.  Empty = laptop/r3 defaults.
case "${1:-${ATOMIC_DEMO_RESOURCE:-local}}" in
    perlmutter) : "${ATOMIC_DEMO_HOME:=${SCRATCH:-${PSCRATCH:-$HOME}}/demo}" ;;
    odo)        : "${ATOMIC_DEMO_HOME:=$HOME/tmp/demo}"
                # OLCF compute nodes reach the internet only through the
                # ORNL HTTP proxy (below): ssh to GitHub hangs, https works
                case "$ATOMIC_DEMO_ORBIT_REPO" in
                    git+ssh://*|ssh://*) ATOMIC_DEMO_ORBIT_REPO='https://github.com/radical-cybertools/radical.orbit.git' ;;
                esac
                case "$ATOMIC_DEMO_ATOMIC_REPO" in
                    git+ssh://*|ssh://*) ATOMIC_DEMO_ATOMIC_REPO='https://github.com/radical-collaboration/ATOMIC.git' ;;
                esac
                ;;
    *)          : "${ATOMIC_DEMO_HOME:=}"                                    ;;
esac

# where ensure_stack keeps its own clones of the two pinned refs.  They
# are a *cache*: cloned once, `git pull --ff-only` on every run, and
# re-installed only when HEAD moved.  `down.sh --wipe` keeps them.
: "${ATOMIC_DEMO_SRC:=${ATOMIC_DEMO_HOME:-${ATOMIC_DEMO_TMP:-/tmp/atomic-demo}}/src}"

# 1 = ignore a local checkout even when it is clean and on the pinned ref,
# and always install from the clone (what a fresh host does anyway)
: "${ATOMIC_DEMO_FORCE_CLONE:=0}"

# interpreter used to *create* the venv (>= 3.10); ignored once it exists
: "${ATOMIC_DEMO_PYTHON:=python3}"

# environment module providing that python, loaded per resource below
# (perlmutter: NERSC's python module; odo: cray-python); empty = none
: "${ATOMIC_DEMO_PYTHON_MODULE:=}"

export ATOMIC_DEMO_ORBIT_REPO  ATOMIC_DEMO_ORBIT_REF
export ATOMIC_DEMO_ATOMIC_REPO ATOMIC_DEMO_ATOMIC_REF
export ATOMIC_DEMO_HOME ATOMIC_DEMO_SRC ATOMIC_DEMO_FORCE_CLONE
export ATOMIC_DEMO_PYTHON
export ATOMIC_DEMO_PYTHON_MODULE
export ATOMIC_DEMO_PIP_PINS

# --------------------------------------------------------------------------
# the venv -- the one place the pinned stack is installed
# --------------------------------------------------------------------------
#
# Default: the orbit venv when this host has one (the laptop), a venv of
# the demo's own otherwise (any other host).  ensure_stack creates it if
# it is missing.  $VE is the older name of the same thing and still works.

if [ -n "${VE:-}" ]; then
    : "${ATOMIC_DEMO_VE:=$VE}"
fi

if [ -z "${ATOMIC_DEMO_VE:-}" ]; then
    if   [ -n "$ATOMIC_DEMO_HOME" ]; then ATOMIC_DEMO_VE="$ATOMIC_DEMO_HOME/ve"
    elif [ -d "$ORBIT_SRC/ve3" ];    then ATOMIC_DEMO_VE="$ORBIT_SRC/ve3"
    else                                  ATOMIC_DEMO_VE="$HOME/.atomic-demo/ve"
    fi
fi

VE="$ATOMIC_DEMO_VE"

export ATOMIC_DEMO_VE VE

# the record of what ensure_stack put into that venv
ATOMIC_DEMO_STAMP="$VE/atomic-demo.stamp"

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

# demo_hint MSG...  -- a "what to do next" line.  Suppressed while up.sh
# orchestrates the role scripts, because up.sh prints its own summary.
demo_hint() {
    [ "${ATOMIC_DEMO_ORCHESTRATED:-0}" = '1' ] && return 0
    demo_log "$@"
}

# demo_fail MSG [LOGFILE...] -- die, naming the logs worth reading.
# $DEMO_TOOL is the script that gives up (set at the top of each).
demo_fail() {
    local msg="$1"; shift
    local log

    printf '\n'
    demo_warn "$msg"

    for log in "$@"; do
        [ -f "$log" ] || continue
        printf '\n--- last 30 lines of %s ---\n' "$log" >&2
        tail -n 30 "$log" >&2 || true
    done

    printf '\n' >&2
    demo_die "$msg -- ${DEMO_TOOL:-the demo} gives up;" \
             "run demo/2026_09_08/down.sh before retrying"
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

# demo_drop_path VAR VALUE -- remove VALUE from a ':'-list, if present.
# Used on PYTHONPATH: a shell that still carries $ORBIT_SRC/src would
# shadow the *installed* radical.orbit with a checkout of unknown branch,
# which is precisely the failure this demo directory was pinned against.
demo_drop_path() {
    local var="$1" val="$2" cur='' out='' part=''

    eval "cur=\${$var:-}"

    [ -n "$cur" ] || return 0

    local IFS=':'
    for part in $cur; do
        [ "$part" = "$val" ] && continue
        [ -z "$part" ]       && continue
        if [ -z "$out" ]; then out="$part"
        else                   out="$out:$part"
        fi
    done
    unset IFS

    if [ -z "$out" ]; then eval "unset $var"
    else                   eval "export $var=\"\$out\""
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

# demo_broker_pid -- print the pid from the broker pidfile, but only if
# that process is still alive; non-zero (and silent) otherwise.
demo_broker_pid() {
    local pid=''

    [ -f "$ATOMIC_DEMO_BROKER_PID" ] || return 1

    pid="$(cat "$ATOMIC_DEMO_BROKER_PID" 2> /dev/null || true)"

    [ -n "$pid" ]                    || return 1
    kill -0 "$pid" 2> /dev/null      || return 1

    printf '%s\n' "$pid"
}

# demo_require_broker -- die politely unless the demo broker answers.
# join.sh and submit.sh are useless without it, and "connection refused"
# from a CLI is a worse first message than this one.
demo_require_broker() {
    case "$ATOMIC_DEMO_BROKER_HOST" in
        *'TODO('*)
            demo_die "no broker host for '$ATOMIC_DEMO_RESOURCE':" \
                     "$ATOMIC_DEMO_BROKER_HOST --" \
                     'broker.sh prints the value to export' ;;
    esac

    demo_broker_alive && return 0

    demo_die "no demo broker on $RADICAL_ORBIT_BROKER_URL --" \
             'run demo/2026_09_08/broker.sh first'
}

# demo_fed_resources -- print "name liveness" for every resource the
# federation lists (empty when the broker is unreachable).  Used to spot
# records that outlived their endpoint: the federation state persists
# across broker restarts, and a resource that was never `atomic-leave`d
# comes back with liveness 'lost' and blocks a re-join (409).
demo_fed_resources() {
    "$VE/bin/atomic-resources" --json 2> /dev/null | "$VE/bin/python" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for r in data if isinstance(data, list) else []:
    print(r.get("name") or "", r.get("liveness") or "")
' 2> /dev/null || true
}

# demo_leave_stale NAME -- if the federation lists NAME with a liveness
# other than ok, leave it (with --cancel-tasks) so a fresh join can take
# the name.  Returns 1 when NAME is listed and alive (someone else's, or
# an earlier join of ours that is still running).
demo_leave_stale() {
    local name="$1" line liveness=''

    while read -r line; do
        case "$line" in
            "$name "*) liveness="${line#* }" ;;
        esac
    done <<< "$(demo_fed_resources)"

    [ -n "$liveness" ] || return 0            # not listed: nothing to do
    [ "$liveness" != ok ] || return 1         # listed and alive

    demo_log "stale   : $name is still in the federation (liveness" \
             "$liveness) from an earlier run -- leaving it first"
    timeout "${ATOMIC_DEMO_STOP_WAIT:-30}" \
        "$VE/bin/atomic-leave" "$name" --cancel-tasks \
        >> "$RUN_DIR/leave.log" 2>&1 \
        || demo_warn "atomic-leave $name reported a problem (see $RUN_DIR/leave.log)"
    return 0
}

# demo_mkdirs -- every directory the demo writes into (idempotent).
# Called by whichever role script runs first; the manual sequence has no
# single entry point that could do it once.
demo_mkdirs() {
    local name

    mkdir -p "$RUN_DIR"                        \
             "$RADICAL_ORBIT_FEDERATION_STATE" \
             "$ATOMIC_CAMPAIGN_STATE"          \
             "$ATOMIC_STORE_ROOT"              \
             "$ATOMIC_WM_STATE"

    # an unresolved scratch base is a TODO, not a directory name
    case "$ATOMIC_DEMO_SCRATCH_BASE" in
        *'TODO('*) return 0 ;;
    esac

    for name in "${ATOMIC_DEMO_RESOURCES[@]}"; do
        mkdir -p "$ATOMIC_DEMO_SCRATCH_BASE/$name"
    done
}

# --------------------------------------------------------------------------
# which resource is this shell about?
# --------------------------------------------------------------------------
#
# `source env.sh RESOURCE`, $ATOMIC_DEMO_RESOURCE, or `local`.  The role
# scripts pass their own `--resource` (join.sh: its positional resource)
# straight through, so the name reaches here from one place only.
#
# Mode is *detected*, never configured: $SLURM_JOB_ID set means this
# shell is inside an allocation, so the endpoint IS the resource ->
# `allocation` mode, one implicit member, its pilot starts at join.
# Unset means a login node -> `login` mode, one member per pilot shape,
# pilots submitted on demand.  local_* and r3 have a fixed mode by
# nature (fake resources on a laptop / a workstation with no batch
# system); the detection drives perlmutter and odo.

ATOMIC_DEMO_KNOWN='local local_a local_b local_c r3 perlmutter odo'

# a sourced *path* is not a resource name (smoke.py sources this file)
case "${1:-}" in
    ''|*/*|*.sh) : ;;
    *)           ATOMIC_DEMO_RESOURCE="$1" ;;
esac

: "${ATOMIC_DEMO_RESOURCE:=local}"

case " $ATOMIC_DEMO_KNOWN " in
    *" $ATOMIC_DEMO_RESOURCE "*) : ;;
    *) demo_warn "unknown resource '$ATOMIC_DEMO_RESOURCE' -- known:" \
                 "$ATOMIC_DEMO_KNOWN; falling back to 'local'"
       ATOMIC_DEMO_RESOURCE='local' ;;
esac

if [ -n "${SLURM_JOB_ID:-}" ]; then
    ATOMIC_DEMO_MODE='allocation'
    ATOMIC_DEMO_MODE_WHY="inside a Slurm allocation (SLURM_JOB_ID=$SLURM_JOB_ID)"
else
    ATOMIC_DEMO_MODE='login'
    ATOMIC_DEMO_MODE_WHY='no SLURM_JOB_ID -- this looks like a login node'
fi

# demo_load_python_module -- `module load $ATOMIC_DEMO_PYTHON_MODULE` on
# hosts whose system python is too old for the stack.  Idempotent, so it
# runs on every source: the venv's python is a symlink into the module
# tree, which must be on the path on reruns too.  Non-interactive shells
# have no `module` function until lmod's init is sourced.
demo_load_python_module() {
    local mod="${ATOMIC_DEMO_PYTHON_MODULE:-}" init

    [ -n "$mod" ] || return 0

    if ! type module > /dev/null 2>&1; then
        for init in /usr/share/lmod/lmod/init/bash \
                    /opt/cray/pe/lmod/lmod/init/bash \
                    /etc/profile.d/lmod.sh \
                    /usr/share/Modules/init/bash; do
            [ -r "$init" ] || continue
            # shellcheck disable=SC1090
            . "$init" && break
        done
    fi

    if ! type module > /dev/null 2>&1; then
        demo_warn "no 'module' command here -- cannot load $mod;" \
                  'set $ATOMIC_DEMO_PYTHON to a python >= 3.10 instead'
        return 0
    fi

    if module load "$mod" > /dev/null 2>&1; then
        demo_log "module  : loaded $mod ($(python3 -V 2>&1))"
    else
        demo_warn "module load $mod failed -- set \$ATOMIC_DEMO_PYTHON_MODULE" \
                  'or $ATOMIC_DEMO_PYTHON'
    fi
}

# The demo's broker lives on radical.3, the RADICAL lab server -- so that
# is the broker every resource dials unless $ATOMIC_DEMO_BROKER_HOST says
# otherwise, the laptop's fake resources included (they can join the
# real broker like any other resource).  A broker started on the laptop
# itself binds loopback and broker.sh switches this to 127.0.0.1 for its
# own run; up.sh does the same for the all-local cycle.
ATOMIC_DEMO_BROKER_DEFAULT_HOST='95.217.193.116'
: "${ATOMIC_DEMO_BROKER_HOST:=$ATOMIC_DEMO_BROKER_DEFAULT_HOST}"

case "$ATOMIC_DEMO_RESOURCE" in

    # the laptop: three fake resources against a loopback broker.  Their
    # modes are baked into demo_join_args (local_a allocation, local_b and
    # local_c login) -- the detection above does not apply to them.
    local|local_a|local_b|local_c)
        ATOMIC_DEMO_HOST='laptop'
        ATOMIC_DEMO_SITE='Rutgers'
        ATOMIC_DEMO_RESOURCES=(local_a local_b local_c)
        ATOMIC_DEMO_PILOT_RESOURCES=(local_a)
        # a broker started here listens on loopback only
        : "${ATOMIC_DEMO_BROKER_BIND:=127.0.0.1}"
        : "${ATOMIC_DEMO_SCRATCH_BASE:=${ATOMIC_DEMO_TMP:-/tmp/atomic-demo}}"
        ;;

    # the Rutgers workstation.  It hosts the broker of the distributed
    # run, and joins itself as a resource -- a workstation with no batch
    # system, so allocation mode regardless of what was detected.
    r3) ATOMIC_DEMO_HOST='r3'
        ATOMIC_DEMO_SITE='Rutgers'
        ATOMIC_DEMO_MODE='allocation'
        ATOMIC_DEMO_MODE_WHY='r3 is a workstation -- the endpoint is the resource'
        ATOMIC_DEMO_RESOURCES=(r3)
        ATOMIC_DEMO_PILOT_RESOURCES=(r3)
        # this IS the broker host: bind every interface ('r3' is the
        # laptop's ssh alias and does not resolve here, hence no name)
        : "${ATOMIC_DEMO_BROKER_BIND:=0.0.0.0}"
        : "${ATOMIC_DEMO_SCRATCH_BASE:=${ATOMIC_DEMO_TMP:-/tmp/atomic-demo}}"
        ;;

    perlmutter)
        ATOMIC_DEMO_HOST='perlmutter'
        ATOMIC_DEMO_SITE='NERSC'
        ATOMIC_DEMO_RESOURCES=(perlmutter)
        # the system python3 is 3.6; NERSC's python module is a self-
        # contained conda python (verified 2026-09-08: 3.12-26.1.0 exists)
        : "${ATOMIC_DEMO_PYTHON_MODULE:=python/3.12-26.1.0}"
        demo_load_python_module
        if   [ -n "${PSCRATCH:-}" ]; then
            : "${ATOMIC_DEMO_SCRATCH_BASE:=$PSCRATCH/atomic-demo}"
        elif [ -n "${SCRATCH:-}" ]; then
            : "${ATOMIC_DEMO_SCRATCH_BASE:=$SCRATCH/atomic-demo}"
        else
            : "${ATOMIC_DEMO_SCRATCH_BASE:=TODO(neither \$PSCRATCH nor \$SCRATCH is set -- give the Perlmutter scratch path)}"
        fi
        ;;

    odo) ATOMIC_DEMO_HOST='odo'
        ATOMIC_DEMO_SITE='OLCF'
        ATOMIC_DEMO_RESOURCES=(odo)
        # OLCF Cray systems ship cray-python (>= 3.9; the current one is
        # 3.11) -- not verified on Odo yet, override if it is not enough
        : "${ATOMIC_DEMO_PYTHON_MODULE:=cray-python}"
        demo_load_python_module
        # $HOME/tmp for now (2026-09-08): $MEMBERWORK is not set in the
        # allocation shell, and the Odo home has room.  Project: fus183.
        : "${ATOMIC_DEMO_SCRATCH_BASE:=$HOME/tmp/atomic-demo}"
        # OLCF compute nodes have no direct route out; everything (git,
        # pip, the endpoint's dial to the broker -- websockets >= 14 reads
        # https_proxy) goes through the ORNL proxy.  Values from OLCF's
        # docs (software/analytics/jax.rst, 2026-09-08).  Set
        # ATOMIC_DEMO_NO_PROXY=1 to leave the environment alone.
        if [ "${ATOMIC_DEMO_NO_PROXY:-0}" != 1 ]; then
            export all_proxy='socks://proxy.ccs.ornl.gov:3128/'
            export ftp_proxy='ftp://proxy.ccs.ornl.gov:3128/'
            export http_proxy='http://proxy.ccs.ornl.gov:3128/'
            export https_proxy='http://proxy.ccs.ornl.gov:3128/'
            export no_proxy='localhost,127.0.0.0/8,*.ccs.ornl.gov'
        fi
        ;;
esac

# Resources that must show a live pilot before the smoke test starts.
# An allocation-mode join starts its pilot right away; a login-mode
# member has min_pilots=0 and only starts one when the first task
# arrives, so it is never waited for.  The laptop and r3 set this
# themselves above.
if [ -z "${ATOMIC_DEMO_PILOT_RESOURCES+set}" ]; then
    if [ "$ATOMIC_DEMO_MODE" = 'allocation' ]; then
        ATOMIC_DEMO_PILOT_RESOURCES=("${ATOMIC_DEMO_RESOURCES[@]}")
    else
        ATOMIC_DEMO_PILOT_RESOURCES=()
    fi
fi

export ATOMIC_DEMO_RESOURCE ATOMIC_DEMO_HOST ATOMIC_DEMO_SITE
export ATOMIC_DEMO_MODE ATOMIC_DEMO_SCRATCH_BASE

# say it out loud away from the laptop, where it is the whole question
case "$ATOMIC_DEMO_HOST" in
    laptop) : ;;
    *)      demo_log "resource: $ATOMIC_DEMO_RESOURCE" \
                     "(host $ATOMIC_DEMO_HOST, site $ATOMIC_DEMO_SITE)"
            demo_log "mode    : $ATOMIC_DEMO_MODE --" \
                     "$ATOMIC_DEMO_MODE_WHY"
            demo_log "scratch : $ATOMIC_DEMO_SCRATCH_BASE" ;;
esac

# --------------------------------------------------------------------------
# interpreter / path
# --------------------------------------------------------------------------

demo_prepend_path PATH "$VE/bin"

demo_drop_path PYTHONPATH "$ORBIT_SRC/src"
demo_drop_path PYTHONPATH "$ATOMIC_SRC/src"

# --------------------------------------------------------------------------
# broker
# --------------------------------------------------------------------------

# Two different things:
#   ATOMIC_DEMO_BROKER_BIND  the address the broker LISTENS on (broker.sh
#                            only): 127.0.0.1 on the laptop, 0.0.0.0 on r3
#   ATOMIC_DEMO_BROKER_HOST  the name clients and endpoints DIAL: radical.3
#                            everywhere unless exported (see above)
# Pilots do not use either: the broker hands them its own advertised
# URL, which for a wildcard bind is its FQDN (or outbound IP).
# A non-default port: the user's own brokers live on 8000/8003.
: "${ATOMIC_DEMO_BROKER_PORT:=8010}"
: "${ATOMIC_DEMO_BROKER_BIND:=$ATOMIC_DEMO_BROKER_HOST}"

export ATOMIC_DEMO_BROKER_BIND ATOMIC_DEMO_BROKER_HOST ATOMIC_DEMO_BROKER_PORT
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
ATOMIC_DEMO_INSTALL_LOG="$RUN_DIR/install.log"
ATOMIC_DEMO_SMOKE_LOG="$RUN_DIR/smoke.log"

# pointer file written by up.sh, read by down.sh
ATOMIC_DEMO_STATE_BAK="$RUN_DIR/state.bak.latest"

# The dispatcher's state root is a module constant -- the plugin has no
# env override, so up.sh backs this directory up and clears it.  The
# variable itself is overridable so the harness can be exercised without
# touching a live dispatcher's state.
: "${ATOMIC_DEMO_DISPATCHER_STATE:=$HOME/.radical/orbit/task_dispatcher/state}"

# --------------------------------------------------------------------------
# ensure_stack -- put the pinned stack into $VE, on any host
# --------------------------------------------------------------------------
#
# Called by broker.sh, join.sh and submit.sh (and, through broker.sh, by
# up.sh).  All four take `--skip-install`; the three role scripts also
# take `--reinstall`.
#
# Per package, in order:
#
#   1. a local checkout ($ORBIT_SRC for radical.orbit, the checkout these
#      scripts live in for atomic-wm) is used only if it is on the pinned
#      ref AND has no modified tracked files AND ATOMIC_DEMO_FORCE_CLONE
#      is not 1.  It is never fetched, checked out, stashed or otherwise
#      touched -- it is the developer's working tree, not ours,
#   2. otherwise the pinned ref is cloned into $ATOMIC_DEMO_SRC/<repo>
#      (once) and `git pull --ff-only`ed (every run).  A pull that fails
#      -- no network on a login node -- is a warning, not an error: the
#      clone on disk is still a pinned stack,
#   3. the resolved commit is compared against $VE/atomic-demo.stamp and
#      pip runs only if it moved (or the venv lacks the package, or
#      --reinstall).  That is the fast path: an unchanged stack costs one
#      `git pull` and one `git rev-parse` per package.
#
# Everything goes into $RUN_DIR/install.log.

# demo_git_url URL -- pip's `git+<url>` spelling turned into a git URL
demo_git_url() {
    printf '%s\n' "${1#git+}"
}

# demo_stamp_get PKG FIELD -- the recorded value, empty when unknown
demo_stamp_get() {
    local pkg="${1//./\\.}" field="$2"

    [ -f "$ATOMIC_DEMO_STAMP" ] || return 0

    sed -n "s/^$pkg $field=\\(.*\\)\$/\\1/p" "$ATOMIC_DEMO_STAMP" | tail -n 1
}

# demo_stamp_put PKG REF COMMIT SOURCE -- record what was just installed
demo_stamp_put() {
    local pkg="$1" ref="$2" commit="$3" src="$4"
    local re="${pkg//./\\.}" tmp="$ATOMIC_DEMO_STAMP.$$"

    if [ -f "$ATOMIC_DEMO_STAMP" ]; then
        grep -v "^$re " "$ATOMIC_DEMO_STAMP" > "$tmp" 2> /dev/null || true
    else
        : > "$tmp"
    fi

    printf '%s ref=%s\n%s commit=%s\n%s source=%s\n%s stamped=%s\n' \
           "$pkg" "$ref"    "$pkg" "$commit" \
           "$pkg" "$src"    "$pkg" "$(date '+%Y-%m-%dT%H:%M:%S')" >> "$tmp"

    mv "$tmp" "$ATOMIC_DEMO_STAMP"
}

# demo_pkg_installed PKG -- 0 if $VE holds the package's console entry point
demo_pkg_installed() {
    case "$1" in
        radical.orbit) [ -f "$VE/bin/radical-orbit-broker.py" ] ;;
        atomic-wm)     [ -x "$VE/bin/atomic-join" ]             ;;
        *)             return 1                                 ;;
    esac
}

# demo_ensure_venv -- create $VE if it is missing (python >= 3.10)
demo_ensure_venv() {
    local python="$ATOMIC_DEMO_PYTHON" have=''

    [ -x "$VE/bin/python" ] && return 0

    command -v "$python" > /dev/null 2>&1 \
        || demo_die "no python found ('$python') -- set \$ATOMIC_DEMO_PYTHON"

    if ! "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
    then
        have="$("$python" -V 2>&1 | cut -d' ' -f2)"
        demo_die "$python is ${have:-too old}, but the demo stack needs" \
                 'python >= 3.10 -- load a newer python module or set' \
                 '$ATOMIC_DEMO_PYTHON'
    fi

    demo_log "venv    : creating $VE ($("$python" -V 2>&1))"

    "$python" -m venv "$VE" \
        || demo_die "could not create $VE -- is python3-venv installed?"

    "$VE/bin/python" -m pip install --quiet --upgrade pip setuptools wheel \
        >> "$ATOMIC_DEMO_INSTALL_LOG" 2>&1 \
        || demo_warn 'could not upgrade pip/setuptools/wheel -- continuing'
}

# demo_checkout_usable PATH REF -- 0 if PATH is a git checkout sitting on
# REF with no modified tracked files.  Reads only; sets DEMO_CHECKOUT_WHY
# to the reason when the answer is no.
demo_checkout_usable() {
    local path="$1" ref="$2" branch=''

    DEMO_CHECKOUT_WHY=''

    if [ "${ATOMIC_DEMO_FORCE_CLONE:-0}" = '1' ]; then
        DEMO_CHECKOUT_WHY='ATOMIC_DEMO_FORCE_CLONE=1'
        return 1
    fi

    if [ -z "$path" ] || ! git -C "$path" rev-parse --git-dir > /dev/null 2>&1
    then
        DEMO_CHECKOUT_WHY="no git checkout at ${path:-<unset>}"
        return 1
    fi

    branch="$(git -C "$path" rev-parse --abbrev-ref HEAD 2> /dev/null || true)"

    if [ "$branch" != "$ref" ]; then
        DEMO_CHECKOUT_WHY="$path is on '${branch:-?}', the demo pins '$ref'"
        return 1
    fi

    if [ -n "$(git -C "$path" status --porcelain --untracked-files=no \
               2> /dev/null)" ]; then
        DEMO_CHECKOUT_WHY="$path has uncommitted changes to tracked files"
        return 1
    fi

    return 0
}

# demo_clone DIR REPO REF -- make DIR a clone of REPO on REF, up to date.
# Clones once, pulls afterwards; a failing pull is survivable, a failing
# first clone is not.
demo_clone() {
    local dir="$1" repo="$2" ref="$3" url='' branch=''

    url="$(demo_git_url "$repo")"

    # a clone left over from a different pin is not this pin's clone
    if git -C "$dir" rev-parse --git-dir > /dev/null 2>&1; then

        branch="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2> /dev/null || true)"

        if [ "$branch" != "$ref" ]; then
            demo_log "clone   : $dir is on '${branch:-?}' -- re-cloning '$ref'"
            rm -rf "$dir"
        fi
    fi

    if ! git -C "$dir" rev-parse --git-dir > /dev/null 2>&1; then

        rm -rf "$dir"
        mkdir -p "$(dirname "$dir")"

        demo_log "clone   : $url @ $ref -> $dir"

        git clone --quiet --branch "$ref" "$url" "$dir" \
            >> "$ATOMIC_DEMO_INSTALL_LOG" 2>&1 \
            || demo_die "could not clone $url @ $ref into $dir" \
                        "(see $ATOMIC_DEMO_INSTALL_LOG) -- no ssh key on this" \
                        'host?  Set the *_REPO variable to an https URL,' \
                        'see demo/2026_09_08/env.sh'
        return 0
    fi

    if [ -n "$(git -C "$dir" status --porcelain --untracked-files=no \
               2> /dev/null)" ]; then
        demo_warn "$dir has local modifications -- not pulling"
        return 0
    fi

    if git -C "$dir" pull --ff-only --quiet \
           >> "$ATOMIC_DEMO_INSTALL_LOG" 2>&1; then
        demo_log "pull    : $dir up to date with $ref"
        return 0
    fi

    demo_warn "git pull in $dir failed (network? diverged?) -- continuing" \
              "with the commit already checked out there"
}

# demo_ensure_component PKG REPO REF CHECKOUT EXTRA CLONE_DIR
demo_ensure_component() {
    local pkg="$1" repo="$2" ref="$3" checkout="$4" extra="$5" clone="$6"
    local src='' kind='' commit='' short=''

    if demo_checkout_usable "$checkout" "$ref"; then
        src="$checkout"
        kind='checkout'
    else
        demo_log "source  : $pkg -- $DEMO_CHECKOUT_WHY"
        demo_clone "$clone" "$repo" "$ref"
        src="$clone"
        kind='clone'
    fi

    commit="$(git -C "$src" rev-parse HEAD 2> /dev/null || true)"

    [ -n "$commit" ] || demo_die "cannot read HEAD of $src"

    short="${commit:0:12}"

    DEMO_STACK_COMMITS="$DEMO_STACK_COMMITS$pkg $ref $short $kind $src"$'\n'

    if [ "${ATOMIC_DEMO_REINSTALL:-0}" != '1' ]                 \
       && [ "$(demo_stamp_get "$pkg" ref)"    = "$ref" ]         \
       && [ "$(demo_stamp_get "$pkg" commit)" = "$commit" ]      \
       && [ "$(demo_stamp_get "$pkg" source)" = "$src" ]         \
       && demo_pkg_installed "$pkg"; then

        demo_log "install : $pkg up to date ($ref $short, $kind)"
        return 0
    fi

    demo_log "install : $pkg from $kind $src ($ref $short)"

    "$VE/bin/python" -m pip install --quiet --no-input "$src$extra" \
        >> "$ATOMIC_DEMO_INSTALL_LOG" 2>&1 \
        || demo_fail "pip install $pkg from $src$extra failed" \
                     "$ATOMIC_DEMO_INSTALL_LOG"

    demo_stamp_put "$pkg" "$ref" "$commit" "$src"
}

# demo_check_console_scripts -- the entry points the demo launches by name
demo_check_console_scripts() {
    local tool missing=''

    for tool in atomic-join atomic-leave atomic-resources atomic-campaign \
                atomic-fake-md atomic-fake-train; do
        [ -x "$VE/bin/$tool" ] || missing="$missing $tool"
    done

    for tool in radical-orbit-broker.py radical-orbit-endpoint.py \
                radical-orbit-endpoint-wrapper.sh; do
        [ -f "$VE/bin/$tool" ] || missing="$missing $tool"
    done

    [ -z "$missing" ] \
        || demo_fail "entry points missing after install:$missing" \
                     "$ATOMIC_DEMO_INSTALL_LOG"
}

# ensure_stack -- the whole of the above, for both packages
ensure_stack() {

    if [ "${ATOMIC_DEMO_SKIP_INSTALL:-0}" = '1' ]; then
        demo_log 'install : skipped (--skip-install)'
        return 0
    fi

    DEMO_STACK_COMMITS=''

    mkdir -p "$RUN_DIR"
    printf '\n===== %s =====\n' "$(date '+%F %T')" \
        >> "$ATOMIC_DEMO_INSTALL_LOG"

    demo_ensure_venv

    demo_ensure_component 'radical.orbit'                   \
                          "$ATOMIC_DEMO_ORBIT_REPO"         \
                          "$ATOMIC_DEMO_ORBIT_REF"          \
                          "$ORBIT_SRC" ''                   \
                          "$ATOMIC_DEMO_SRC/radical.orbit"

    demo_ensure_component 'atomic-wm'                       \
                          "$ATOMIC_DEMO_ATOMIC_REPO"        \
                          "$ATOMIC_DEMO_ATOMIC_REF"         \
                          "$ATOMIC_SRC" '[cli]'             \
                          "$ATOMIC_DEMO_SRC/ATOMIC"

    demo_ensure_pip_pins
    demo_check_console_scripts
}

# demo_ensure_pip_pins -- the PyPI versions the demo was validated with.
# Stamped like a package, so this is a no-op on the second run.
demo_ensure_pip_pins() {
    local pins="${ATOMIC_DEMO_PIP_PINS:-}"

    [ -n "$pins" ] || return 0

    if [ "${ATOMIC_DEMO_REINSTALL:-0}" != '1' ] \
       && [ "$(demo_stamp_get 'pip-pins' ref)" = "$pins" ]; then
        demo_log "install : pip pins up to date ($pins)"
        return 0
    fi

    demo_log "install : pip pins $pins"

    # shellcheck disable=SC2086
    "$VE/bin/python" -m pip install --quiet --no-input $pins \
        >> "$ATOMIC_DEMO_INSTALL_LOG" 2>&1 \
        || demo_fail "pip install of the demo pins ($pins) failed" \
                     "$ATOMIC_DEMO_INSTALL_LOG"

    demo_stamp_put 'pip-pins' "$pins" '-' '-'
}

# demo_report_stack -- what ensure_stack resolved, one line per package
demo_report_stack() {
    local pkg ref short kind src

    [ -n "${DEMO_STACK_COMMITS:-}" ] || return 0

    while read -r pkg ref short kind src; do
        [ -n "$pkg" ] || continue
        demo_log "stack   : $pkg $ref @ $short ($kind $src)"
    done <<< "$DEMO_STACK_COMMITS"
}

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
# arrangement.
#
# Why the smoke test may insist on TWO GPU members: each of them declares
# ONE core and max_pilots=1, so a member runs one train task at a time and
# the three sweep points cannot all run concurrently on one.  What makes
# the second member actually get work is timing, not capacity alone: the
# dispatcher grows the second member's pilot while the first is busy, so
# that pilot must become active within 2 x the train stage's duration of
# the first one.  `train` therefore runs with `--duration-sec 30`, which
# leaves ~60 s of margin against a warm-up measured at 5-15 s locally.
#
# The "GPU" is fake (psij `local`, rhapsody backend `concurrent`): only
# the *declaration* matters for routing, and nothing reserves a GPU.

# ATOMIC_DEMO_RESOURCES is set per resource name in "which resource is
# this shell about?" above -- the laptop's three, or the one real
# resource this host is.  join.sh accepts exactly those names.

# demo_join_args NAME -- fills the array DEMO_JOIN_ARGS with the
# `atomic-join` arguments for one resource (--detach is added by
# join.sh, which is the only caller).
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
                     --scratch    "$ATOMIC_DEMO_SCRATCH_BASE/local_a") ;;

        local_b) DEMO_JOIN_ARGS=(
                     --name       local_b
                     --mode       login
                     --site       NERSC
                     --kind       hpc
                     --member     'cpu:queue=local,account=demo,nodes=1,cpus=2,walltime=1800,node_hours=2,software=lammps,pytorch,site=NERSC,mem_gb_per_node=16'
                     --member     'gpu:queue=local,account=demo,nodes=1,cpus=1,gpus=1,walltime=1800,node_hours=1,max_pilots=1,software=pytorch,site=NERSC,mem_gb_per_node=16'
                     --scratch    "$ATOMIC_DEMO_SCRATCH_BASE/local_b") ;;

        local_c) DEMO_JOIN_ARGS=(
                     --name       local_c
                     --mode       login
                     --site       PSC
                     --kind       hpc
                     --member     'cpu:queue=local,account=demo,nodes=1,cpus=2,walltime=1800,node_hours=2,software=pytorch,site=PSC,mem_gb_per_node=16'
                     --member     'gpu:queue=local,account=demo,nodes=1,cpus=1,gpus=1,walltime=1800,node_hours=1,max_pilots=1,software=pytorch,site=PSC,mem_gb_per_node=16'
                     --scratch    "$ATOMIC_DEMO_SCRATCH_BASE/local_c") ;;

        # ------------------------------------------------------------------
        # The REAL demo resources.  Selected by `source env.sh <name>`,
        # i.e. by `join.sh r3|perlmutter|odo` -- so a laptop shell never
        # sees them and `join.sh perlmutter` on the laptop is rejected by
        # demo_known_resource before it can confuse anybody.
        #
        # `$ATOMIC_DEMO_MODE` is DETECTED, not configured: inside a Slurm
        # allocation ($SLURM_JOB_ID set) the endpoint IS the resource ->
        # allocation mode; on a login node -> login mode with declared
        # members.  r3 is a workstation and is always allocation mode.
        #
        # Anything still spelled TODO(...) makes join.sh refuse, loudly,
        # before it talks to the broker.
        # ------------------------------------------------------------------

        # r3 -- the Rutgers workstation, and the broker host itself.  No
        # placeholders: it joins in seconds, which makes it the resource to
        # join live, on camera.
        r3)      DEMO_JOIN_ARGS=(
                     --name       r3
                     --mode       allocation
                     --site       Rutgers
                     --kind       workstation
                     --software   lammps,pytorch
                     --scratch    "$ATOMIC_DEMO_SCRATCH_BASE/r3") ;;

        # perlmutter (NERSC) and odo (OLCF).  Two shapes, one per detected
        # mode.  The allocation shape is the recommended one and has no
        # placeholders at all once $PSCRATCH / $MEMBERWORK are set: run
        # `salloc` (or `srun --pty`), then `join.sh perlmutter` inside it.
        # Capabilities are detected there (`sysinfo`, `queue_info` /
        # `job_allocation` for size and remaining walltime).
        perlmutter|odo)

            if [ "$ATOMIC_DEMO_MODE" = 'allocation' ]; then

                # shared_fs=false: neither site shares the broker's (r3's)
                # filesystem, so --scratch is a path HERE and inputs are
                # staged through the pilot.  Odo also needs gpus=8 declared:
                # a node has 4 MI250X = 8 GCDs, and the AMD GPUs are
                # invisible to the nvidia-smi based detection (which reports
                # cores=128, gpus=0).
                local decl='shared_fs=false'
                if [ "$name" = 'odo' ]; then
                    decl='gpus=8,shared_fs=false'
                fi

                DEMO_JOIN_ARGS=(
                     --name       "$name"
                     --mode       allocation
                     --site       "$ATOMIC_DEMO_SITE"
                     --kind       hpc
                     --declare    "$decl"
                     --software   lammps,pytorch
                     --scratch    "$ATOMIC_DEMO_SCRATCH_BASE/$name")

            else

                # Login mode: pilots are submitted on demand, so queue,
                # account, pilot size and budget have to be declared -- one
                # --member per pilot shape, and every remote member carries
                # `shared_fs=false` plus an explicit `scratch_base=`.
                #
                # NOTE this only works from a broker host that can reach
                # this site's batch system: psij detects the executor on the
                # BROKER host, and r3 has no Slurm.  Hence the TODOs and
                # hence the recommendation above -- start an allocation.
                local q_cpu='TODO(CPU queue/partition)'
                local q_gpu='TODO(GPU queue/partition)'
                local acct='TODO(allocation/project id)'
                local cpus='TODO(cpus per node)'
                local gpus='TODO(gpus per node)'
                local wall='TODO(pilot walltime, seconds)'
                local nh_cpu='TODO(cpu member budget)'
                local nh_gpu='TODO(gpu member budget)'
                local maxp='TODO(max concurrent gpu pilots)'
                if [ "$name" = odo ]; then
                    # Odo (OLCF, 2026-09-08): `interact` partition, project
                    # fus183; a node is one 64-core EPYC + 4 MI250X = 8
                    # GCDs (Odo user guide).  30 min pilots, 2 node-hours
                    # per member, one GPU pilot at a time.
                    q_cpu='interact'; q_gpu='interact'; acct='fus183'
                    cpus=64; gpus=8; wall=1800; nh_cpu=2; nh_gpu=2; maxp=1
                fi
                local base="$ATOMIC_DEMO_SCRATCH_BASE/$name"

                DEMO_JOIN_ARGS=(
                     --name       "$name"
                     --mode       login
                     --site       "$ATOMIC_DEMO_SITE"
                     --kind       hpc
                     --member     "cpu:queue=$q_cpu,account=$acct,nodes=1,cpus=$cpus,walltime=$wall,node_hours=$nh_cpu,software=lammps,pytorch,site=$ATOMIC_DEMO_SITE,shared_fs=false,scratch_base=$base"
                     --member     "gpu:queue=$q_gpu,account=$acct,nodes=1,cpus=$cpus,gpus=$gpus,walltime=$wall,node_hours=$nh_gpu,max_pilots=$maxp,software=pytorch,site=$ATOMIC_DEMO_SITE,shared_fs=false,scratch_base=$base"
                     --scratch    "$base")
            fi ;;

        # ------------------------------------------------------------------
        # Allocation mode DOES declare `shared_fs=false`: it travels as a
        # top-level field of the join record (`--declare shared_fs=false`,
        # set above for perlmutter and odo), and the implicit member the
        # broker builds inherits it -- so the resource's `--scratch` is
        # read as a path on ITS host and inputs are staged there.
        #
        # In login mode the same statement is made per --member
        # (`shared_fs=false,scratch_base=…`, see above).  Without it the
        # broker would stage inputs and mkdir a cwd on its *own* host and
        # the task would run remotely with a bogus cwd and no inputs,
        # silently.
        # ------------------------------------------------------------------

        *)       demo_warn "no join arguments known for '$name'"
                 DEMO_JOIN_ARGS=()
                 return 1 ;;
    esac
}

# demo_join_args_todo -- 0 if DEMO_JOIN_ARGS still carries a placeholder,
# and then $DEMO_JOIN_TODO names the first one.  A resource template that
# was uncommented but not filled in must fail *here*, not four minutes
# later inside a batch job.
demo_join_args_todo() {
    local arg

    DEMO_JOIN_TODO=''

    for arg in "${DEMO_JOIN_ARGS[@]}"; do
        case "$arg" in
            *'TODO('*) DEMO_JOIN_TODO="$arg"; return 0 ;;
        esac
    done

    return 1
}

# demo_known_resource NAME -- 0 if NAME is one of the demo's resources
demo_known_resource() {
    local name="$1" known

    for known in "${ATOMIC_DEMO_RESOURCES[@]}"; do
        [ "$known" = "$name" ] && return 0
    done

    return 1
}

# (ATOMIC_DEMO_PILOT_RESOURCES -- the resources that must show a live
# pilot before the smoke test starts -- is set per resource name in
# "which resource is this shell about?" above, NOT here: it is `local_a`
# on the laptop, the resource itself in allocation mode, and empty in
# login mode, where a member has min_pilots=0 and only starts a pilot
# when the first task arrives.)

# --------------------------------------------------------------------------
# the campaign the client step submits
# --------------------------------------------------------------------------
#
# submit.sh's defaults; smoke.py carries the same two values of its own
# (it must stay runnable with nothing sourced).

: "${ATOMIC_DEMO_SPEC:=$ATOMIC_SRC/examples/workflow_vacancy.json}"
: "${ATOMIC_DEMO_SWEEP:=temperature=300,600,900}"

export ATOMIC_DEMO_SPEC ATOMIC_DEMO_SWEEP

# --------------------------------------------------------------------------
# timeouts (seconds) -- every wait loop in the demo scripts uses one of these
# --------------------------------------------------------------------------

: "${ATOMIC_DEMO_BROKER_WAIT:=60}"     # broker answering GET /endpoints
: "${ATOMIC_DEMO_JOIN_WAIT:=120}"      # one atomic-join --detach call
: "${ATOMIC_DEMO_RESOURCE_WAIT:=90}"   # all resources listed + pilots up
: "${ATOMIC_DEMO_CAMPAIGN_WAIT:=600}"  # submit.sh --wait budget
: "${ATOMIC_DEMO_STOP_WAIT:=15}"       # SIGTERM grace before SIGKILL
: "${ATOMIC_DEMO_POLL:=2}"             # wait-loop poll interval

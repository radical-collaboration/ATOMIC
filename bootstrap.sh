#!/bin/bash
#
# bootstrap.sh -- make a machine ready to join the ATOMIC federation, then
# join it.  One command, on a machine which has nothing but python3 (and
# git, if radical.orbit is installed from a repository):
#
#     ./bootstrap.sh ~/atomic-ve --broker https://r3:8003 --name my_box \
#                                --mode allocation --software lammps
#
# It creates the venv if it is missing, installs radical.orbit and this
# package (non-editable) if they are missing, and then execs
# `atomic-join` with the arguments you passed.  Running it again on a
# prepared venv skips straight to the join -- it is idempotent, and it
# prints every step it takes.
#
# Knobs (environment):
#
#   PYTHON         interpreter used to create the venv      (default: python3)
#   ORBIT_SPEC     pip spec for radical.orbit; a local path works too, and
#                  'none' skips the install entirely
#                  (default: git+https://github.com/radical-cybertools/
#                            radical.orbit@$ORBIT_BRANCH)
#   ORBIT_BRANCH   branch used by the default ORBIT_SPEC    (default: devel)
#
#                  NOTE: `devel` does not yet host the `federation` plugin
#                  the join talks to -- until `feature/atomic-federation`
#                  is merged, point the *broker's* installation at that
#                  branch (ORBIT_BRANCH=feature/atomic-federation).  The
#                  joining side only needs an endpoint, which devel has.
#   ATOMIC_SPEC    pip spec for this package
#                  (default: the directory this script lives in, [cli] extra)
#   ATOMIC_FORCE   set to 1 to reinstall even when things are present
#   ATOMIC_NO_EXEC set to 1 to prepare the venv but not run atomic-join
#
# The `cli` extra pulls in `requests`: the base install is deliberately
# stdlib-only so the workload tools also run under a pilot's python.

set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON="${PYTHON:-python3}"
ORBIT_BRANCH="${ORBIT_BRANCH:-devel}"
ORBIT_SPEC="${ORBIT_SPEC:-git+https://github.com/radical-cybertools/radical.orbit@${ORBIT_BRANCH}}"
ATOMIC_SPEC="${ATOMIC_SPEC:-${SELF}[cli]}"
ATOMIC_FORCE="${ATOMIC_FORCE:-0}"
ATOMIC_NO_EXEC="${ATOMIC_NO_EXEC:-0}"


say()  { echo "--> $*"; }
fail() { echo "bootstrap.sh: error: $*" >&2; exit 1; }

usage() {
    cat >&2 <<EOF
usage: bootstrap.sh <venv-dir> [atomic-join args ...]

  <venv-dir>   virtualenv to create/reuse (e.g. ~/atomic-ve)

  everything after it is passed to atomic-join, e.g.:

    bootstrap.sh ~/atomic-ve --broker https://host:8003 --name my_box \\
                 --mode allocation --declare cores=8 --software lammps

  bootstrap.sh ~/atomic-ve --help   shows atomic-join's own options
EOF
    exit 2
}


# --------------------------------------------------------------------------
# arguments
# --------------------------------------------------------------------------
test $# -ge 1 || usage

VENV="$1"; shift
case "$VENV" in
    -h|--help) usage ;;
esac

# expand a leading ~ (we may be called without a shell expansion)
VENV="${VENV/#\~/$HOME}"


# --------------------------------------------------------------------------
# 1) the venv
# --------------------------------------------------------------------------
if test -x "$VENV/bin/python"; then
    say "reusing venv: $VENV"
else
    command -v "$PYTHON" >/dev/null 2>&1 \
        || fail "no python found ($PYTHON) -- set \$PYTHON"

    # atomic-wm needs 3.10+ (and so does radical.orbit); a login node's
    # default python3 is often older, and the failure four steps later
    # is unreadable
    "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || fail "$PYTHON is $("$PYTHON" -V 2>&1 | cut -d' ' -f2), but python >= 3.10 is required -- load a newer python module or set \$PYTHON"

    say "creating venv: $VENV ($($PYTHON -V 2>&1))"
    "$PYTHON" -m venv "$VENV" \
        || fail "could not create the venv (python3-venv installed?)"
    say 'upgrading pip'
    "$VENV/bin/python" -m pip install --quiet --upgrade pip setuptools wheel
fi

PIP="$VENV/bin/python -m pip"


# --------------------------------------------------------------------------
# 2) radical.orbit -- provides the endpoint atomic-join starts
# --------------------------------------------------------------------------
have_orbit() {
    "$VENV/bin/python" -c 'import radical.orbit' >/dev/null 2>&1
}

if test "$ORBIT_SPEC" = 'none'; then
    say 'skipping the radical.orbit install (ORBIT_SPEC=none)'
elif have_orbit && test "$ATOMIC_FORCE" != '1'; then
    say "radical.orbit is present ($("$VENV/bin/python" -c \
        'import radical.orbit as o; print(getattr(o, "version", "?"))' \
        2>/dev/null || echo '?'))"
else
    say "installing radical.orbit from: $ORBIT_SPEC"
    $PIP install "$ORBIT_SPEC" || fail "could not install $ORBIT_SPEC"
fi


# --------------------------------------------------------------------------
# 3) this package (non-editable, with the CLI extra)
# --------------------------------------------------------------------------
if test -x "$VENV/bin/atomic-join" && test "$ATOMIC_FORCE" != '1'; then
    say 'atomic-wm is present'
else
    say "installing atomic-wm from: $ATOMIC_SPEC"
    $PIP install "$ATOMIC_SPEC" || fail "could not install $ATOMIC_SPEC"
fi

test -x "$VENV/bin/atomic-join" \
    || fail "atomic-join was not installed into $VENV"


# --------------------------------------------------------------------------
# 4) join
# --------------------------------------------------------------------------
if test "$ATOMIC_NO_EXEC" = '1'; then
    say "prepared: $VENV/bin/atomic-join"
    exit 0
fi

# the endpoint child needs the venv's bin first on $PATH (psij launched
# pilots resolve radical-orbit-endpoint-wrapper.sh by name); atomic-join
# enforces that for its child, we do it here for anything else the user
# runs from this shell
export PATH="$VENV/bin:$PATH"

if test $# -eq 0; then
    say 'no atomic-join arguments given -- showing its usage'
    exec "$VENV/bin/atomic-join" --help
fi

say "running: atomic-join $*"
exec "$VENV/bin/atomic-join" "$@"

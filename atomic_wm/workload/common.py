"""Shared helpers for the synthetic ATOMIC workload tools.

Stdlib only -- see ``atomic_wm.workload.__doc__``.

All tools emit the same JSON envelope::

    {"type"       : "simulation",
     "params"     : {...},          # the tool's own arguments
     "series"     : {...},          # equal-length metric lists (plottable)
     "summary"    : {...},          # scalars
     "produced_by": {"tool": ..., "version": ..., "host": ..., "ts": ...}}

The envelope is assembled in exactly one place -- :func:`make_envelope` --
so that a revision of the contract touches a single function.
"""

import argparse
import hashlib
import json
import os
import socket
import statistics
import sys
import time

from typing import Any, Dict, Iterable, List, Mapping, NoReturn
from typing import Optional, Sequence

from .. import __version__

# default wall time of a workload task, in seconds; the demo paces itself
# through these so the UI has something to show while tasks run
DEFAULT_DURATION = 5.0

# number of decimals kept in emitted series/summary values (keeps the
# JSON small and byte-comparable across runs)
PRECISION = 6


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

def die(tool: str, msg: str, code: int = 2) -> NoReturn:
    """Report a fatal input error and exit with `code` (default: 2).

    2 is what argparse itself uses for usage errors, so a caller sees one
    consistent 'your input was wrong' exit code.  Annotated ``NoReturn``
    so type checkers know the code after a ``die()`` is unreachable.
    """

    sys.stderr.write('%s: error: %s\n' % (tool, msg))
    sys.exit(code)


def require(tool: str, condition: bool, msg: str) -> None:
    """:func:`die` unless `condition` holds."""

    if not condition:
        die(tool, msg)


# ---------------------------------------------------------------------------
# argument helpers
# ---------------------------------------------------------------------------

def default_seed(*parts: Any) -> int:
    """Derive a stable RNG seed from a tool's own parameters.

    Used when no ``--seed`` was given: identical parameters then yield
    identical output (on any machine, any python -- unlike ``hash()``,
    blake2b does not depend on PYTHONHASHSEED), while *different*
    parameters yield visibly different noise.
    """

    blob = json.dumps(list(parts), sort_keys=True,
                      separators=(',', ':')).encode('utf-8')

    return int.from_bytes(hashlib.blake2b(blob, digest_size=4).digest(),
                          'big')


def add_std_args(parser: argparse.ArgumentParser,
                 duration: float = DEFAULT_DURATION) -> None:
    """Add the arguments every workload tool understands."""

    parser.add_argument('--seed', type=int, default=None,
                        help='RNG seed -- output is fully determined by '
                             'the arguments plus this seed (default: a '
                             'stable hash of the tool parameters, so '
                             'identical parameters give identical output)')
    parser.add_argument('--duration-sec', type=float, default=duration,
                        metavar='D',
                        help='pad the run to about D seconds of wall time '
                             '(default: %g)' % duration)


def check_std_args(tool: str, args: argparse.Namespace) -> None:
    """Validate the arguments added by :func:`add_std_args`."""

    require(tool, args.duration_sec >= 0.0, '--duration-sec must be >= 0')


def pace(started: float, duration: float) -> float:
    """Sleep until `duration` seconds passed since the `started` stamp.

    `started` is a :func:`time.monotonic` stamp taken at tool start.
    Returns the elapsed wall time.
    """

    remaining = duration - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)

    return time.monotonic() - started


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------

def read_input_json(tool: str, path: str) -> Dict[str, Any]:
    """Read and validate an upstream stage's JSON document.

    Any problem (missing file, unreadable, not JSON, not an object) is a
    fatal input error with a message naming the file.
    """

    if not os.path.exists(path):
        die(tool, 'input file not found: %s' % path)

    try:
        with open(path, 'r', encoding='utf-8') as fin:
            doc = json.load(fin)
    except json.JSONDecodeError as e:
        die(tool, 'input file is not valid JSON: %s (%s)' % (path, e))
    except OSError as e:
        die(tool, 'cannot read input file: %s (%s)' % (path, e))

    if not isinstance(doc, dict):
        die(tool, 'input file must contain a JSON object: %s' % path)

    return doc


def write_json_atomic(path: str, doc: Mapping[str, Any]) -> None:
    """Write `doc` to `path` atomically (temp file in the same dir + rename).

    A reader (the campaign's result collection, say) thus never observes a
    half-written output file, and a crashed run leaves no partial output.
    """

    path = os.path.abspath(path)
    base = os.path.dirname(path)

    if base:
        os.makedirs(base, exist_ok=True)

    tmp = os.path.join(base, '.%s.%d.tmp' % (os.path.basename(path),
                                             os.getpid()))
    try:
        with open(tmp, 'w', encoding='utf-8') as fout:
            json.dump(doc, fout, indent=2)
            fout.write('\n')
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(tmp, path)

    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# the envelope -- the one place which knows the output contract
# ---------------------------------------------------------------------------

def make_envelope(tool: str,
                  type_: str,
                  params: Mapping[str, Any],
                  series: Mapping[str, Sequence[float]],
                  summary: Mapping[str, Any],
                  extra: Optional[Mapping[str, Any]] = None
                  ) -> Dict[str, Any]:
    """Build the JSON envelope shared by all workload tools.

    `extra` adds top-level keys next to the envelope's own -- ``fake_md``
    uses it to mirror ``params.temperature`` as a top-level
    ``temperature`` field, as the demo contract shows it.
    """

    doc = {'type'       : type_,
           'params'     : dict(params),
           'series'     : {k: list(v) for k, v in series.items()},
           'summary'    : dict(summary),
           'produced_by': {'tool'   : tool,
                           'version': __version__,
                           'host'   : socket.gethostname(),
                           'ts'     : time.time()}}

    for key, val in (extra or {}).items():
        if key in doc:
            raise ValueError('extra key %r collides with the envelope' % key)
        doc[key] = val

    return doc


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------

def rounded(values: Iterable[float]) -> List[float]:
    """Round a series to :data:`PRECISION` decimals."""

    return [round(float(v), PRECISION) for v in values]


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean, rounded; 0.0 for an empty series."""

    if not values:
        return 0.0

    return round(statistics.fmean(values), PRECISION)


def stddev(values: Sequence[float]) -> float:
    """Population standard deviation, rounded; 0.0 for < 2 values."""

    if len(values) < 2:
        return 0.0

    return round(statistics.pstdev(values), PRECISION)


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp `value` into ``[lo, hi]``."""

    return max(lo, min(hi, value))

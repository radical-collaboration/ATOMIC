"""``atomic-fake-descriptors`` -- synthetic descriptor analysis stage.

Optional third stage type (``analysis``).  Consumes an ``atomic-fake-md``
document and emits a histogram of per-atom "descriptor" values drawn
around the simulation's mean energy.  Deterministic for a given
``--seed``.
"""

import argparse
import random
import sys
import time

from typing import Any, Dict, Optional, Sequence, Tuple

from . import common

TOOL = 'atomic-fake-descriptors'
TYPE = 'analysis'

DEFAULT_ATOMS = 256
DEFAULT_BINS  = 20


# ---------------------------------------------------------------------------
def input_stats(doc: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Extract ``(mean_energy, std_energy)`` from an upstream document.

    Taken from the MD summary when present, else computed from
    ``series.energy``.  ``None`` if neither is available.
    """

    summary = doc.get('summary')
    if isinstance(summary, dict):
        mean = summary.get('mean_energy')
        std  = summary.get('std_energy')
        if common.is_number(mean) and common.is_number(std):
            return float(mean), float(std)

    series = doc.get('series')
    if isinstance(series, dict):
        energy = series.get('energy')
        if isinstance(energy, list) and energy:
            try:
                vals = [float(v) for v in energy]
            except (TypeError, ValueError):
                return None
            return common.mean(vals), common.stddev(vals)

    return None


# ---------------------------------------------------------------------------
def histogram(values: Sequence[float], bins: int
              ) -> Dict[str, Sequence[float]]:
    """Bin `values` into `bins` equal-width bins.

    Returns ``{'bin_center': [...], 'count': [...]}``; the counts always
    sum to ``len(values)`` (the maximum falls into the last bin).
    """

    lo, hi = min(values), max(values)
    width  = (hi - lo) / bins if hi > lo else 1.0
    if hi <= lo:
        lo -= width * bins / 2.0

    count = [0] * bins
    for val in values:
        idx = int((val - lo) / width)
        count[min(max(idx, 0), bins - 1)] += 1

    center = [lo + width * (i + 0.5) for i in range(bins)]

    return {'bin_center': common.rounded(center),
            'count'     : count}


# ---------------------------------------------------------------------------
def run(mean_energy: float, std_energy: float, n_atoms: int, bins: int,
        seed: Optional[int], duration_sec: float,
        source: Optional[str] = None) -> Dict[str, Any]:
    """Produce the full output document for the given arguments.

    ``seed=None`` derives a stable seed from the parameters themselves.
    """

    if seed is None:
        seed = common.default_seed(TOOL, round(mean_energy, common.PRECISION),
                                   round(std_energy, common.PRECISION),
                                   n_atoms, bins)

    rng    = random.Random(seed)
    sigma  = max(std_energy, 1.0e-6)
    values = [rng.gauss(mean_energy, sigma) for _ in range(n_atoms)]
    series = histogram(values, bins)

    summary = {'n_atoms'         : n_atoms,
               'n_bins'          : bins,
               'mean_descriptor' : common.mean(values),
               'std_descriptor'  : common.stddev(values),
               'min_descriptor'  : round(min(values), common.PRECISION),
               'max_descriptor'  : round(max(values), common.PRECISION)}

    params  = {'n_atoms'     : n_atoms,
               'bins'        : bins,
               'mean_energy' : round(mean_energy, common.PRECISION),
               'std_energy'  : round(std_energy,  common.PRECISION),
               'seed'        : seed,
               'duration_sec': duration_sec,
               'input'       : source}

    return common.make_envelope(TOOL, TYPE, params, series, summary)


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console script entry point."""

    started = time.monotonic()

    parser = argparse.ArgumentParser(
        prog=TOOL, description='synthetic descriptor analysis stage')
    parser.add_argument('--in', dest='inp', required=True, metavar='PATH',
                        help='input JSON file from the MD stage')
    parser.add_argument('--out', required=True, metavar='PATH',
                        help='output JSON file (written atomically)')
    parser.add_argument('--n-atoms', type=int, default=DEFAULT_ATOMS,
                        metavar='N', help='number of atoms to describe '
                                          '(default: %d)' % DEFAULT_ATOMS)
    parser.add_argument('--bins', type=int, default=DEFAULT_BINS,
                        metavar='B', help='histogram bins (default: %d)'
                                          % DEFAULT_BINS)
    common.add_std_args(parser)

    args = parser.parse_args(argv)

    common.require(TOOL, args.n_atoms > 0, '--n-atoms must be > 0')
    common.require(TOOL, args.bins    > 0, '--bins must be > 0')
    common.check_std_args(TOOL, args)

    doc   = common.read_input_json(TOOL, args.inp)
    stats = input_stats(doc)

    if stats is None:
        common.die(TOOL, 'no energy data found in %s -- expected `summary.'
                         'mean_energy` or `series.energy`' % args.inp)

    out = run(stats[0], stats[1], args.n_atoms, args.bins, args.seed,
              args.duration_sec, source=args.inp)
    common.write_output(TOOL, args.out, out)

    elapsed = common.pace(started, args.duration_sec)

    sys.stdout.write('%s: atoms=%d bins=%d mean_descriptor=%.3f -> %s '
                     '(%.2fs)\n'
                     % (TOOL, args.n_atoms, args.bins,
                        out['summary']['mean_descriptor'], args.out,
                        elapsed))
    return 0


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.exit(main())

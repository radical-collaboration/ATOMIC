"""``atomic-fake-train`` -- synthetic ML training stage.

Stands in for a real trainer (PyTorch, ...).  Consumes the JSON document
of an upstream ``atomic-fake-md`` stage, emits a decaying loss curve and
a rising accuracy curve.  The accuracy plateau *decreases with the input
temperature* (0.99 @300 K, 0.93 @600 K, 0.85 @900 K) -- that is the point:
the three workflows of the demo sweep must produce visibly different
curves.  Deterministic for a given ``--seed``.
"""

import argparse
import math
import random
import sys
import time

from typing import Any, Dict, Optional, Sequence

from . import common

TOOL = 'atomic-fake-train'
TYPE = 'ml_training'

# Accuracy plateau as a function of the MD temperature: the contract
# (plans/00-overview.md) pins 0.99 @300 K, 0.93 @600 K, 0.85 @900 K, and
# requires the plateau to be *strictly* monotone in temperature.  Between
# and beyond the knots we interpolate/extrapolate linearly, clamped.
PLATEAU_KNOTS = [(300.0, 0.99), (600.0, 0.93), (900.0, 0.85)]
PLATEAU_MAX   = 0.99
PLATEAU_MIN   = 0.50


# ---------------------------------------------------------------------------
def input_temperature(doc: Dict[str, Any]) -> Optional[float]:
    """Extract the simulation temperature from an upstream document.

    Looked for in ``params.temperature``, then at the top level, then as
    the mean of ``series.temperature`` -- so a hand-written or a
    third-party upstream document works as well as our own.
    """

    for src in (doc.get('params'), doc, doc.get('summary')):
        if isinstance(src, dict):
            val = src.get('temperature')
            if common.is_number(val):
                return float(val)

    series = doc.get('series')
    if isinstance(series, dict):
        vals = series.get('temperature')
        if isinstance(vals, list) and vals:
            try:
                return float(common.mean([float(v) for v in vals]))
            except (TypeError, ValueError):
                return None

    return None


# ---------------------------------------------------------------------------
def accuracy_plateau(temperature: float) -> float:
    """The accuracy this model tops out at for the given MD temperature.

    Piecewise linear through :data:`PLATEAU_KNOTS`, extrapolated with the
    outermost slope and clamped to ``[PLATEAU_MIN, PLATEAU_MAX]``.
    """

    lo_t, lo_p = PLATEAU_KNOTS[0]
    hi_t, hi_p = PLATEAU_KNOTS[-1]

    if temperature <= lo_t:
        # colder than the first knot: extrapolate with the first slope
        nxt_t, nxt_p = PLATEAU_KNOTS[1]
        slope = (nxt_p - lo_p) / (nxt_t - lo_t)
        plateau = lo_p + (temperature - lo_t) * slope

    elif temperature >= hi_t:
        # hotter than the last knot: extrapolate with the last slope
        prv_t, prv_p = PLATEAU_KNOTS[-2]
        slope = (hi_p - prv_p) / (hi_t - prv_t)
        plateau = hi_p + (temperature - hi_t) * slope

    else:
        plateau = hi_p
        for (t0, p0), (t1, p1) in zip(PLATEAU_KNOTS, PLATEAU_KNOTS[1:]):
            if t0 <= temperature <= t1:
                plateau = p0 + (p1 - p0) * (temperature - t0) / (t1 - t0)
                break

    return common.clamp(plateau, PLATEAU_MIN, PLATEAU_MAX)


# ---------------------------------------------------------------------------
def train(temperature: float, epochs: int, seed: int
          ) -> Dict[str, Sequence[float]]:
    """Generate the ``epoch`` / ``loss`` / ``accuracy`` series."""

    rng        = random.Random(seed)
    plateau    = accuracy_plateau(temperature)
    rate       = 4.0 / epochs                    # ~98% converged at the end
    loss_floor = 0.05 + (PLATEAU_MAX - plateau) * 1.5
    loss_start = 2.50
    acc_sigma  = 0.001
    loss_sigma = 0.020

    epoch, loss, accuracy = [], [], []

    for i in range(epochs):
        decay = math.exp(-rate * (i + 1))
        acc   = plateau * (1.0 - decay) + rng.gauss(0.0, acc_sigma)
        lss   = loss_floor + (loss_start - loss_floor) * decay \
                           + rng.gauss(0.0, loss_sigma)
        epoch.append(i)
        accuracy.append(common.clamp(acc, 0.0, 1.0))
        loss.append(max(lss, 0.0))

    return {'epoch'   : epoch,
            'loss'    : common.rounded(loss),
            'accuracy': common.rounded(accuracy)}


# ---------------------------------------------------------------------------
def run(temperature: float, epochs: int, seed: Optional[int],
        duration_sec: float,
        source: Optional[str] = None) -> Dict[str, Any]:
    """Produce the full output document for the given arguments.

    ``seed=None`` derives a stable seed from the parameters themselves.
    """

    if seed is None:
        seed = common.default_seed(TOOL, temperature, epochs)

    series = train(temperature, epochs, seed)

    # average a decent tail so no single noisy epoch can flip the
    # temperature ordering of `final_accuracy`
    tail    = max(3, epochs // 5)
    final   = common.mean(series['accuracy'][-tail:])
    summary = {'final_accuracy'   : final,
               'final_loss'       : series['loss'][-1],
               'accuracy_plateau' : round(accuracy_plateau(temperature),
                                          common.PRECISION),
               'input_temperature': temperature,
               'epochs'           : epochs}

    params  = {'epochs'      : epochs,
               'temperature' : temperature,
               'seed'        : seed,
               'duration_sec': duration_sec,
               'input'       : source}

    return common.make_envelope(TOOL, TYPE, params, series, summary)


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console script entry point."""

    started = time.monotonic()

    parser = argparse.ArgumentParser(
        prog=TOOL, description='synthetic ML training stage')
    parser.add_argument('--in', dest='inp', required=True, metavar='PATH',
                        help='input JSON file from the MD stage')
    parser.add_argument('--epochs', type=int, required=True, metavar='E',
                        help='number of training epochs to emit')
    parser.add_argument('--out', required=True, metavar='PATH',
                        help='output JSON file (written atomically)')
    common.add_std_args(parser)

    args = parser.parse_args(argv)

    common.require(TOOL, args.epochs > 0, '--epochs must be > 0')
    common.check_std_args(TOOL, args)

    doc  = common.read_input_json(TOOL, args.inp)
    temp = input_temperature(doc)

    if temp is None:
        common.die(TOOL, 'no temperature found in %s -- expected '
                         '`params.temperature`, `temperature`, '
                         '`summary.temperature` or `series.temperature`'
                         % args.inp)

    if temp <= 0:
        common.die(TOOL, 'input temperature must be > 0, found %r in %s'
                         % (temp, args.inp))

    out = run(temp, args.epochs, args.seed, args.duration_sec,
              source=args.inp)
    common.write_output(TOOL, args.out, out)

    elapsed = common.pace(started, args.duration_sec)

    sys.stdout.write('%s: T=%g epochs=%d final_accuracy=%.4f '
                     'final_loss=%.4f -> %s (%.2fs)\n'
                     % (TOOL, temp, args.epochs,
                        out['summary']['final_accuracy'],
                        out['summary']['final_loss'], args.out, elapsed))
    return 0


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.exit(main())

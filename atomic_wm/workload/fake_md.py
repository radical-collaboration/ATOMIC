"""``atomic-fake-md`` -- synthetic molecular dynamics stage.

Stands in for a real MD engine (LAMMPS, ...).  Produces an energy series
which relaxes exponentially towards a temperature dependent equilibrium
mean, plus a thermostat-like temperature series fluctuating around the
requested temperature.  Deterministic for a given ``--seed``.
"""

import argparse
import math
import random
import sys
import time

from typing import Any, Dict, Optional, Sequence

from . import common

TOOL = 'atomic-fake-md'
TYPE = 'simulation'


# ---------------------------------------------------------------------------
def simulate(temperature: float, steps: int, seed: int
             ) -> Dict[str, Sequence[float]]:
    """Generate the ``step`` / ``energy`` / ``temperature`` series.

    The equilibrium energy rises with temperature, the system relaxes
    towards it from a colder start, and both series carry temperature
    scaled noise.  Pure function of its arguments -- same arguments, same
    numbers, on any machine.
    """

    rng      = random.Random(seed)
    e_equi   = -100.0 + 0.05 * temperature       # equilibrium mean energy
    e_start  = e_equi - 25.0                     # starting (cold) energy
    tau      = max(1.0, steps / 5.0)             # relaxation time constant
    e_sigma  = 0.40 * math.sqrt(temperature / 300.0)
    t_sigma  = 0.02 * temperature

    step, energy, temp = [], [], []

    for i in range(steps):
        relax = math.exp(-i / tau)
        step.append(i)
        energy.append(e_equi + (e_start - e_equi) * relax
                             + rng.gauss(0.0, e_sigma))
        temp.append(temperature + rng.gauss(0.0, t_sigma))

    return {'step'       : step,
            'energy'     : common.rounded(energy),
            'temperature': common.rounded(temp)}


# ---------------------------------------------------------------------------
def run(temperature: float, steps: int, seed: Optional[int],
        duration_sec: float) -> Dict[str, Any]:
    """Produce the full output document for the given arguments.

    ``seed=None`` derives a stable seed from the parameters themselves --
    identical parameters then give identical output without ``--seed``.
    """

    if seed is None:
        seed = common.default_seed(TOOL, temperature, steps)

    series  = simulate(temperature, steps, seed)
    energy  = series['energy']
    summary = {'mean_energy'     : common.mean(energy),
               'std_energy'      : common.stddev(energy),
               'final_energy'    : energy[-1] if energy else 0.0,
               'mean_temperature': common.mean(series['temperature']),
               'n_steps'         : steps}

    params  = {'temperature' : temperature,
               'steps'       : steps,
               'seed'        : seed,
               'duration_sec': duration_sec}

    # `temperature` is mirrored at the top level as well: the demo
    # contract shows it there, and downstream stages look for it in both
    # places (see fake_train.input_temperature).
    return common.make_envelope(TOOL, TYPE, params, series, summary,
                                extra={'temperature': temperature})


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console script entry point."""

    started = time.monotonic()

    parser = argparse.ArgumentParser(
        prog=TOOL, description='synthetic MD simulation stage')
    parser.add_argument('--temperature', type=float, required=True,
                        metavar='T', help='simulation temperature in K')
    parser.add_argument('--steps', type=int, required=True, metavar='N',
                        help='number of MD steps to emit')
    parser.add_argument('--out', required=True, metavar='PATH',
                        help='output JSON file (written atomically)')
    common.add_std_args(parser)

    args = parser.parse_args(argv)

    common.require(TOOL, args.temperature > 0, '--temperature must be > 0')
    common.require(TOOL, args.steps > 0,       '--steps must be > 0')
    common.check_std_args(TOOL, args)

    doc = run(args.temperature, args.steps, args.seed, args.duration_sec)
    common.write_output(TOOL, args.out, doc)

    elapsed = common.pace(started, args.duration_sec)

    sys.stdout.write('%s: T=%g steps=%d mean_energy=%.3f std_energy=%.3f '
                     '-> %s (%.2fs)\n'
                     % (TOOL, args.temperature, args.steps,
                        doc['summary']['mean_energy'],
                        doc['summary']['std_energy'], args.out, elapsed))
    return 0


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.exit(main())

# The synthetic ATOMIC workload

The demo pipeline (MD → ML training → analysis) is executed with three
*synthetic* tools shipped by `atomic_wm`. They produce **numbers, not
science** — but they produce them in the shape a real code would, so the
federation, the campaign manager and the demo UI exercise the real data
path end to end.

| console script | stage type | reads | writes |
|---|---|---|---|
| `atomic-fake-md` | `simulation` | — | `md.json` |
| `atomic-fake-train` | `ml_training` | `md.json` | `model.json` |
| `atomic-fake-descriptors` | `analysis` | `md.json` | `desc.json` |

Design rules (they are the contract, not an accident):

- **stdlib only.** A task runs these with whatever python the target
  resource provides; `pip install atomic-wm` must not need the network
  on a login node. No numpy, no requests, nothing.
- **deterministic, with or without `--seed`.** Same arguments plus same
  seed → same numbers on any machine; only `produced_by.ts`/`host` vary.
  Without `--seed` the seed is a *stable hash of the tool's own
  parameters* (`blake2b` over a canonical JSON of e.g. tool + temperature
  + steps — unlike `hash()`, independent of `PYTHONHASHSEED`), so
  identical parameters reproduce identical output and different sweep
  points get visibly different noise. The resolved seed is always
  recorded in `params.seed`, so a result stays reproducible.
- **paced by `--duration-sec`** (default 5 s). The tool computes its
  result immediately and then pads its wall time, so the demo has tasks
  that visibly run without wasting cycles.
- **atomic output.** Every output is written to a temp file in the target
  directory and `os.replace()`d into place, so result collection never
  reads a half-written file and a crash leaves no partial output.
- **clear failure.** Bad arguments or unusable input exit with code `2`
  and a one-line `tool: error: …` message on stderr (2 is what argparse
  uses for usage errors, so callers see one 'bad input' code).
- **one summary line on stdout**, which ends up in the task's log.

## The JSON envelope

Every tool emits the same document:

```json
{
  "type": "simulation",
  "params": {"temperature": 300.0, "steps": 200, "seed": 0,
             "duration_sec": 5.0},
  "series": {"step": [0, 1, 2],
             "energy": [-124.9, -123.6, -122.4],
             "temperature": [301.2, 297.7, 305.9]},
  "summary": {"mean_energy": -90.18, "std_energy": 6.50,
              "final_energy": -85.54, "mean_temperature": 298.37,
              "n_steps": 200},
  "produced_by": {"tool": "atomic-fake-md", "version": "0.1.0",
                  "host": "nid001234", "ts": 1757100000.0}
}
```

- `series` — equal-length lists, the thing the UI plots.
- `summary` — scalars, the thing a table shows.
- `params` — the tool's own arguments, so a result is self-describing.
- `produced_by` — provenance; `host` is where the task actually ran,
  which is how the UI can show *which resource* produced a curve.

The envelope is assembled in exactly one place —
`atomic_wm/workload/common.py::make_envelope()`. A revision of the
contract touches that one function (plus the per-tool `summary` dicts).

`atomic-fake-md` additionally mirrors its temperature as a **top-level
`temperature`** key, because `plans/00-overview.md` §"Fake workload"
shows it there while `plans/03-atomic-workload.md` puts it in `params`.
Both are satisfied; consumers should prefer `params.temperature`.
`make_envelope(..., extra={...})` is the mechanism.

## `atomic-fake-md`

```
atomic-fake-md --temperature T --steps N --out md.json
               [--seed S] [--duration-sec D]
```

Energy relaxes exponentially from a cold start towards a
temperature-dependent equilibrium mean (`-100 + 0.05·T`, so hotter runs
sit higher), with noise scaled by `sqrt(T/300)`. The temperature series
fluctuates around `T` like a thermostat (σ = 2 %).

Summary: `mean_energy`, `std_energy`, `final_energy`, `mean_temperature`,
`n_steps`.

## `atomic-fake-train`

```
atomic-fake-train --in md.json --epochs E --out model.json
                  [--seed S] [--duration-sec D]
```

Reads the MD document and takes its temperature from
`params.temperature`, else a top-level `temperature`, else
`summary.temperature`, else the mean of `series.temperature` — so a
hand-written or third-party upstream document works too. No temperature
anywhere is a fatal input error.

Loss decays exponentially towards a floor; accuracy rises towards a
plateau that **decreases with the input temperature**:

| MD temperature | accuracy plateau |
|---|---|
| ≤ 300 K | 0.99 |
| 600 K | 0.93 |
| 900 K | 0.85 |

(piecewise linear through those knots, extrapolated with the outermost
slope, clamped to `[0.50, 0.99]`). That is the whole point of the sweep:
the three workflows of `examples/campaign_sweep.json` produce visibly
different, correctly ordered curves in the UI.

`final_accuracy` is **strictly decreasing** in temperature by
construction: the plateau gaps are 0.06 while the accuracy noise is
σ = 0.004, and `final_accuracy` is the mean of the last 10 % of epochs,
so no single noisy epoch can flip the ordering. Tests assert the
ordering both with an explicit seed, with the derived default seeds, and
across 25 seeds. Summary: `final_accuracy`,
`final_loss`, `accuracy_plateau`, `input_temperature`, `epochs`.

## `atomic-fake-descriptors`

```
atomic-fake-descriptors --in md.json --out desc.json
                        [--n-atoms N] [--bins B] [--seed S]
                        [--duration-sec D]
```

Optional third stage. Draws `--n-atoms` (default 256) per-atom
"descriptor" values around the MD run's `mean_energy` / `std_energy` and
histograms them into `--bins` (default 20) bins. `series` is
`{"bin_center": [...], "count": [...]}` with counts summing to `n_atoms`;
summary carries `n_atoms`, `n_bins` and the descriptor min/mean/max.

## Swapping in a real code

The workflow spec (`examples/workflow_vacancy.json`) refers to a stage
only by its `cmd`, its declared `inputs`/`outputs` and its
`requirements`. Replacing a fake stage with a real one therefore means:

1. keep the CLI shape — the real tool must accept the parameter the sweep
   varies and an explicit `--out` path, and write its output into the
   task's cwd;
2. keep the JSON envelope — emit `type` / `params` / `series` / `summary`
   (a thin wrapper script around LAMMPS or a training script is enough;
   it can import `atomic_wm.workload.common.make_envelope` if
   `atomic_wm` is installed on that resource, or just write the dict);
3. declare what the real code needs in the stage `requirements`
   (`cores`, `gpus`, `software`) — the federation's `pick` filters
   resources against exactly those keys, so a stage needing `lammps`
   lands only on a resource that declared it at join time;
4. raise `--duration-sec`'s real equivalent (walltime) in the pool
   declaration if the real code runs longer than the pilot walltime.

Nothing else in the stack knows that the workload is fake.

## Testing

```
ve3/bin/python -m pytest tests/ -q     # unit tests (source tree)
ve3/bin/flake8 atomic_wm/ tests/       # lint
ve3/bin/pip install /home/merzky/projects/atomic
ve3/bin/atomic-fake-md --temperature 300 --steps 50 --out /tmp/md.json \
                       --duration-sec 0.2
ve3/bin/atomic-fake-train --in /tmp/md.json --epochs 10 \
                          --out /tmp/model.json --duration-sec 0.2
```

`tests/test_workload.py` covers determinism, series shapes, the
temperature→plateau ordering, atomic writes (including no temp file left
behind on failure), the bad-input exit codes, pacing, and a subprocess
run of the full three-stage pipeline.

# P3 — synthetic workload + package skeleton (atomic repo)

Repo/branch: `/home/merzky/projects/atomic` @ `feature/demo-wm`.

## Package skeleton (this piece creates it; others add to it)

```
pyproject.toml            # name atomic-wm, package atomic_wm, console scripts,
                          # entry-points group "radical.orbit.plugins"
atomic_wm/__init__.py
atomic_wm/workload/{fake_md.py, fake_train.py, fake_descriptors.py, common.py}
atomic_wm/cli/            # (02, 04)
atomic_wm/plugins/        # (04) atomic_campaign plugin
atomic_wm/ui/atomic.js    # (05)
tests/
docs/
demo/local/               # (06)
```
- Build backend: setuptools; `requires-python >= 3.10`; deps: `numpy`
  (already in ve3? check; if not, keep workloads numpy-free — plain
  `random` + `math` is enough), plus the HTTP lib chosen in 02.
- Console scripts: `atomic-fake-md`, `atomic-fake-train`,
  `atomic-fake-descriptors`, `atomic-join`, `atomic-leave`,
  `atomic-resources`, `atomic-campaign`.
- Entry point: `[project.entry-points."radical.orbit.plugins"]
  atomic_campaign = "atomic_wm.plugins.campaign"`.

## Workloads (contract in 00-overview §"Fake workload")

- Deterministic **by default**: seed = stable hash (e.g. `zlib.crc32`) of
  the tool's own parameters (temperature, steps / epochs), `--seed`
  overrides; the train plateau gaps (0.99 / 0.93 / 0.85 for 300 / 600 /
  900 K, monotone in T) dominate the noise so `final_accuracy` is strictly
  decreasing in temperature — tested. Wall time controlled by `--duration-sec`
  (default 5) so the demo paces itself; write output atomically (tmp +
  rename); exit non-zero with a clear message on bad input; print a
  one-line summary to stdout (shows up in task logs).
- `fake_md`: energy series relaxes toward a temperature-dependent mean
  with noise; temperature series fluctuates around T; summary has
  `mean_energy`, `std_energy`, `n_steps`.
- `fake_train`: reads `md.json`; loss decays, accuracy rises to a plateau
  that decreases with input temperature (e.g. 0.99 at 300 K → 0.85 at
  900 K) — the point is that three workflows produce visibly different
  curves; summary `final_accuracy`, `epochs`.
- `fake_descriptors`: histogram of "descriptor" values from `md.json`
  (bins + counts), summary `n_atoms`. Optional stage; keep small.
- Shared JSON envelope: `{"type": …, "params": {…}, "series": {…},
  "summary": {…}, "produced_by": {"tool", "version", "host", "ts"}}`.

## Files

- `atomic_wm/workload/*.py`, `tests/test_workload.py` (determinism, shape
  of series, temperature-dependent plateau ordering, atomic write, bad
  input exit code), `docs/workload.md` (what each tool emits, why it is
  fake, how to swap in real codes: same CLI + JSON envelope).
- A sample workflow spec `examples/workflow_vacancy.json` matching
  00-overview and a sample campaign request `examples/campaign_sweep.json`.

## Acceptance

- `ve3/bin/pip install /home/merzky/projects/atomic` succeeds; the three
  tools run from `ve3/bin/`; `pytest tests/ -q` green; `flake8` clean.

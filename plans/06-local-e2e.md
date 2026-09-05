# P6 — local end-to-end demo run (atomic repo)

Repo/branch: `/home/merzky/projects/atomic` @ `feature/demo-wm`.
Goal: one command brings up the whole demo on localhost; one command
proves it works; one command tears it down. This is also Monday's
rehearsal harness — the real run differs only in join arguments.

## Files (`demo/local/`)

- `env.sh` — resolves `ORBIT_SRC=/home/merzky/radical/radical.orbit`,
  `VE=$ORBIT_SRC/ve3`, exports `PYTHONPATH=$ORBIT_SRC/src`,
  `PATH=$VE/bin:$PATH`, `RADICAL_ORBIT_BROKER_URL=http://127.0.0.1:8000`,
  run dir `demo/local/run/` (logs, pids, scratch under `/tmp/atomic-demo`).
- `up.sh` — (1) `ve3/bin/pip install /home/merzky/projects/atomic` (quiet,
  non-editable) so entry points and console scripts are current; (2) start
  broker `--no-auth --port 8000 --plugins
  task_dispatcher,federation,atomic_campaign` (background, log, pidfile,
  wait for `GET /endpoints` 200); (3) three joins, detached:
  - `local_a`: `--mode allocation --site Rutgers --kind workstation --declare cores=4,gpus=0,mem_gb=8 --software lammps`
  - `local_b`: `--mode allocation --site NERSC --kind hpc --declare cores=4,gpus=1,mem_gb=16 --software lammps,pytorch`
  - `local_c`: `--mode login --site PSC --kind hpc --queue local --account demo --nodes 1 --cpus 2 --walltime 1800 --node-hours 2 --software pytorch`
  (4) wait until `atomic-resources` shows all three; print the Explorer
  URL and the resource table.
- `smoke.py` — submit `examples/workflow_vacancy.json` with sweep
  `temperature=300,600,900`; wait ≤ 10 min; assert: campaign DONE, three
  workflows DONE, stage `md` ran on a resource advertising `lammps`, stage
  `train` on one advertising `pytorch`, at least two distinct resources
  used, `results` has three `final_accuracy` values strictly decreasing
  with temperature, store directory contains the six JSON outputs and
  manifests; print a compact placement table. Exit non-zero with the
  failing assertion and pointers to logs.
- `down.sh` — leave all, stop endpoints/pilots/broker, verify with
  `pgrep -af radical-orbit` (must be empty), keep logs.
- `demo/local/README.md` — how to run, what you should see, where logs
  are, how to map to the real resources (join arguments per machine,
  which resource to join live).

## Spike gate

Before P6 starts, the spike report (background agent) must say the local
pool path works. If it does not, P1's `submit` gains an `endpoint-mode`
fallback (dispatcher `endpoint=` + output collection via the endpoint's
`staging` plugin) and `up.sh` joins resources in that mode.

## Acceptance

- `./demo/local/up.sh && ve3/bin/python demo/local/smoke.py && ./demo/local/down.sh`
  passes from a clean state twice in a row.
- Total smoke wall time < 5 min with default workload durations.
- Explorer page (P5) shows the run live while smoke runs.

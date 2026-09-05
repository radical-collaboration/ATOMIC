# P6 — local end-to-end demo run (atomic repo)

Repo/branch: `/home/merzky/projects/atomic` @ `feature/demo-wm`.
Goal: one command brings up the whole demo on localhost; one command
proves it works; one command tears it down. This is also Monday's
rehearsal harness — the real run differs only in join arguments.

## Files (`demo/local/`)

- `env.sh` — resolves `ORBIT_SRC=/home/merzky/radical/radical.orbit`,
  `VE=$ORBIT_SRC/ve3`, exports `PYTHONPATH=$ORBIT_SRC/src`,
  `PATH=$VE/bin:$PATH`, `RADICAL_ORBIT_BROKER_URL=https://127.0.0.1:8010`,
  `RADICAL_ORBIT_BROKER_CERT=$HOME/.radical/orbit/broker_cert.pem`,
  `RADICAL_ORBIT_RHAPSODY_BACKEND=concurrent`, `RADICAL_ORBIT_LOG_LVL=INFO`;
  `unset RADICAL_LOG_LVL RADICAL_ORBIT_LOG_FILE`; run dir `demo/local/run/`
  (logs, pids), scratch/state under `/tmp/atomic-demo`. (All per the spike
  findings in 00-overview.)
- `up.sh` — (1) `ve3/bin/pip install $ORBIT_SRC` then `ve3/bin/pip install
  /home/merzky/projects/atomic` (quiet, non-editable) so pilots, wrapper,
  entry points and console scripts are current; (2) isolate dispatcher /
  federation / campaign state dirs under `/tmp/atomic-demo` (config or
  env if the plugins support it; else back up `~/.radical/orbit/
  task_dispatcher/state/` to `demo/local/run/state.bak-<ts>` and clear);
  (3) start broker `--host 127.0.0.1 --port 8010 --no-auth --plugins
  task_dispatcher,federation,atomic_campaign` (background, log, pidfile,
  wait for `GET /endpoints` 200); (4) three joins, detached:
  - `local_a`: `--mode allocation --site Rutgers --kind workstation --declare cores=4,gpus=0,mem_gb=8 --software lammps --scratch /tmp/atomic-demo/local_a`
  - `local_b`: `--mode allocation --site NERSC --kind hpc --declare cores=4,gpus=1,mem_gb=16 --software lammps,pytorch --scratch /tmp/atomic-demo/local_b`
  - `local_c`: `--mode login --site PSC --kind hpc --queue local --account demo --nodes 1 --cpus 2 --walltime 1800 --node-hours 2 --software pytorch --scratch /tmp/atomic-demo/local_c`
  (5) wait until `atomic-resources` shows all three, and until the two
  allocation-mode resources report `pilots_active ≥ 1` (budget 90 s each,
  they warm up in parallel); print the Explorer URL and the resource table.
- `smoke.py` — submit `examples/workflow_vacancy.json` with sweep
  `temperature=300,600,900`; wait ≤ 10 min; assert: campaign DONE, three
  workflows DONE, stage `md` ran on a resource advertising `lammps`, stage
  `train` on one advertising `pytorch`, at least two distinct resources
  used, `results` has three `final_accuracy` values strictly decreasing
  with temperature, store directory contains the six JSON outputs and
  manifests; print a compact placement table. Exit non-zero with the
  failing assertion and pointers to logs.
- `down.sh` — `atomic-leave` all three, stop the broker, then `pkill -f
  radical-orbit-endpoint` (psij `local` cancel only cancels the wrapper
  job; child pilots can outlive it and keep redialing), verify with
  `pgrep -af radical-orbit` (must be empty), keep logs. Time-outs in
  `smoke.py` are on task/campaign state, never on pilot state.
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

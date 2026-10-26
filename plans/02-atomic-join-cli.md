# P2 — `atomic-join` CLI + bootstrap (atomic repo)

Repo/branch: `/home/merzky/projects/atomic` @ `feature/demo-wm`.
Package: `atomic_wm` (see 03 for the package skeleton — whoever lands
first creates `pyproject.toml`; coordinate through STATUS.md).

## What "joining" means

One command turns a machine — or a running compute allocation — into a
federated resource:

```
atomic-join --broker https://r3:8003 --name perlmutter_a --mode allocation \
            --site NERSC --software lammps,pytorch
atomic-join --broker … --name bridges_login --mode login --site PSC \
            --queue RM --account abc123 --nodes 1 --cpus 128 --walltime 3600 \
            --node-hours 20 --software lammps
atomic-join --broker http://127.0.0.1:8000 --name local_a --mode allocation \
            --declare cores=4,gpus=0,mem_gb=8 --software lammps       # localhost
```

Steps performed:
1. Start an endpoint: `radical-orbit-endpoint.py --name ep_<name> --url …
   [--token …] [--cert …] --plugins default` as a child process (the
   endpoint has no log-file flag: redirect stdout/err to
   `~/.radical/orbit/atomic/<name>/endpoint.log`). The child's env: `PATH`
   with the venv `bin` first (psij-launched pilots find
   `radical-orbit-endpoint-wrapper.sh` by name), `RADICAL_ORBIT_LOG_LVL`
   set, `RADICAL_LOG_LVL` and `RADICAL_ORBIT_LOG_FILE` removed,
   `RADICAL_ORBIT_RHAPSODY_BACKEND` passed through if set.
2. Wait until the endpoint shows as connected (`GET /endpoints`, timeout
   60 s, clear error otherwise).
3. Auto-detect capabilities unless `--declare` overrides: `sysinfo` is
   session-scoped — register a sysinfo session on the endpoint, `GET
   /<ep>/sysinfo/metrics/{sid}` (`cores_logical`, `memory.total`, GPUs),
   unregister; `queue_info/job_allocation` is session-less (allocation
   mode only). Declared values win over detected ones.
4. `POST /broker/federation/join/default` with the assembled record
   (federation routes always use the reserved `default` sid — no session
   registration needed). Include `scratch_base` from `--scratch` if given.
5. Stay in the foreground, relaying endpoint liveness; on SIGINT/SIGTERM
   call `leave/default/<name>` and stop the endpoint. `--detach` writes a
   pidfile (`~/.radical/orbit/atomic/<name>/endpoint.pid`) and exits (for
   the demo's pre-joined resources); `atomic-leave NAME` reads it, calls
   `leave`, and terminates the endpoint process **and any surviving pilot
   children** (psij `local` cancel only cancels the wrapper job — match
   on `radical-orbit-endpoint` + the pool prefix `fed-<name>_`).

## Files

- `atomic_wm/cli/join.py` (`atomic-join`), `atomic_wm/cli/leave.py`,
  `atomic_wm/cli/resources.py` (`atomic-resources`: table with name, site,
  mode, cores/gpus/mem, software, node-hours used/remaining, pilots,
  tasks, liveness; `--json`).
- `atomic_wm/client.py` — thin HTTP client for gateway + federation +
  campaign routes (`requests`, present in ve3; TLS verify against
  `--cert`/`$RADICAL_ORBIT_BROKER_CERT`, sid `default` baked in). The
  supervisor seeds this file with the function signatures; P2 implements
  it; P4's CLI calls it (P4 tests mock it) — do not rename functions.
- `atomic_wm/endpoint_proc.py` — start/stop/health of the endpoint child.
- `bootstrap.sh` (repo root) — `bootstrap.sh <venv-dir> <atomic-join args…>`:
  create venv if missing, `pip install` radical.orbit (from a configurable
  git URL/branch or local path) and this repo (non-editable), then `exec
  atomic-join …`. Idempotent; prints what it does. Must work with only
  python3 + git on a login node.
- `docs/join.md` — modes, arguments, what happens, troubleshooting (log
  locations, "endpoint never connected", firewall note: outbound only).
- `tests/test_cli_join.py` — argument parsing, record assembly from
  detected+declared values (declared wins), mode validation (login needs
  queue/account/size/node-hours), fake HTTP server or `responses`-style
  mocking for the join flow, signal handling → leave.

## Acceptance

- `pytest tests/ -q` green; `flake8 atomic_wm` clean.
- Locally: with a broker + federation running, `atomic-join --mode
  allocation --name local_a --declare cores=4 --software lammps` results
  in a resource listed by `atomic-resources` within 10 s; Ctrl-C removes it.
- `bootstrap.sh` from a fresh temp dir reaches `atomic-join --help`.

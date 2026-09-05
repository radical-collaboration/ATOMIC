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
   [--token …] --plugins default` as a child process (stdout/err to
   `~/.radical/orbit/atomic/<name>/endpoint.log`). `PATH` must include the
   venv `bin` so psij-launched pilots find `radical-orbit-endpoint-wrapper.sh`.
2. Wait until the endpoint shows as connected (`GET /endpoints`, timeout
   60 s, clear error otherwise).
3. Auto-detect capabilities unless `--declare` overrides: call the
   endpoint's `sysinfo` (`cores_logical`, memory, GPUs) and `queue_info`
   (`job_allocation` in allocation mode) through the gateway.
4. `POST /broker/federation/join/{sid}` with the assembled record (own
   plugin session via `register_session`).
5. Stay in the foreground, relaying endpoint liveness; on SIGINT/SIGTERM
   call `leave` and stop the endpoint. `--detach` writes a pidfile and
   exits (for the demo's pre-joined resources).

## Files

- `atomic_wm/cli/join.py` (`atomic-join`), `atomic_wm/cli/leave.py`,
  `atomic_wm/cli/resources.py` (`atomic-resources`: table with name, site,
  mode, cores/gpus/mem, software, node-hours used/remaining, pilots,
  tasks, liveness; `--json`).
- `atomic_wm/client.py` — thin HTTP client for gateway + federation +
  campaign routes (`requests`/`httpx` — use what orbit already depends on;
  check `setup.py` install_requires). Shared by 02/04/05 CLI pieces.
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

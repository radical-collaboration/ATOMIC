# The ATOMIC WM demo (2026-09-08)

NSF ATOMIC (Rutgers). Demo presented 2026-09-08.

This demo shows one user putting their own compute resources into a
shared pool and running science work across them. The user starts a
broker, then joins resources into the pool. Each join declares what the
resource has: cores, GPUs, installed software. The user then submits a
campaign: a parameter sweep that turns one small workflow into several
workflows, one per temperature. The workflow manager places each stage of
each workflow on a resource that matches what the stage needs: the MD
stage needs `lammps`, the training stage needs `pytorch` and a GPU.
Results are staged back to a central store on the broker host, and the
campaign is visible while it runs in the Explorer UI.

## Slides

- Presentation:
  https://docs.google.com/presentation/d/1RdDFXfN_TKK9a50oiSlkhM8WMFnheCkV35so1-J_aJM/

## Demo steps ↔ slides

| Step | What you see | Slide | Command |
|---|---|---|---|
| Start the broker | the central service comes up, Explorer URL printed | D1 | `broker.sh` |
| Join resources | resources appear in the pool with their software and allowance | D2, 4 Federation | `join.sh <resource>` |
| Submit the campaign | one sweep becomes several workflows | D3, 3 Campaigns | `submit.sh --wait` |
| Watch placement | each stage lands on a resource that matches its requirements | D3 | Explorer UI |
| Results come back | outputs and models collected centrally, addressable after the run | D4, 5 Central data management | `atomic-campaign results` |

The Explorer UI is served by the broker; `broker.sh` prints its URL. The
CLI does the same things as the UI (`atomic-join`, `atomic-leave`,
`atomic-resources`, `atomic-campaign`), because the interface is the
point, not the UI.

## What is real and what is simulated

Real:

- The ORBIT broker and the endpoints that join it.
- The federation plugin and the task dispatcher: capability matching,
  resource registry, budget bookkeeping, placement.
- Joins: a resource dials out, no inbound firewall exception, no service
  installed by the site.
- Data movement: inputs staged to where the work runs, outputs collected
  back into the central store.

Simulated:

- The science workloads. `atomic-fake-md`, `atomic-fake-train` and
  `atomic-fake-descriptors` are synthetic stand-ins. They burn a little
  time and write plausible JSON; they do no science.
- The Campaign Manager. `atomic_campaign` is a small broker-hosted
  plugin, enough to expand a sweep and drive the stages. The real
  campaign manager is later project work.
- The UI. The Explorer plugin is a stand-in for the ATOMIC portal. Its
  job is to prove the interface exists, not to demo the portal.

## The resources

| name | host | mode | scratch | broker |
|---|---|---|---|---|
| `r3` | radical.3, the Rutgers workstation and the broker host | allocation (no batch system) | `/tmp/atomic-demo` | itself, bound `0.0.0.0:8010` |
| `perlmutter` | NERSC | allocation (inside `salloc`) | `$PSCRATCH/atomic-demo`, else `$SCRATCH` | radical.3 |
| `odo` | OLCF, project `fus183` | allocation (inside an allocation) | `$HOME/tmp/atomic-demo` | radical.3, via the ORNL proxy |
| `local`, `local_a/b/c` | the laptop's three fake resources | baked in | `/tmp/atomic-demo` | its own loopback broker |

All commands below are run from `demo/2026_09_08/` in a checkout of this
repo.

## The run

**On radical.3 (the broker host).**

```bash
git pull
./broker.sh --resource r3        # installs the stack, starts the broker
./join.sh   r3                   # r3 joins itself
```

`broker.sh` ends with the `scp` line for the broker certificate that the
remote hosts need.

**On Perlmutter.** On a login node, get an allocation, then join from
inside it:

```bash
salloc -N 2 -C cpu -q interactive -t 2:00:00 -A amsc007
cd <checkout>/demo/2026_09_08 && ./join.sh perlmutter
```

**On Odo.** Get an allocation in the `interact` queue on project
`fus183`, then, inside it:

```bash
cd <checkout>/demo/2026_09_08 && ./join.sh odo
```

**Submit the campaign** (from the broker host, or any host with the
environment):

```bash
./submit.sh --wait               # sweep temperature=300,600,900
./submit.sh --wait --sweep temperature=300,600
```

Watch it in the Explorer at https://95.217.193.116:8010/ (self-signed
certificate, accept the browser warning). `--wait` prints the campaign
status and results when it is done; `atomic-campaign results <cid>`
prints them again later (`source ./env.sh` first to get the CLIs).

**Teardown.** Run `./down.sh` on each host. It takes down only what that
host started or joined, and on the broker host it also stops the broker.
The scripts remember the resource of the last join or broker run in
`run/resource`, so `down.sh` needs no argument there.

## What the scripts do for you

- Install the pinned stack themselves: `radical.orbit@feature/atomic-federation`
  and `ATOMIC@feature/demo-wm`, cloned under the site's demo home (or
  `/tmp/atomic-demo/src`), `git pull` plus a reinstall only when the
  commit moved (`--skip-install` skips the check, `--reinstall` forces it).
- Load the site python module: `python/3.12-26.1.0` on Perlmutter,
  `cray-python` on Odo.
- Put the venv and the clones on scratch: `$SCRATCH/demo` on Perlmutter,
  `$HOME/tmp/demo` on Odo (home directories are quota'd).
- Set the ORNL proxy on Odo, and clone over https there.
- Declare `shared_fs=false` for the remote joins (plus `gpus=8` on Odo).
  The endpoint inside the allocation is the pilot; nothing else is
  launched there.
- Dial radical.3 as the broker from every resource.
- Start `broker.sh` with an empty federation; `--keep-state` opts out.

## Prerequisites

- The broker certificate at `~/.radical/orbit/broker_cert.pem` on each
  remote host. `broker.sh` prints the `scp` line for it.
- A clone of this repo that can reach GitHub: ssh works on Perlmutter,
  Odo needs https and `env.sh` switches its repo URLs over automatically.
- python >= 3.10, which the module load provides.

## If something goes wrong

- A stale federation record (liveness not `ok`) does not block a re-join:
  `join.sh` leaves it first and says so.
- `a resource named 'X' is already in the federation` (409) means a live
  one: `atomic-leave X`, then join again.
- Logs are in `run/`: `broker.log`, `join-<name>.log`, `leave.log`.
- The endpoint's own log, pilot start-up included, is in
  `/tmp/atomic-demo/endpoints/<name>/endpoint.log`.
- `pgrep -af radical-orbit` shows what is still running.
- `./down.sh --all-endpoints` kills every orbit endpoint on the machine,
  not only this demo's. Only do that if none of the others are yours.

## Environment

| variable | what |
|---|---|
| `ATOMIC_DEMO_BROKER_HOST` | the broker every client and endpoint dials (default radical.3) |
| `ATOMIC_DEMO_RESOURCE` | the resource, when no argument is given |
| `ATOMIC_DEMO_SCRATCH_BASE` | where task scratch goes on this machine |
| `ATOMIC_DEMO_HOME` | where the venv and the clones live on an HPC site |
| `ATOMIC_DEMO_PYTHON_MODULE` | the environment module loaded before anything else |

## The localhost variant

`./up.sh` brings the whole demo up on a laptop: a loopback broker on
`https://127.0.0.1:8010/` plus three fake resources (`local_a`,
`local_b`, `local_c`). It sets the broker host itself, so there is
nothing to export. `$ATOMIC_DEMO_VE/bin/python smoke.py` then submits the
same campaign and asserts what the demo claims (placement by capability,
several resources and members used, results in the central store), and
`./down.sh` tears it down again.

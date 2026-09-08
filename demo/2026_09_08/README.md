# The ATOMIC WM demo (2026-09-08)

NSF ATOMIC (Rutgers). Demo presented 2026-09-08.

This demo shows one user putting their own compute resources into a
shared pool and running science work across them. The user starts a
broker, then joins resources into the pool — each join declares what the
resource has: cores, GPUs, installed software. The user then submits a
campaign: a parameter sweep that turns one small workflow into several
workflows, one per temperature. The workflow manager places each stage of
each workflow on a resource that matches what the stage needs — the MD
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

The Explorer UI is served by the broker; `broker.sh` and `up.sh` print
its URL. The CLI does the same things as the UI — `atomic-join`,
`atomic-leave`, `atomic-resources`, `atomic-campaign` — because the
interface is the point, not the UI.

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

## The scripts — one per role

The harness is one script per **role**, and they all source `env.sh` —
which owns the whole environment and every shared helper, so no script
duplicates any of it:

| script | role | what it is |
|---|---|---|
| `broker.sh` | broker | install, isolate state, start the broker, wait for it |
| `join.sh` | resource | join **one** resource (`--live` = the on-camera join) |
| `submit.sh` | client | submit the campaign (`--wait` = follow it to the end) |
| `up.sh` | orchestrator | `broker.sh` + three `join.sh` + wait + report |
| `down.sh` | teardown | leave, stop, sweep, verify, restore |
| `smoke.py` | proof | submit a campaign and **assert** the demo's claims |

Everything runs against `https://127.0.0.1:8010`. Tuesday's run on real
machines is the *same code* with different `atomic-join` arguments — see
"Mapping to the real resources" at the end.

Every role — broker, endpoints, pilots, CLIs — runs the **same pinned
software stack**, installed into one venv by `ensure_stack`. Read "How
the stack is pinned and installed" below before running this on a host
you have not run it on.

## Which resource — one parameter, everywhere

`env.sh` takes exactly **one** parameter: the resource this shell is
about. Every role script passes it through, so you never source `env.sh`
by hand:

```bash
demo/2026_09_08/join.sh local_a               # RESOURCE is the argument
demo/2026_09_08/join.sh perlmutter
demo/2026_09_08/broker.sh    --resource r3    # the others take --resource
demo/2026_09_08/submit.sh    --resource r3 --wait
demo/2026_09_08/down.sh      --resource r3
demo/2026_09_08/check_env.sh --resource odo
```

Without a flag they use `$ATOMIC_DEMO_RESOURCE`, and failing that
`local`. `up.sh` is the laptop orchestrator and is always `local`.

| name | host | site | broker | scratch base |
|---|---|---|---|---|
| `local` | the laptop | Rutgers | radical.3 (a broker started here binds `127.0.0.1`) | `/tmp/atomic-demo` |
| `local_a` `local_b` `local_c` | the laptop's three fake resources | Rutgers | radical.3 | `/tmp/atomic-demo` |
| `r3` | radical.3, the RADICAL lab server — the demo's broker host | Rutgers | binds `0.0.0.0:8010` | `/tmp/atomic-demo` |
| `perlmutter` | NERSC | NERSC | radical.3 | `$PSCRATCH/atomic-demo` (else `$SCRATCH`) |
| `odo` | OLCF's Slurm test system (project `fus183`) | OLCF | radical.3 | `$HOME/tmp/atomic-demo` |

**The broker is radical.3 for every resource** — `https://95.217.193.116:8010`
— unless `ATOMIC_DEMO_BROKER_HOST` says otherwise. The laptop's fake
resources are no exception: `join.sh local_a` joins the real broker. Only
`broker.sh --resource local` and `up.sh` switch themselves to
`127.0.0.1`, since a laptop broker binds loopback and is reachable
nowhere else; to join or submit against that one, export
`ATOMIC_DEMO_BROKER_HOST=127.0.0.1` (broker.sh prints the line).
`ATOMIC_DEMO_BROKER_BIND` is the listen address (broker.sh only);
`ATOMIC_DEMO_SCRATCH_BASE` overrides the scratch base anywhere.

**The join mode is detected, not configured.** `$SLURM_JOB_ID` set means
this shell is *inside an allocation*, so the endpoint **is** the resource
→ `allocation` mode, one implicit member, its pilot starts at join.
Unset means a login node → `login` mode, one `--member` per pilot shape,
pilots submitted on demand. Every script prints what it found:

```
[12:37:16] resource: perlmutter (host perlmutter, site NERSC)
[12:37:16] mode    : allocation -- inside a Slurm allocation (SLURM_JOB_ID=12345)
[12:37:16] scratch : /pscratch/sd/m/merzky/atomic-demo
```

`local_a`/`local_b`/`local_c` and `r3` have a fixed mode by nature (fake
resources on a laptop; a workstation with no batch system), so detection
only drives `perlmutter` and `odo`.

**The recommended remote path is an allocation**: `salloc` (or
`srun --pty`), then `join.sh perlmutter` inside it. Login mode needs a
broker host that can reach that site's batch system — psij detects the
executor on the **broker** host, and r3 has no Slurm — so the login-mode
member declarations carry `TODO(...)` placeholders and `join.sh` refuses
them until they are filled in. The allocation shape has no placeholders
at all once `$PSCRATCH` / `$MEMBERWORK` is set.

## How the stack is pinned and installed

The demo does **not** run "whatever is checked out on this host". On
2026-09-08 the broker host `three` had `radical.orbit` on `devel`, the
old `broker.sh` installed *that*, and the broker died with
`No plugin matches 'federation'`. So:

**The pins** (`env.sh`) — these two refs *are* the demo stack, on the
broker host, on every resource host and on the laptop:

| variable | default |
|---|---|
| `ATOMIC_DEMO_ORBIT_REPO` | `git+ssh://git@github.com/radical-cybertools/radical.orbit.git` |
| `ATOMIC_DEMO_ORBIT_REF` | `feature/atomic-federation` |
| `ATOMIC_DEMO_ATOMIC_REPO` | `git+ssh://git@github.com/radical-collaboration/ATOMIC.git` |
| `ATOMIC_DEMO_ATOMIC_REF` | `feature/demo-wm` |
| `ATOMIC_DEMO_PIP_PINS` | `rhapsody-py==0.4.0 opentelemetry-sdk==1.43.0` |

The two PyPI pins are not cosmetic. `radical.orbit` requires a bare
`rhapsody-py`, so a fresh venv resolves the newest release — and
rhapsody does not declare its `opentelemetry` dependency at all, so a
pilot's session init dies with `No module named 'opentelemetry'` and
**every task FAILS**. The laptop only ever worked because something else
had pulled opentelemetry in. Found on 2026-09-08 building a venv from
scratch; pinned here to what the demo was validated with.

Both refs are pushed. **The demo date directory is the pin**: when the
refs move on, a later demo gets its own `demo/<date>/` rather than this
one silently changing meaning.

**No ssh key on the host?** Override the transport:

```bash
export ATOMIC_DEMO_ORBIT_REPO=https://github.com/radical-cybertools/radical.orbit.git
export ATOMIC_DEMO_ATOMIC_REPO=https://github.com/radical-collaboration/ATOMIC.git
```

**The venv** — `$ATOMIC_DEMO_VE`, defaulting to `$ORBIT_SRC/ve3` where
that exists (the laptop) and `~/.atomic-demo/ve` otherwise (any other
host). `ensure_stack` creates it with `python3 -m venv` if it is missing
and refuses a python older than 3.10 with a clear message
(`$ATOMIC_DEMO_PYTHON` picks the interpreter). Both packages are
installed **non-editable**; nothing is ever put on `PYTHONPATH`. The
broker is `$VE/bin/radical-orbit-broker.py`, the CLIs are `$VE/bin/*`,
and the pilots already ran the installed code — so all roles run the
same bits.

**Where a package is installed from**, decided per package by
`ensure_stack`:

| condition | source |
|---|---|
| a local checkout exists (`$ORBIT_SRC` for orbit, the checkout these scripts live in for atomic-wm), is on the pinned ref, and has no modified tracked files — and `ATOMIC_DEMO_FORCE_CLONE` is not `1` | that checkout (developer convenience; what the laptop does) |
| anything else — wrong branch, dirty tree, no checkout, `ATOMIC_DEMO_FORCE_CLONE=1` | the demo's **own clone** of the pinned ref under `$ATOMIC_DEMO_SRC` (default `/tmp/atomic-demo/src/`) |

Your checkouts are **never** fetched, checked out, stashed or otherwise
touched — they are read, and used or declined. When one is declined the
run says why (`… is on 'devel', the demo pins 'feature/atomic-federation'`).

The clone is made once (`git clone --branch <ref>`) and brought up to
date with `git pull --ff-only` on every run, so a rerun picks up a new
commit on the pinned branch. A pull that fails — no network on a login
node — is a **warning**: the commit already on disk is still a pinned
stack. A first clone that fails is an error, and names the https
override.

**The stamp** — `$ATOMIC_DEMO_VE/atomic-demo.stamp` records, per package,
the ref, the resolved commit, the source and when it was installed. `pip`
runs only when that commit moved (or the venv lacks the package, or
`--reinstall` was given), so `join.sh` and `submit.sh` cost one `git
pull` and one `git rev-parse` when nothing changed:

```
[09:41:02] install : radical.orbit up to date (feature/atomic-federation 21482d9a3ce8, checkout)
```

`--reinstall` forces `pip` anyway; `--skip-install` skips the whole step
(and is what `up.sh` passes to its three joins, because `broker.sh` has
just done it). `check_env.sh` prints the venv, the installed versions,
the stamp against the pins, the state of both checkouts and both clones,
and whether the pinned refs are reachable. All install output goes to
`demo/2026_09_08/run/install.log`.

## Running it — by hand, one terminal per role

This is the on-stage sequence. Every step prints what it did and what to
do next.

```bash
demo/2026_09_08/check_env.sh              # optional: are we ready?
```

**Terminal 1 — the broker.** `ensure_stack` first (it prints the resolved
commits), then the broker; returns once it answers, and the broker keeps
running in the background with a pidfile.

```bash
demo/2026_09_08/broker.sh                 # --skip-install to skip the stack
                                          # check, --reinstall to force pip
```

**Terminal 2 — the resources that are up before the audience arrives.**
Each call joins one resource, detached, and returns.

```bash
demo/2026_09_08/join.sh local_a
demo/2026_09_08/join.sh local_b
```

**The live moment — joining a resource on camera.** `--live` runs
`atomic-join` in the *foreground*: the join scrolls by, the federation
grows by one row in the Explorer, and the process stays there until
Ctrl-C leaves the federation again. It therefore cannot print the
resource table afterwards, and says so — watch it from a third terminal
with `atomic-resources` (after `source demo/2026_09_08/env.sh`).

```bash
demo/2026_09_08/join.sh local_c --live    # Ctrl-C to leave again
```

**The client.** `--wait` polls until the campaign is terminal and then
prints the collected results; without it, it prints the campaign id and
returns.

```bash
demo/2026_09_08/submit.sh --wait          # or --sweep temperature=300,600
```

Or drive the same campaign from the Explorer's ATOMIC page
(`https://127.0.0.1:8010/`) — the form submits the same spec and sweep.

**Teardown**, whichever way the demo was run:

```bash
demo/2026_09_08/down.sh                   # leave, stop, restore, keep logs
```

## Running it — the automated path

`up.sh` is exactly the sequence above with the three joins detached, plus
the wait-for-resources step. It is what a rehearsal (and CI) uses:

```bash
demo/2026_09_08/up.sh                              # broker + 3 federated resources
$ATOMIC_DEMO_VE/bin/python demo/2026_09_08/smoke.py   # submit a campaign, assert it
demo/2026_09_08/down.sh                            # leave, stop, restore, keep logs
```

(`$ATOMIC_DEMO_VE` is the demo venv — `source demo/2026_09_08/env.sh`
first, or spell it out; on this laptop it is
`/home/merzky/radical/radical.orbit/ve3`.)

`submit.sh` *runs* the demo, `smoke.py` *proves* it: same campaign, but
smoke.py asserts the seven claims listed below and exits non-zero naming
the one that broke.

## What comes up

One broker hosting three plugins (`task_dispatcher`, `federation`,
`atomic_campaign`) plus three endpoints joined as federated resources —
**five members in two capability class pools**:

| pool      | member            | site    | size        | software        | node-hours |
|-----------|-------------------|---------|-------------|-----------------|-----------:|
| `fed-cpu` | `local_a.default` | Rutgers | 1 × 4c      | lammps          | derived    |
| `fed-cpu` | `local_b.cpu`     | NERSC   | 1 × 2c      | lammps, pytorch | 2          |
| `fed-cpu` | `local_c.cpu`     | PSC     | 1 × 2c      | pytorch         | 2          |
| `fed-gpu` | `local_b.gpu`     | NERSC   | 1 × 1c + 1g | pytorch         | 1          |
| `fed-gpu` | `local_c.gpu`     | PSC     | 1 × 1c + 1g | pytorch         | 1          |

A dispatcher pool is a **capability class**, not a site: a resource
declares one *member* per shape of pilot it is willing to run, and each
member joins the pool for its class. `local_a` joins in `allocation`
mode and therefore has exactly one implicit member; `local_b` and
`local_c` join in `login` mode with two members each
(`atomic-join --member NAME:key=value,…`, see `docs/join.md`).

**The routing story.** `md` requires lammps and no GPU, so it goes to
`fed-cpu` and can only land on `local_a.default` or `local_b.cpu` —
`local_c.cpu` has no lammps. `train` requires pytorch and one GPU, so it
goes to `fed-gpu`, whose two members sit at two different "sites". The
**dispatcher**, not the federation, decides which of them each task gets:
the federation picks a *class*, the dispatcher picks the member.

**Why two GPU members, and why `train` takes 30 s.** Each GPU member
declares one core and `max_pilots=1`, so it runs one `train` task at a
time and the three sweep points cannot all run concurrently on one
member. But capacity alone does not *force* the second member to be
used: the dispatcher grows its pilot while the first member is busy, so
that pilot has to become active within **2 × the stage duration** of the
first one. `train` therefore runs with `--duration-sec 30`, which leaves
roughly 60 s of margin against a locally measured pilot warm-up of
5–15 s. That is what makes `smoke.py --min-gpu-members 2` (the default)
a fair assertion rather than a race; lower it to `1` on a federation
that genuinely has one GPU member.

The "GPU" is fake (psij `local`, rhapsody backend `concurrent`) and
nothing reserves it: a declared GPU is a *routing* statement this round,
not an exclusivity guarantee. Only the declaration matters for placement.

`allocation` mode means "this endpoint is the resource": the whole
allocation is one pool with one pilot, started at join time. `login` mode
means "this endpoint sits on a login node": each member declares a queue,
an account, a pilot size and a node-hour budget, and pilots are submitted
on demand (locally through psij's `local` executor) when work arrives —
so only `local_a` has a pilot before the campaign starts.

## What you should see

`up.sh` prints one line per step and ends with the resource table:

```
[00:12:01] pins     : radical.orbit@feature/atomic-federation, atomic-wm@feature/demo-wm
[00:12:03] install : radical.orbit up to date (feature/atomic-federation 21482d9a3ce8, checkout)
[00:12:04] install : atomic-wm from clone /tmp/atomic-demo/src/ATOMIC (feature/demo-wm 38e4548e8239)
[00:12:30] stack   : radical.orbit feature/atomic-federation @ 21482d9a3ce8 (checkout /home/merzky/radical/radical.orbit)
[00:12:30] stack   : atomic-wm feature/demo-wm @ 38e4548e8239 (clone /tmp/atomic-demo/src/ATOMIC)
[00:12:31] state   : dispatcher state moved to demo/2026_09_08/run/state.bak-…
[00:12:32] broker  : up (pid 41234), GET /endpoints answers 200
[00:12:33] join    : local_a (log: demo/2026_09_08/run/join-local_a.log)
…
[00:13:02] wait    : 3 resources federated, pilots up on local_a
[00:13:02] Explorer : https://127.0.0.1:8010/
```

Open the Explorer URL in a browser (accept the self-signed certificate
warning) — the ATOMIC page shows the federation and, once `smoke.py`
runs, the campaign filling in live.

`smoke.py` submits `examples/workflow_vacancy.json` with the sweep
`temperature=300,600,900`, waits for the campaign, and prints a placement
table plus the accuracies:

```
workflow  temperature  stage  state  resource  member   pool     task
--------  -----------  -----  -----  --------  -------  -------  ----------------------
wf-000    300          md     DONE   local_a   default  fed-cpu  cmp-b6b15c84-wf-000-md
wf-000    300          train  DONE   local_c   gpu      fed-gpu  cmp-b6b15c84-wf-000-train
wf-001    600          train  DONE   local_b   gpu      fed-gpu  cmp-b6b15c84-wf-001-train
…
[01:19:31] accuracy : temperature=300    wf-000  final_accuracy=0.965761
[01:19:31] accuracy : temperature=600    wf-001  final_accuracy=0.907226
[01:19:31] accuracy : temperature=900    wf-002  final_accuracy=0.828262
[01:19:31] OK       : campaign cmp-b6b15c84, 3 workflows, 6 stages, 3 resources, 4 members, 24s
```

Measured on this laptop **before** the class-pool change: `up.sh` ~14 s
including both pip installs, campaign wall time ~24 s, `down.sh` ~3 s.
The numbers after it are **not measured yet** — pending the next
acceptance run. Expect the campaign to take noticeably longer: five
members mean up to five pilots on one laptop, and the two GPU members
serialise three 30 s `train` tasks two at a time.  The accuracies are deterministic —
the same three numbers come back on every run, because the workload seeds
itself from its own parameters.

It asserts, and exits 1 naming the assertion if any of it is untrue:

1. the campaign reached `DONE`,
2. three workflows, all `DONE`, every stage `DONE` and placed,
3. every stage ran in the pool of its class (`md` → `fed-cpu`,
   `train` → `fed-gpu`) on a member advertising the software it requires
   (`md` → `lammps`, `train` → `pytorch`) and, for `train`, declaring a
   GPU,
4. at least two distinct resources and two distinct members were used
   (`--min-resources`, `--min-members`),
5. the three `train` stages spread over at least two GPU members
   (`--min-gpu-members`, default 2) — one member taking all of them means
   the class pool did not balance,
6. `final_accuracy` strictly decreases with temperature (the fake trainer
   guarantees this, so a violation means results were mixed up),
7. the central store holds all six JSON outputs and a `manifest.json` per
   stage.

Budget: the whole smoke run should stay under five minutes.

`smoke.py` needs no environment of its own: the scripts export into their own
subshell, so when `$RADICAL_ORBIT_BROKER_URL` is unset `smoke.py` reads
the handful of variables it needs back out of `env.sh`. Sourcing `env.sh`
first still wins — an explicitly set variable is never overridden.

## Where things are

| what | where |
|---|---|
| broker log | `demo/2026_09_08/run/broker.log` |
| join logs (detached joins; `--live` writes to the terminal) | `demo/2026_09_08/run/join-<resource>.log` |
| what `submit.sh` submitted | `demo/2026_09_08/run/submit.log` |
| endpoint logs (incl. pilot start-up) | `/tmp/atomic-demo/endpoints/<resource>/endpoint.log` |
| the joined record of a resource (members, pools, member ids — what `atomic-leave` matches its pilots with) | `/tmp/atomic-demo/endpoints/<resource>/record.json` |
| campaign + results payloads from the last smoke run | `demo/2026_09_08/run/smoke-campaign.json`, `smoke-results.json` |
| install output (clone, pull, pip) | `demo/2026_09_08/run/install.log` |
| what the venv holds (ref, commit, source) | `$ATOMIC_DEMO_VE/atomic-demo.stamp` |
| the demo's own clones of the pinned refs | `/tmp/atomic-demo/src/{radical.orbit,ATOMIC}` |
| federation / campaign plugin state | `/tmp/atomic-demo/state/` |
| central result store | `/tmp/atomic-demo/store/<campaign>/<workflow>/<stage>/` |
| task scratch | `/tmp/atomic-demo/<resource>/` |
| dispatcher state backup | `demo/2026_09_08/run/state.bak-<timestamp>` |

The demo writes into exactly two places: `/tmp/atomic-demo` and
`demo/2026_09_08/run`. The one exception is the task dispatcher's state
directory (`~/.radical/orbit/task_dispatcher/state`), which has no
environment override and whose stale sessions would be replayed at broker
start — `broker.sh` moves it into `demo/2026_09_08/run/state.bak-<ts>` and
`down.sh` moves it back.

`demo/2026_09_08/run/` is scratch: it is safe to delete between runs and should
not be committed.

## Options

```
broker.sh --resource N   which host this broker is for (local | r3;
                         default $ATOMIC_DEMO_RESOURCE, else local)
          --skip-install skip the stack check entirely (fast iteration)
          --reinstall    pip install both packages even on a stamp match
          --plugins LIST broker-hosted plugin set (default:
                         task_dispatcher,federation,atomic_campaign)
                         Refuses to start a second broker while the
                         pidfile of a live one is around.

join.sh RESOURCE         local_a | local_b | local_c | r3 |
                         perlmutter | odo — also env.sh's parameter, so
                         it selects the broker, site, scratch base and
                         join arguments (env.sh, demo_join_args).
                         Refuses a resource whose arguments still carry
                         a TODO(...) placeholder.
        --live           foreground join (Ctrl-C leaves again); without
                         it the endpoint is detached and join.sh returns
        --skip-install   skip the stack check
        --reinstall      force pip

submit.sh --resource N  which broker to talk to (default local)
          --sweep K=V,V  parameter sweep (default temperature=300,600,900)
          --spec PATH    workflow spec (default examples/workflow_vacancy.json)
          --wait         poll to the end, then print the results;
                         exit 0 only on DONE
          --timeout SEC  budget for --wait (default 600)
          --skip-install skip the stack check
          --reinstall    force pip

up.sh   --skip-install   skip the stack check (fast iteration)
        --reinstall      force pip
        --no-join        broker only, no resources
        --plugins LIST   broker-hosted plugin set (default:
                         task_dispatcher,federation,atomic_campaign)

smoke.py --timeout SEC   campaign budget (default 600)
         --sweep K=V,V   parameter sweep (default temperature=300,600,900)
         --spec FILE     workflow spec (default examples/workflow_vacancy.json)
         --min-resources N     distinct resources required (default 2)
         --min-members N       distinct members required (default 2)
         --min-gpu-members N   GPU members the GPU stages must spread
                               over (default 2; 1 disables the check)
         --no-store-check   skip the store assertions (broker on another host)

down.sh --resource N     which host to tear down (default local)
        --all-endpoints  kill every radical-orbit-endpoint process, not
                         only this demo's (careful: you may run others)
        --wipe           also remove /tmp/atomic-demo (but not
                         /tmp/atomic-demo/src — the clones are a cache)
```

The stack knobs, all read from the environment (see `env.sh`):

```
ATOMIC_DEMO_RESOURCE             the resource, when no flag is given
ATOMIC_DEMO_BROKER_HOST          the host clients dial (default: radical.3)
ATOMIC_DEMO_BROKER_BIND          the address broker.sh listens on (0.0.0.0 on r3)
ATOMIC_DEMO_SCRATCH_BASE         where task scratch goes on this machine
ATOMIC_DEMO_ORBIT_REPO / _REF    the radical.orbit pin
ATOMIC_DEMO_ATOMIC_REPO / _REF   the atomic-wm pin
ATOMIC_DEMO_PIP_PINS             extra pinned PyPI requirements
ATOMIC_DEMO_VE                   the venv everything is installed into
ATOMIC_DEMO_SRC                  where the demo keeps its own clones
ATOMIC_DEMO_FORCE_CLONE=1        ignore local checkouts, always clone
ATOMIC_DEMO_HOME                 where venv + clones live on an HPC site
                                 (perlmutter: $SCRATCH/demo, odo:
                                 $HOME/tmp/demo; home dirs are quota'd)
ATOMIC_DEMO_PYTHON               interpreter used to create the venv
ATOMIC_DEMO_PYTHON_MODULE        environment module loaded first (perlmutter:
                                 python/3.12-26.1.0, odo: cray-python)
```

`source demo/2026_09_08/env.sh` in your own shell to get the same environment
by hand — after that `atomic-resources`, `atomic-campaign status <cid>`
and friends talk to the demo broker with no flags. Do that before running
`atomic-leave` manually, or it will look for pidfiles in the wrong place
(`env.sh` moves `atomic-join`'s state root to `/tmp/atomic-demo/endpoints`
via `$ATOMIC_WM_STATE`, so a demo run leaves nothing in `~/.radical`).

`env.sh` also exports `ATOMIC_TOOL_PREFIX=$VE/bin`: the campaign runner
uses it to turn a bare `atomic-fake-md` into an absolute path, so a pilot
whose python environment lacks `atomic-wm` still finds the workload
tools. The broker process is the one that needs it — it builds the task
command — and inherits it from `broker.sh`.

## Before a cross-host run (Tuesday) — two non-negotiables

1. **Every remote member must declare `shared_fs=false` and an explicit
   `scratch_base`** in its `--member` spec. A member without them inherits the
   resource's scratch, which for a login-mode resource falls back to a
   *broker-local* path: the broker would then copy inputs and `mkdir` a cwd
   on its own host and the task would run remotely with a bogus cwd and no
   inputs — silently.
2. **The pilot's psij executor is detected on the broker host**, not on the
   member's endpoint. A broker on a non-Slurm host (r3) submits every
   login-mode pilot with the `local` executor — no `sbatch`. Use
   **allocation mode** for remote resources (start an `salloc` / `srun
   --pty` and run `join.sh` inside it — the endpoint is then in the
   allocation and a local launch there is exactly right), or run the
   broker on a Slurm-visible login node. Login mode from a non-Slurm
   broker host is not supported yet (per-member executor override is a
   known follow-up) — which is why `join.sh` refuses the login-mode
   templates until their `TODO(...)`s are filled in.

## Troubleshooting

**The broker starts but says `No plugin matches 'federation'`.** The venv
holds a radical.orbit that is not the pinned one — the 2026-09-08 failure
on the broker host `three`. `check_env.sh` names it (the stamp section and
the install-sources section); `broker.sh --reinstall` fixes it. If a local
checkout is on the wrong branch the run says so and clones instead, so
this should no longer be reachable without `--skip-install`.

**`ensure_stack` says it is installing from a clone, and you expected
your checkout.** It prints the reason on the line above: wrong branch,
uncommitted changes to tracked files, no checkout, or
`ATOMIC_DEMO_FORCE_CLONE=1`. Commit (or stash) and re-run — the demo
never touches your working tree itself.

**`could not clone … no ssh key on this host?`** Export the https
overrides (see "How the stack is pinned and installed") and re-run.

**`broker.sh` (or `up.sh`) says the broker died during startup.** Read
`demo/2026_09_08/run/broker.log`. Usual causes: the TLS key is more permissive
than `0600` (the broker refuses to start), a plugin listed in `--plugins`
is not installed (`federation` lives in radical.orbit, `atomic_campaign`
in this repo — `broker.sh` installs both from the pinned refs; try
`--reinstall`, and check `run/install.log`), or
port 8010 is taken (`demo/2026_09_08/check_env.sh`).

**`atomic-join` fails or the endpoint never connects.** Read
`demo/2026_09_08/run/join-<name>.log` and
`/tmp/atomic-demo/endpoints/<name>/endpoint.log`. The classic causes are
the two environment rules `env.sh` exists for: `RADICAL_LOG_LVL` set to
something the endpoint's `--log-level` rejects (e.g. `DEBUG_9`), and
`$PATH` without `$ATOMIC_DEMO_VE/bin`, which makes psij fail to find
`radical-orbit-endpoint-wrapper.sh` when it launches a pilot.

**A TLS error mentioning "certificate is not valid for '127.0.0.1'".**
The broker certificate is `CN=localhost.localdomain` with no
subjectAltName, so it does not match the address the demo dials.
radical.orbit's own clients handle this by *pinning* the cert and
switching hostname matching off (`runtime.py::_tls_context`: a pinned
cert authenticates the peer on its own), and `atomic_wm.client.Client`
does the same — so the CLIs and `smoke.py` are fine. `curl` has no such
switch, which is why every `curl` in these scripts carries `-k`. If you
write your own client, pin the cert with `check_hostname = False`; do not
reach for `verify=False`.

**Resources never report `pilots_active >= 1`.** A pilot needs 60–90 s to
warm up on a cold broker; `up.sh` budgets 90 s. Beyond that, look for the
pilot's own endpoint (`fed-<class>_<resource>.<member>_<pid>`, or
`fed-<name>_<pid>` for a pool that is not a class pool) in the endpoint
log and in
`pgrep -af radical-orbit`. Never time out on *pilot* state in your own
scripts — only on task state.

**`smoke.py` times out.** It polls campaign state only, so a timeout means
a task is stuck, not that a pilot is slow. `demo/2026_09_08/run/broker.log`
shows the dispatcher's view; `smoke-campaign.json` shows how far each
workflow got.

**`down.sh` reports surviving processes.** psij's `local` executor cancels
only the wrapper job, so a pilot endpoint can outlive its pool and keep
redialing the broker. `down.sh` sweeps processes matching this demo's
names; `--all-endpoints` widens that to every orbit endpoint on the
machine (only do that if none of them are yours).

## Mapping to the real resources

The demo code does not know it is running on localhost, it installs the
*same pinned stack* on every host, and the resource is one parameter — so
the real run is **these very scripts**, with a different name.

The three real resources, all declared in `env.sh` (`demo_join_args`),
live and selectable, no editing required:

| name | what | mode | placeholders |
|---|---|---|---|
| `r3` | the Rutgers workstation, and the broker host | allocation (a workstation has no batch system) | none |
| `perlmutter` | NERSC | detected | none inside an allocation; the login-mode members carry `TODO(...)` |
| `odo` | OLCF's Slurm test system | detected | same |

### On the broker host (r3)

```bash
demo/2026_09_08/check_env.sh --resource r3   # optional
demo/2026_09_08/broker.sh    --resource r3   # installs the pinned stack, starts
demo/2026_09_08/join.sh      r3              # r3 joins itself
```

`broker.sh` binds all interfaces and ends with a `remote :` block: the
`scp` of the broker cert that Perlmutter and Odo need (they dial
radical.3 by default, nothing to export).

### On Perlmutter / Odo

Clone this repo (or copy `demo/2026_09_08/` plus `bootstrap.sh`), copy
the broker's `broker_cert.pem` to `~/.radical/orbit/` there (or export
`RADICAL_ORBIT_BROKER_CERT`), then:

```bash
# no ssh key for GitHub on this host?  then:
#   export ATOMIC_DEMO_ORBIT_REPO=https://github.com/radical-cybertools/radical.orbit.git
#   export ATOMIC_DEMO_ATOMIC_REPO=https://github.com/radical-collaboration/ATOMIC.git

salloc -N 1 ...                                   # <- the recommended path
demo/2026_09_08/join.sh perlmutter                # inside the allocation
```

`join.sh` creates the venv and installs the pinned stack itself — no
`bootstrap.sh` step and no pre-existing checkout needed, only `git` and a
python ≥ 3.10. It prints the mode it detected before it does anything.

Inside the allocation there is nothing to fill in: the endpoint **is**
the resource, capabilities are detected (`sysinfo`, and
`queue_info`/`job_allocation` for the allocation's size and remaining
walltime), the pilot starts at join, and `--scratch` comes from
`$PSCRATCH` / `$MEMBERWORK`. Set `ATOMIC_DEMO_SCRATCH_BASE` if neither is
right.

On a **login node** the same command selects login mode instead, and
refuses:

```
[12:37:16] mode    : login -- no SLURM_JOB_ID -- this looks like a login node
[12:37:16] ERROR: the login-mode join arguments for 'perlmutter' still carry a
placeholder: cpu:queue=TODO(CPU queue/partition),… -- fill in every TODO(...) in
demo_join_args (demo/2026_09_08/env.sh) first
```

That is deliberate: **login mode does not work from the r3 broker at
all** (see the gaps below), so the placeholders are a stop sign, not a
chore. Fill them in only when the broker moves onto a Slurm-visible login
node; each member then also needs `shared_fs=false` and an explicit
`scratch_base=`, which the templates already carry.

The `cpu` member joins `fed-cpu`, the `gpu` member joins `fed-gpu`
alongside every other site's GPU member — that is the whole point of the
class pools. The flat `--queue/--nodes/--cpus/…` flags still describe a
single-member resource and cannot be combined with `--member`.

Note the tension `env.sh` spells out at the join templates:
`shared_fs=false` and an explicit `scratch_base=` are declarable **only
per `--member`**, i.e. only in login mode — and `--member` is an error in
allocation mode, where the one implicit member is built with
`shared_fs=True` hard-coded (`plugin_federation.py::_implicit_member`).
That is harmless exactly when the endpoint runs inside the allocation and
the tasks it launches see the `--scratch` it declared, which is why the
allocation path is the recommended one.

Suggested choreography for the live demo: **join Perlmutter and Odo
beforehand** (an allocation can take minutes to start, and a queued pilot
is not a good stage moment), then **join `r3` live** — it is a
workstation, joins in seconds, and the Explorer shows the resource table
growing by one row while the audience watches. Leave it live and submit
the campaign afterwards, so the new resource actually takes work.

Two known gaps for a cross-host run, both recorded in the plans:

- pilots are submitted by psij **on the broker host**, so `login`-mode
  joins only work if the broker host can reach that batch system; the
  broker sits on r3, which has no Slurm, so `allocation` mode is the only
  supported shape for Perlmutter and Odo this round,
- inputs for stage *k+1* now travel **in the submit body** (`inputs_b64`)
  and the dispatcher places them wherever the task lands, so they no
  longer assume a shared filesystem — but base64 in a JSON body is ~1.33×
  the file size, which is right for the demo's few-KB JSON and wrong for
  a multi-MB restart file; output collection goes through the pilot's own
  `staging` plugin first, so results come back either way.

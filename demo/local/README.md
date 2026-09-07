# The ATOMIC WM demo, on one laptop

Three commands bring the whole demo up on localhost, prove it works, and
take it down again:

```bash
demo/local/check_env.sh                       # optional: are we ready?
demo/local/up.sh                              # broker + 3 federated resources
ve3/bin/python demo/local/smoke.py            # submit a campaign, assert it
demo/local/down.sh                            # leave, stop, restore, keep logs
```

Everything runs against `https://127.0.0.1:8010`. Tuesday's run on real
machines is the *same code* with different `atomic-join` arguments — see
"Mapping to the real resources" at the end.

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

The two GPU members declare one core and `max_pilots=1`, so each of them
runs one `train` task at a time. With three sweep points the class pool
therefore has to use both — which is what `smoke.py --min-gpu-members`
asserts, and what would silently not happen if `fed-gpu` had a single
member.

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
[00:12:03] install : radical.orbit from /home/merzky/radical/radical.orbit
[00:12:31] state   : dispatcher state moved to demo/local/run/state.bak-…
[00:12:32] broker  : up (pid 41234), GET /endpoints answers 200
[00:12:33] join    : local_a (log: demo/local/run/join-local_a.log)
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

Measured on this laptop before the class-pool change: `up.sh` ~14 s
including both pip installs, campaign wall time ~24 s, `down.sh` ~3 s.
Five members mean up to five pilots on one laptop and the GPU members
serialise the three `train` tasks two at a time, so the campaign now
takes longer — watch that number.  The accuracies are deterministic —
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

`smoke.py` needs no environment of its own: `up.sh` exports into its own
subshell, so when `$RADICAL_ORBIT_BROKER_URL` is unset `smoke.py` reads
the handful of variables it needs back out of `env.sh`. Sourcing `env.sh`
first still wins — an explicitly set variable is never overridden.

## Where things are

| what | where |
|---|---|
| broker log | `demo/local/run/broker.log` |
| join logs | `demo/local/run/join-<resource>.log` |
| endpoint logs (incl. pilot start-up) | `/tmp/atomic-demo/endpoints/<resource>/endpoint.log` |
| campaign + results payloads from the last smoke run | `demo/local/run/smoke-campaign.json`, `smoke-results.json` |
| pip output | `demo/local/run/pip.log` |
| federation / campaign plugin state | `/tmp/atomic-demo/state/` |
| central result store | `/tmp/atomic-demo/store/<campaign>/<workflow>/<stage>/` |
| task scratch | `/tmp/atomic-demo/<resource>/` |
| dispatcher state backup | `demo/local/run/state.bak-<timestamp>` |

The demo writes into exactly two places: `/tmp/atomic-demo` and
`demo/local/run`. The one exception is the task dispatcher's state
directory (`~/.radical/orbit/task_dispatcher/state`), which has no
environment override and whose stale sessions would be replayed at broker
start — `up.sh` moves it into `demo/local/run/state.bak-<ts>` and
`down.sh` moves it back.

`demo/local/run/` is scratch: it is safe to delete between runs and should
not be committed.

## Options

```
up.sh   --skip-install   skip the two pip installs (fast iteration)
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

down.sh --all-endpoints  kill every radical-orbit-endpoint process, not
                         only this demo's (careful: you may run others)
        --wipe           also remove /tmp/atomic-demo
```

`source demo/local/env.sh` in your own shell to get the same environment
by hand — after that `atomic-resources`, `atomic-campaign status <cid>`
and friends talk to the demo broker with no flags. Do that before running
`atomic-leave` manually, or it will look for pidfiles in the wrong place
(`env.sh` moves `atomic-join`'s state root to `/tmp/atomic-demo/endpoints`
via `$ATOMIC_WM_STATE`, so a demo run leaves nothing in `~/.radical`).

`env.sh` also exports `ATOMIC_TOOL_PREFIX=$VE/bin`: the campaign runner
uses it to turn a bare `atomic-fake-md` into an absolute path, so a pilot
whose python environment lacks `atomic-wm` still finds the workload
tools. The broker process is the one that needs it — it builds the task
command — and inherits it from `up.sh`.

## Troubleshooting

**`up.sh` says the broker died during startup.** Read
`demo/local/run/broker.log`. Usual causes: the TLS key is more permissive
than `0600` (the broker refuses to start), a plugin listed in `--plugins`
is not installed (`federation` lives in radical.orbit, `atomic_campaign`
in this repo — `up.sh` installs both; try without `--skip-install`), or
port 8010 is taken (`demo/local/check_env.sh`).

**`atomic-join` fails or the endpoint never connects.** Read
`demo/local/run/join-<name>.log` and
`/tmp/atomic-demo/endpoints/<name>/endpoint.log`. The classic causes are
the two environment rules `env.sh` exists for: `RADICAL_LOG_LVL` set to
something the endpoint's `--log-level` rejects (e.g. `DEBUG_9`), and
`$PATH` without `ve3/bin`, which makes psij fail to find
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
a task is stuck, not that a pilot is slow. `demo/local/run/broker.log`
shows the dispatcher's view; `smoke-campaign.json` shows how far each
workflow got.

**`down.sh` reports surviving processes.** psij's `local` executor cancels
only the wrapper job, so a pilot endpoint can outlive its pool and keep
redialing the broker. `down.sh` sweeps processes matching this demo's
names; `--all-endpoints` widens that to every orbit endpoint on the
machine (only do that if none of them are yours).

## Mapping to the real resources

The demo code does not know it is running on localhost. For Tuesday,
replace the three `atomic-join` lines in `env.sh` (`demo_join_args`) with
the real ones. In every case: install first (`bootstrap.sh` does venv +
both installs on a bare login node), point `--broker` at the broker's URL,
and copy `broker_cert.pem` next to `~/.radical/orbit/` on that host (or
export `RADICAL_ORBIT_BROKER_CERT`).

**Perlmutter, inside an allocation** (run `atomic-join` from the batch
job / `salloc` shell — the whole allocation becomes one resource, and its
pilot starts at join):

```bash
atomic-join --broker https://<broker>:8010 --name perlmutter_a \
            --mode allocation --site NERSC --kind hpc \
            --software lammps,pytorch \
            --scratch $SCRATCH/atomic-demo/perlmutter_a --detach
```

Capabilities are detected (`sysinfo`, and `queue_info/job_allocation` for
the allocation's size and remaining walltime); add `--declare
cores=…,gpus=…,mem_gb=…` only to override what is detected, and
`--node-hours H` to cap what this join contributes.

**Bridges-2 (or any second site), from the login node** — pilots are
submitted on demand, so the queue, account, pilot size and budget are
declared, one `--member` per shape of pilot the site should run:

```bash
atomic-join --broker https://<broker>:8010 --name bridges_login \
            --mode login --site PSC --kind hpc \
            --member cpu:queue=RM,account=<acct>,nodes=1,cpus=128,\
walltime=3600,node_hours=20,software=lammps,site=PSC \
            --member gpu:queue=GPU,account=<acct>,nodes=1,cpus=64,gpus=8,\
walltime=3600,node_hours=8,software=pytorch,site=PSC \
            --scratch $PROJECT/atomic-demo/bridges --detach
```

The `cpu` member joins `fed-cpu`, the `gpu` member joins `fed-gpu`
alongside every other site's GPU member — that is the whole point of the
class pools. The flat `--queue/--nodes/--cpus/…` flags still describe a
single-member resource and cannot be combined with `--member`.

**radical.3 (the workstation)** — same shape as `local_a` (allocation
mode, one implicit member):

```bash
atomic-join --broker https://<broker>:8010 --name radical3 \
            --mode allocation --site Rutgers --kind workstation \
            --software lammps,pytorch \
            --scratch /tmp/atomic-demo/radical3 --detach
```

Suggested choreography for the live demo: **join Perlmutter and the
second site beforehand** (an allocation can take minutes to start, and a
queued pilot is not a good stage moment), then **join `radical.3` live**
— it is a workstation, joins in seconds, and the Explorer shows the
resource table growing by one row while the audience watches. Leave it
live and submit the campaign afterwards, so the new resource actually
takes work.

Two known gaps for a cross-host run, both recorded in the plans:

- pilots are submitted by psij **on the broker host**, so `login`-mode
  joins only work if the broker host can reach that batch system; on
  Tuesday the broker sits on a machine without Slurm, so prefer
  `allocation` mode for the remote sites,
- inputs for stage *k+1* now travel **in the submit body** (`inputs_b64`)
  and the dispatcher places them wherever the task lands, so they no
  longer assume a shared filesystem — but base64 in a JSON body is ~1.33×
  the file size, which is right for the demo's few-KB JSON and wrong for
  a multi-MB restart file; output collection goes through the pilot's own
  `staging` plugin first, so results come back either way.

# Joining a resource — `atomic-join`, `atomic-leave`, `atomic-resources`

One command turns a machine — a laptop, a login node, or a compute
allocation that is already running — into a resource of the ATOMIC
federation:

```sh
atomic-join --broker https://r3:8003 --name perlmutter_a --mode allocation \
            --site NERSC --software lammps,pytorch
```

`atomic-join` starts an ORBIT *endpoint* for you, waits until the broker
sees it, works out what the machine offers, registers it with the
broker's `federation` plugin and then stays in the foreground until you
press Ctrl-C — at which point the resource leaves the federation again
and the endpoint is stopped.  Nothing is left behind.

- `atomic-resources` shows what is currently federated.
- `atomic-leave <name>` tears down a resource joined with `--detach`.

---

## Resources, members and capability classes

A dispatcher pool is a **capability class** (`fed-cpu`, `fed-gpu`), not a
site.  A resource declares one **member** per shape of pilot it is
willing to run, and each member joins the pool for its class:

```
resource local_b ── members ──┬── local_b.cpu   class cpu → pool fed-cpu
                              └── local_b.gpu   class gpu → pool fed-gpu
resource local_a ── members ──── local_a.default class cpu → pool fed-cpu
```

- a **member short name** matches `^[a-z0-9][a-z0-9_-]*$` — **no dot**,
  because the dot in the member id is the separator;
- the **member id** the dispatcher sees is `<resource>.<member>`; a
  resource name *may* contain dots, so it always splits on the **last**
  one;
- the **class** is `class=…` when declared, else `gpu` when the member
  declares `gpus`, else `cpu`.  A class name that does not match
  `^[a-z0-9][a-z0-9_-]*$` is rejected, never lower-cased: silently
  turning `GPU` into `gpu` is how a second, invisible pool appears;
- `--mode allocation` is exactly **one** member (`default`) — the
  allocation *is* the resource — and `--member` is an error there;
- a login-mode join **without** `--member` also becomes one `default`
  member, built from the flat flags.  Every join that worked before
  class pools still works, unchanged.

A GPU in a member's size is a **declared** capability used for routing.
Nothing reserves it this round, so two GPU-tagged tasks can share a
one-GPU pilot.

---

## The two modes

| | `--mode allocation` | `--mode login` |
|---|---|---|
| where it runs | inside a compute allocation (or on a workstation) | on a login node |
| the resource is | the allocation itself — the whole thing | the right to submit pilots to a queue |
| size comes from | `sysinfo` + `queue_info/job_allocation` on the endpoint's machine, overridable with `--declare` | the flags you pass (`--nodes × --cpus`, `--nodes × --gpus-per-node`), overridable with `--declare` |
| pilots | one, started at join time, lives as long as the allocation | submitted on demand, up to `--max-pilots` (per member) |
| budget | `--node-hours`; without it the federation derives nodes × walltime from the allocation | `--node-hours`, **required** — unless `--member` carries one per member |
| members | exactly one, implicit (`default`) | one per `--member`, else one built from the flat flags |

Login mode does **not** probe the machine the endpoint runs on: a login
node's 8 cores say nothing about the 128-core pilots it would submit.

Both modes work on localhost — `login` mode then uses psij's `local`
executor, which is exactly what the demo does for a third resource.

### Examples

```sh
# a workstation / an interactive allocation, capabilities auto-detected
atomic-join --broker https://127.0.0.1:8013 --name local_a \
            --mode allocation --declare cores=4,gpus=0 --software lammps \
            --scratch /tmp/atomic-demo/local_a

# a login node which may submit to a partition (one implicit member)
atomic-join --broker https://psc:8003 --name bridges_login --mode login \
            --site PSC --queue RM --account abc123 \
            --nodes 1 --cpus 128 --walltime 3600 --node-hours 20 \
            --software lammps

# the same login node, offering a CPU *and* a GPU shape
atomic-join --broker https://psc:8003 --name bridges --mode login \
            --site PSC --kind hpc \
            --member cpu:queue=RM,account=abc123,nodes=1,cpus=128,\
walltime=3600,node_hours=20,software=lammps,site=PSC \
            --member gpu:queue=GPU,account=abc123,nodes=1,cpus=64,gpus=8,\
walltime=3600,node_hours=8,software=pytorch,mem_gb_per_node=256,site=PSC

# pre-join a resource for a demo and walk away
atomic-join --broker … --name local_b --mode allocation --detach
atomic-leave local_b
```

## `--member NAME:key=value,…`

Repeatable, login mode only, and **mutually exclusive** with the flat
`--queue/--account/--nodes/--cpus/--gpus-per-node/--walltime/--max-pilots`
flags (those describe exactly one member, so mixing the two would leave
it open which member they belong to), with `--software`, and with
`--declare cores=`/`gpus=` — a member carries its own software and size,
and the resource-wide capabilities are their sum. `--declare mem_gb=`
still applies to the resource; per-member memory is the
`mem_gb_per_node` attribute, which is what the dispatcher matches on.

| key | goes to | notes |
|---|---|---|
| `queue` | the member's batch queue | required; must not be `default` (the dispatcher's sentinel) |
| `account` | allocation account | optional |
| `nodes` | nodes per pilot | required, > 0 |
| `cpus` | `cpus_per_node` | required, > 0 |
| `gpus` | `gpus_per_node` | default 0 — **declared, not reserved** |
| `walltime` | `walltime_sec` | required, > 0 |
| `min_pilots` / `max_pilots` | pilot floor / ceiling | default 0 / 1 |
| `node_hours` | `budget.node_hours` | required, > 0 |
| `software` | the member's software list | always a list, see below |
| `class` | the capability class | default: `gpu` if `gpus` > 0, else `cpu` |
| `scratch` | `scratch_base` **on the member's host** | must lie under `$HOME` or `/tmp` |
| `shared_fs` | `true`/`false` | whether broker host and member share `scratch_base` |
| `backend` | `rhapsody_backend` | `concurrent` locally |
| *anything else* | `attributes[key]` | free-form labels the dispatcher matches on: `site=NERSC`, `mem_gb_per_node=256` |

**The one non-obvious rule.** The spec is split once on `:` into the
member name and the rest, and the rest is split on `,`.  A fragment
**without** `=` continues the previous key, turning its value into a
list:

```
--member gpu:queue=GPU,software=pytorch,jax,site=PSC
                       └──────────────────┘  one key, two values
```

so `software=pytorch,jax` is one list and `site=PSC` is still a scalar.
A *leading* fragment without `=` has no previous key and is an error
(`--member local_b:a,b: 'a' is not key=value`) rather than a silently
dropped token, and `software` is **always** normalised to a list — the
record shape never depends on how many tags were typed.  A scalar key
handed several values (`queue=RM,GPU`) is an error too.

A value becomes a number only when the number renders back to exactly
what was typed: `mem_gb_per_node=256` → `256` and `tier=1.5` → `1.5`,
while `007`, `1.50`, `1e3` and `0x10` stay **text**. Labels are matched
by equality, so rewriting the spelling would match something other than
what was written. Several values become a list of strings.

Keys are lower case. A key that only differs in case from a known one
(`Queue=RM`) is rejected rather than quietly filed as an attribute
nobody matches on.

`atomic-join` echoes every parsed member back on success — class, pool,
queue, size, budget, software, attributes — so a typo is visible rather
than silent.

## What `atomic-join` actually does

1. **Starts an endpoint.**  A child process
   `radical-orbit-endpoint.py --name ep_<name> --url <broker> [--token …]
   [--cert …] --plugins default`, run with the same python as the CLI.
   The endpoint has no log-file flag, so its stdout/stderr are redirected
   to `~/.radical/orbit/atomic/<name>/endpoint.log`.  Its environment is
   prepared for the pilots it will spawn: the venv's `bin` comes first on
   `$PATH` (psij resolves `radical-orbit-endpoint-wrapper.sh` by name),
   `RADICAL_ORBIT_LOG_LVL` is set, `RADICAL_LOG_LVL` and
   `RADICAL_ORBIT_LOG_FILE` are removed, and
   `RADICAL_ORBIT_RHAPSODY_BACKEND` is passed through (locally it must be
   `concurrent`).
2. **Waits for the endpoint to connect** — `GET /endpoints` until it
   shows up as connected, 60 s by default (`--connect-timeout`).
3. **Works out the capabilities.**  In allocation mode it asks the
   endpoint: `sysinfo` is session-scoped, so the CLI registers a session,
   reads `metrics/{sid}` (`cpu.cores_logical`, `memory.total`, `gpus`)
   and unregisters it again; it also reads the session-less
   `queue_info/job_allocation` (`n_nodes`, remaining `runtime`), which
   the federation uses to size the pool and derive the budget.  In login
   mode nothing is probed — the pilot description is the capability
   (`cores = nodes × cpus`, `gpus = nodes × gpus-per-node`).
   **`--declare` always wins**, per key: `--declare cores=4` keeps the
   detected GPU count and memory.  Detection is best effort — if
   `sysinfo` is not there, declare what matters (at least `cores`).
4. **Joins** — `POST /broker/federation/join/default` with the resource
   record (name, endpoint, mode, site, kind, capabilities, optional
   `budget` and `scratch_base`, and in login mode either a `members`
   list or the flat `pool` block).  With `--member` the resource-wide
   `capabilities` are the **aggregate** of the members — `cores` =
   Σ nodes × cpus, `gpus` = Σ nodes × gpus, `software` = the union — and
   `budget.node_hours` is the sum of the members' budgets unless
   `--node-hours` says otherwise.  The federation answers with the full
   record — each member with its server-filled `member_id`, `class` and
   `pool_name`, and the budget it settled on — which is what the CLI
   prints.
5. **Stays in the foreground**, reporting when the endpoint's connection
   changes.  On Ctrl-C (SIGINT) or SIGTERM it calls
   `leave/default/<name>` (with `{"cancel_tasks": false}` — a task this
   resource queued can still run on another member of its class pool),
   stops the endpoint, and kills pilot processes that outlived their
   pool.  With `--detach` it instead writes
   `~/.radical/orbit/atomic/<name>/endpoint.pid` and exits — use
   `atomic-leave <name>` for the same teardown later.

## Connection arguments (all three CLIs)

| flag | default | meaning |
|---|---|---|
| `--broker URL` | `$RADICAL_ORBIT_BROKER_URL` | the gateway; TLS is always on |
| `--token TOK` | `$RADICAL_ORBIT_TOKEN`, then `$RADICAL_ORBIT_BROKER_TOKEN`, then `~/.radical/orbit/broker.token` | may be empty for a `--no-auth` broker — no `Authorization` header is then sent |
| `--cert PATH` | `$RADICAL_ORBIT_BROKER_CERT`, then `~/.radical/orbit/broker_cert.pem` | the broker's certificate, used to verify the connection |

These are the same fallbacks `radical-orbit-endpoint.py` itself uses, so
on a machine that is already set up for ORBIT neither flag is needed.

A certificate given here is **pinned**: it becomes the only trust root
for the connection, and the hostname in it is not checked — which is
what `radical.orbit`'s own endpoints do, because the self-signed broker
certificates carry no name for the address a client actually dials
(`127.0.0.1`, an FQDN, a tunnel).  Without `--cert` the system trust
store is used with normal hostname verification.

Federation routes always use the reserved session id `default`; the CLIs
never register a federation session of their own.

## Other arguments worth knowing

| flag | meaning |
|---|---|
| `--member NAME:k=v,…` | declare one member (login mode, repeatable) — see above |
| `--declare cores=,gpus=,mem_gb=` | override detected capabilities (comma separated) |
| `--software a,b` | what is installed here; repeatable.  The campaign matches stage requirements against this list |
| `--node-hours H` | the budget this join contributes (required in login mode; in allocation mode the federation derives one from the allocation if you leave it out) |
| `--scratch DIR` | task scratch base; must lie under `$HOME` or `/tmp` (ORBIT staging rule).  Default: `~/.radical/orbit/federation/scratch/<name>` |
| `--kind hpc\|cluster\|workstation` | free-form classification shown in the UI |
| `--endpoint NAME` | endpoint name to use instead of `ep_<name>` |
| `--plugins LIST` | endpoint plugins (default: the role's default set) |
| `--endpoint-bin PATH` | where `radical-orbit-endpoint.py` lives, if not on `$PATH` (also `$ATOMIC_ENDPOINT_BIN`) |
| `--log-level` | endpoint log level (`INFO` by default) |
| `--detach` | write a pidfile and exit, leaving the endpoint up |

`atomic-leave <name>` takes `--cancel-tasks`: by default leaving a class
pool cancels **nothing**, because another member can still run what this
resource queued; a full teardown (`demo/2026_09_08/down.sh`) asks for the
cancel explicitly.  The federation answers with `members_removed`,
`tasks_requeued` and `tasks_failed`, which the CLI prints.

`atomic-resources` prints a two-level table — one row per resource
(site, member count, the aggregate software, node-hours, pilots, tasks,
liveness) followed by one indented row per member with its own
class/pool, size (`nodes x cores/node (+GPUs/node)`), software,
node-hours, pilots and tasks — or the raw records with `--json`, which
now carry `members`.  A resource whose federation reports no members
renders as a single row, exactly as before.  A `*` behind the node-hours
means the federation could not refresh usage for that row and is showing
its last known values (`"stale": true`).

## Bootstrapping a machine

`bootstrap.sh` prepares a venv and joins in one step, on a machine that
has only `python3` (and `git`, if ORBIT is installed from a repository):

```sh
./bootstrap.sh ~/atomic-ve --broker https://r3:8003 --name my_box \
               --mode allocation --declare cores=8 --software lammps
```

It creates the venv if missing, installs `radical.orbit` and
`atomic-wm[cli]` (non-editable) if missing, and then `exec`s
`atomic-join` with the rest of the arguments.  Re-running it on a
prepared venv skips straight to the join.  Knobs: `PYTHON`,
`ORBIT_SPEC` (a pip spec, a local path, or `none` to skip; defaults to
`git+https://github.com/radical-cybertools/radical.orbit@$ORBIT_BRANCH`),
`ORBIT_BRANCH` (default `devel` — note that `devel` does not yet host the
`federation` plugin the join talks to; until `feature/atomic-federation`
merges, that branch is what the *broker* must run, while the joining side
only needs an endpoint), `ATOMIC_SPEC`, `ATOMIC_FORCE=1` to reinstall,
`ATOMIC_NO_EXEC=1` to only prepare the venv.

> The `cli` extra is what pulls in `requests`.  The base install is
> deliberately stdlib-only so the synthetic workload tools also run under
> a pilot's python; a plain `pip install atomic-wm` therefore gives you
> the workload but not the CLIs.

## Files and state

| path | what |
|---|---|
| `~/.radical/orbit/atomic/<name>/endpoint.log` | the endpoint child's stdout/stderr (`$ATOMIC_WM_STATE` moves the whole tree) |
| `~/.radical/orbit/atomic/<name>/endpoint.pid` | pidfile written as soon as the endpoint child exists (before the join), read by `atomic-leave`; a clean teardown removes it (so if the CLI itself is `SIGKILL`ed, `atomic-leave` can still stop the endpoint) |
| `~/.radical/orbit/atomic/<name>/record.json` | the record the federation answered the join with, including every member's `pool_name` and `member_id`; it is what lets `atomic-leave` match this resource's pilots **exactly** (see Troubleshooting → *Leftover processes*).  Removed on teardown |
| `~/.radical/orbit/logs/ep_<name>.log` | the endpoint's own log (ORBIT's logging) |
| `~/.radical/orbit/federation/scratch/<name>` | default task scratch, unless `--scratch` says otherwise |

## Troubleshooting

**"endpoint 'ep_x' did not connect within 60 s"** — `atomic-join` prints
the tail of `endpoint.log` with the message.  Usual causes:

- *TLS*: the broker always speaks HTTPS, even with `--no-auth`.  Pass
  `--cert ~/.radical/orbit/broker_cert.pem` (or set
  `$RADICAL_ORBIT_BROKER_CERT`), and use `https://` in `--broker`.  A
  `CERTIFICATE_VERIFY_FAILED` / "no appropriate subjectAltName" error
  means no certificate was pinned — pass `--cert`.
- *auth*: with a token-protected broker, `--token` must match; the
  endpoint log shows the rejected handshake.
- *`--log-level` rejected*: a developer shell often exports
  `RADICAL_LOG_LVL=DEBUG_9`.  `atomic-join` removes it for the child, but
  if you start an endpoint by hand you have to.
- *wrong URL*: `--broker` must name the host the broker actually bound
  (`--host 127.0.0.1` advertises `127.0.0.1`, not the FQDN).

**"join failed: HTTP 404/503 … does not host the 'federation' plugin"** —
the broker is up but was started without the federation plugin.  Restart
it with `--plugins …,federation`.  `atomic-join` stops the endpoint it
started before it exits, so nothing is left running.

**"a resource named 'x' is already in the federation"** (409) — pick
another `--name`, or `atomic-leave x` first.  A resource whose CLI was
killed with `SIGKILL` never left the federation; `atomic-leave` cleans
that up even without a pidfile.

**"no core count for this resource"** — capability detection found
nothing (no `sysinfo` on the endpoint, say).  Pass
`--declare cores=<N>`.

**Firewalls** — the endpoint only ever dials *out* to the broker over
one WebSocket connection; nothing listens on the joined machine.  A
login node behind a firewall therefore needs no inbound rule, only
outbound access to the broker's host and port.

**Leftover processes** — `atomic-leave` stops this resource's endpoint
*and* any surviving pilot child of it.  Both are matched **by name, not
by broker**: the endpoint is the process whose `--name` is `ep_<name>`,
a pilot is one whose `--name` is the dispatcher's child endpoint name —
`<pool>_<member_id>_p.<id>` for a class pool, or `<pool>_p.<id>` for a
pool that is not one (psij's `local` executor cancels only the wrapper
job).

Those names are built **literally** from `record.json` (or, if that is
gone, from `GET resource/default/<name>` before the leave), so
`atomic-leave a` can never touch `local_a`'s pilots.  Only when neither
is available does it fall back to a pattern derived from the resource
name — and that fallback has a limit worth knowing: in
`fed-cpu_local_a.cpu_…` the class/resource boundary is a `_` like any
other, so it resolves the ambiguity by refusing an `_` inside the class
half.  A resource named `a` therefore still never claims `local_a`'s
pilots, but pilots of a class whose *own* name contains `_` are not
matched by the fallback at all.  Keep `record.json` and the question
does not arise.  So `atomic-leave local` never touches `local_a`'s processes — and
never touches endpoints of other brokers or other resources on the same
machine.  The pid from the pidfile is only signalled after `/proc`
confirms it really is that endpoint (pids get reused; a stale pidfile is
reported and removed instead).  If there is no pidfile at all,
`atomic-leave` falls back to the process table.  After a teardown,
`pgrep -af radical-orbit` should come back empty.

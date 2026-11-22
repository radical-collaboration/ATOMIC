# The `atomic_campaign` plugin

A **campaign** is one workflow specification plus a parameter sweep. The
plugin expands it into one *workflow instance* per sweep point, runs each
instance's *stages* sequentially through the resource federation, collects
every declared output into a central store on the host that runs it, and exposes
state and results over HTTP for the CLI and the demo UI.

```
POST campaigns/default          planner              runner (one asyncio
{workflow, sweep}   ─────────▶  SweepPlanner  ────▶  task per campaign)
                                  │                         │
                                  │ N instances             │ per stage:
                                  ▼                         ▼
                          wf-000, wf-001, …      federation submit → poll →
                                                 collect → manifest
```

It runs on the broker or on a **service endpoint** next to it (`ep_cm`,
plans/11), never on a compute node (`is_enabled`: roles `broker`, `login`,
`standalone`). It is in no default plugin set, so an endpoint hosts it
only when asked to. Where it runs decides how it reaches the federation:

- **on the broker** -- the sibling plugins **in process**:
  `app.state.endpoint_service.handle_request('POST', '/federation/submit/
  default', …)`;
- **on an endpoint** -- the same routes over the endpoint runtime's
  WebSocket, `EndpointRuntime.call(dst='broker', …)`, each call in a
  thread (the call blocks on the endpoint's own loop) with a 15 s timeout.
  A broker restart does not touch the campaign state there: the runner
  waits the outage out (see *Broker outages* below).

Only the CLI and the Explorer UI use HTTP.

### Running it on a service endpoint (`ep_cm`)

```sh
# the broker: federation + dispatcher, and serve the endpoint's UI module
RADICAL_ORBIT_ENDPOINT_UI_MODULES=atomic_campaign \
    radical-orbit-broker.py --plugins task_dispatcher,federation …

# next to it: a participant with no compute that only hosts the campaigns
radical-orbit-endpoint.py --name ep_cm --plugins atomic_campaign \
                          --url https://<broker>:<port>

# the CLI then talks to /ep_cm/atomic_campaign/…
export ATOMIC_CAMPAIGN_ENDPOINT=ep_cm      # or --campaign-endpoint ep_cm
```

`ep_cm` never joins the federation. `$ATOMIC_CAMPAIGN_STATE`,
`$ATOMIC_STORE_ROOT` and `$ATOMIC_CAMPAIGN_UNREACHABLE_SEC` are read by the
`ep_cm` process, and the central store lives on its host.

## Routes

All routes live under `/<campaign endpoint>/atomic_campaign/…` through the
gateway (`/broker/atomic_campaign/…`, or `/ep_cm/atomic_campaign/…`) and
always use the reserved session id **`default`** (created on demand, always
persistent — never register your own campaign session).

| Route | Meaning |
|---|---|
| `POST campaigns/{sid}` | submit a campaign; returns `{campaign_id, name, state, created_at, started_at, finished_at, workflows:[{id, params, state, reason, stages:[{name, state, resource, task_id}]}]}` |
| `GET campaigns/{sid}` | `{"campaigns": [summary, …]}`, newest first |
| `GET campaign/{sid}/{cid}` | the full campaign record (below) |
| `GET results/{sid}/{cid}` | parsed metrics — what the UI plots |
| `POST cancel/{sid}/{cid}` | stop driving the campaign |
| `GET store/{sid}[?cid=…]` | the central store layout (for UI links) |

Plus the routes every orbit plugin has: `health`, `version`, `ui_config`,
`list_sessions`, `register_session`, `unregister_session/{sid}`.

### Submit body

```json
{"workflow": {"name": "vacancy-classifier",
              "params": {"temperature": 300},
              "stages": [{"name": "md", "type": "simulation",
                          "cmd": ["atomic-fake-md", "--temperature",
                                  "{temperature}", "--out", "md.json"],
                          "requirements": {"cores": 2,
                                           "software": ["lammps"]},
                          "inputs": [], "outputs": ["md.json"]}]},
 "sweep": {"temperature": [300, 600, 900]},
 "max_concurrent_workflows": 8,
 "stage_timeout_sec": 900}
```

- `{param}` placeholders in every `cmd` string are substituted per sweep
  point. Only bare `{name}` placeholders are recognised, so a command that
  contains JSON braces survives untouched. An unknown placeholder, a stage
  without a `cmd`, duplicate stage names, or an `inputs`/`outputs` entry
  that is not a plain file name are **400s at submit time**.
- Several sweep keys give the cartesian product (last key varies fastest).
- Everything that can only fail later — no federation, no resource that
  satisfies the requirements, a task that fails — is reported as campaign
  and stage *state*, not as an HTTP error. A submit is accepted as long as
  the spec is runnable.

### Campaign record

```
campaign : campaign_id, name, state, reason, sweep, created_at,
           started_at, finished_at, workflows[]
workflow : id (wf-000…), name, params, state, reason, created_at,
           finished_at, stages[]
stage    : name, type, cmd, inputs, declared_outputs, requirements,
           state, reason, detail, task_id, resource, member, member_id,
           cls, pool, dispatcher_sid, child_endpoint, cwd, exit_code,
           submitted_at, started_at, finished_at,
           outputs[{name, size, via, path, errors}]
```

States: `PENDING → SUBMITTED → (STAGING) → RUNNING → DONE | FAILED |
CANCELED`; `SKIPPED` for a stage an earlier failure never reached, and
`INTERRUPTED` for anything the broker was still running when it stopped.
A stage succeeds exactly when its task reports `state == DONE` with
`exit_code in (0, null)` **and** every declared output was collected.

`reason` (on the campaign, the workflow and the stage) is **shown on
screen verbatim** by the demo UI, so it is drawn from a small fixed set of
phrases written in the demo's vocabulary — resources, campaigns,
workflows, stages — and never mentions brokers, pilots, endpoints,
dispatchers or plugins:

| reason | when |
|---|---|
| `the resource federation is not available` | the federation is not hosted |
| `no resource satisfies the stage requirements (a.cpu: gpus 0 < 1)` | federation `submit` said 409 **with** a non-empty `reasons` map (resources joined, none matches). The map (`member_id` → why that member lost) rides along in brackets for up to three members — the phrase alone leaves the audience guessing *what* was missing — and becomes `(N resources checked)` beyond that. Either way the full list is in `detail`, as `no resource satisfies requirements: a.cpu: gpus 0 < 1; b.gpu: node_hours exhausted` |
| `no resource satisfies the stage requirements` | 400 with a *placement* refusal — `no member satisfies …` / `… exceed every pilot_size …` (no per-member map to quote) |
| `no resources have joined the federation yet` | federation `submit` said 409 with an **empty** (or absent) `reasons` map — the federation has no members at all, so there is no requirement to go fix |
| `N of M workflows failed` | the campaign roll-up, when its failed workflows do not share one reason (they keep their own; `detail` carries the first one's) |
| `the stage could not be started` | any other submit failure, including a 400 that is not a placement refusal (a malformed body earns a 400 too, and blaming the resources for it would send the reader looking in the wrong place) |
| `the stage failed on resource <name>` | the task ended FAILED / non-zero **and** a poll had confirmed the placement (`member_id`) |
| `the stage failed` | the task ended FAILED / non-zero before any poll named a member — the resource on the record is still the advisory one |
| `the stage status could not be read` | task 404, a 409, or the federation unreachable for longer than the budget (300 s) |
| `the input file could not be placed on the resource` | an input could not be read out of the previous stage's outputs |
| `output(s) not collected: …` | a declared output was nowhere to be found |
| `stage timed out after N s (last state: …)` | the per-stage timeout |
| `the campaign was stopped` | cancel, from the route or the driver |
| `the service was restarted while this was running` | shutdown / restart |
| `the stage did not complete` | anything unexpected |

Whatever ORBIT actually said (a task's `error`, a plugin's `detail`, an
exception) is kept in the sibling **`detail`** field and in the log — never
in `reason`. The one thing that crosses over is the 409's per-member texts:
they are the federation's own words *about resources* (`gpus 0 < 1`,
`software missing: pytorch`), they carry no ORBIT vocabulary, and without
them the screen says only that nothing matched. `StageRun.to_dict()` also
exposes `reason` as `error` for the Explorer module. Keep new failure paths
inside this set.

### Results

`GET results/default/{cid}` returns

```json
{"campaign_id": "cmp-…", "state": "DONE",
 "workflows": [{"id": "wf-000", "params": {"temperature": 300},
                "state": "DONE",
                "metrics": {"md": <envelope>, "train": <envelope>},
                "files":   {"md": [{"name": "md.json", "size": 812,
                                    "json": true}]}}]}
```

`metrics[stage]` is the parsed JSON of the stage's **first** collected
`.json` output (the workload envelope from `docs/workload.md`: `type`,
`params`, `series`, `summary`, `produced_by`). Non-JSON outputs — and any
further JSON ones — are listed under `files[stage]` by name and size only.

## Lifecycle

1. **Plan.** `SweepPlanner.expand()` validates the spec and builds N
   `WorkflowInstance`s with substituted commands.
2. **Drive.** One asyncio task per campaign inside the plugin. Workflows
   run concurrently behind a semaphore (`max_concurrent_workflows`,
   default 8); the stages of one workflow run strictly in order.
3. **Per stage:**
   - task id `<cid>-<wf_id>-<stage>`; `cmd` is a list of strings;
   - **the previous stage's outputs ride in the submit body** as
     `inputs_b64: {name: base64}`. A federation pool is a capability
     class with several members, so nobody knows *where* the task will
     run until the dispatcher places it — there is no directory to stage
     into beforehand. The dispatcher spools the files and puts them
     wherever the task lands. Failing to *read* an input fails the stage
     with `the input file could not be placed on the resource` before the
     task is ever submitted; failing to *place* it is the dispatcher's
     and surfaces as a task failure;
   - `POST federation/submit/default` with the stage's `requirements` →
     `{task, pool, class, dispatcher_sid, resource, member,
     members_eligible}`. The runner never sends a `cwd`: the dispatcher
     assigns one when it places the task and reports it on the poll.
     `resource` in the answer is **advisory** and `member` is `null`
     until dispatch;
   - poll `GET federation/task/default/{task_id}` every 1 s, backing off to
     3 s, until the task state is terminal. The per-stage timeout (default
     15 min) counts **task** state only — a pilot that takes a minute to
     boot never fails a stage. A **404** (other than "unknown session") and
     a **409** fail the stage at once -- waiting cannot help. Every other
     error is waited out within the unreachable budget (*Broker outages*);
   - **placement is what the poll says.** Each poll may carry
     `member_id` (`<resource>.<member>`, split on the *last* dot),
     `member`, `resource`, `cwd` and — not guaranteed — `class`;
     whatever it names overwrites the advisory values from the submit,
     and `member_id` wins over a `resource`/`member` in the same answer
     because it is what the dispatcher stamped on the task. `resource: null` is
     a legitimate answer — for a task the class pool has not placed yet,
     and for one whose resource left the federation while it was queued —
     and never fails a stage: the last placement that *was* named stays
     on the record;
   - at the terminal state the declared outputs are collected
     **immediately** (before the pilot can go away) and a `manifest.json`
     is written.
4. **Finish.** A stage failure fails its workflow (`reason` kept) and skips
   its remaining stages; other workflows carry on. The campaign is `DONE`
   when every workflow is, `FAILED`/`CANCELED` otherwise. A campaign that
   ends `FAILED`/`CANCELED` from that roll-up and has no reason of its own
   takes one from its workflows: their shared reason **verbatim** (so the
   card reads `stage 'md': no resources have joined the federation yet`),
   or `N of M workflows failed` when they disagree — the card and
   `atomic-campaign status` show the *campaign's* reason, so a campaign
   that failed only because its workflows did has to repeat what they
   said. A reason set by cancel, interrupt or the driver always wins, and
   `DONE` still clears any reason a late cancel left behind. An orderly
   service shutdown stamps every unfinished campaign `INTERRUPTED` *before*
   cancelling its driver, so a restart shows "interrupted", not "stopped" —
   nobody asked for it to stop.

### Broker outages and cancels

A broker restart must not fail a campaign. The runner pulls state, so it
only has to tell an outage from an answer (the ORBIT contract, plans/11
§4):

Submit, poll, output collection and cancel classify errors the same way:

| transient -- retried | fatal -- the stage fails |
|---|---|
| `ConnectionError`, `TimeoutError` (the transport) | `409`: a known task whose class has no members now |
| `503`, with or without `Retry-After` | any other `404`: the task is gone |
| `410` "shutting down" | any other `4xx` (a `400`: a refused or malformed call) |
| `404` "unknown session" (the federation is still replaying) | an unexpected error (a bug, not an outage) |
| any other `5xx` | |

- **Unreachable budget.** Transient errors are retried for up to
  `unreachable_budget_sec` (default **300 s**,
  `$ATOMIC_CAMPAIGN_UNREACHABLE_SEC`), counted from the first failure of an
  uninterrupted run; any successful call resets it.
- **Collection retry.** An output that no source delivered while a source
  failed transiently (the broker went away right after the task ended) is
  fetched again within the same budget. Only after that does the stage
  fail with "output(s) not collected". A file that is simply not there is
  not waited for.
- **Submit retry.** A transient submit error is retried within the same
  budget with the **same task id**: a known task id answers with its
  existing record, so a submit whose answer got lost does not run twice.
  That holds once ORBIT branch `fix/federation-submit-ledger` is merged.
  Until then, ORBIT devel's `plugin_federation._route_submit` picks a
  class pool again without consulting its ledger, so a retry after a
  `5xx` that arrived after the task had landed may land in another pool
  and duplicate the task. Fatal submit errors fail the stage at once.
- **Cancels reach ORBIT.** Every cancel -- a stopped campaign, a stage
  timeout, a cancelled `run_workflow` -- is first put on the campaign's
  persisted `pending_cancels` list, then tried (shielded) up to three
  times. A stage that ends `FAILED` or `CANCELED` any other way while its
  task may still run queues a cancel without trying it. That covers an
  unreachable budget that runs out during the submit (the submit may have
  landed) or the poll, a fatal poll answer, and a crash. An entry the
  federation could not take stays. One plugin
  drainer settles the list; runners never retry an entry themselves.
  The drainer retries every `drain_interval_sec`, and wakes at once when
  a runner sees the federation answer after a failure, on any route, on
  a reconnect and when a campaign finishes. A refused or unknown task
  settles the entry.
- **Shutdown does not cancel.** An orderly stop (`shutdown`, or
  `on_host_stop` on `ep_cm`) marks running campaigns `INTERRUPTED` and
  leaves their tasks running; a broker restart therefore never kills HPC
  work. Their task ids are queued in `pending_cancels`, as are those of a
  campaign found running in the state file after a crash, and the next
  start cancels them: there is no resume, so nobody would collect them.

### Output collection order

Per declared output, the first path that delivers bytes wins; the winner is
recorded as `via` in the stage record and the manifest:

| `via` | how | when it works |
|---|---|---|
| `pilot_staging` | `POST /<child_endpoint>/staging/get/<sid>` `{"filename": "<cwd>/<name>"}` → base64 in the **`content`** key | always, incl. no shared filesystem — needs the pilot to be alive |
| `dispatcher_stage_out` | `GET task_dispatcher/stage_out/{dispatcher_sid}/{task_id}/{name}` → base64 in **`content_b64`** | broker and pilot share a filesystem |
| `broker_local` | direct read of `<cwd>/<name>` | localhost |

The pilot path uses orbit's own `StagingClient` -- on the broker over the
broker caller, on `ep_cm` over `_RuntimeHTTP(runtime, child_endpoint)`
(120 s per call); it registers
and unregisters its staging session per call. Paths must resolve under `~`
or `/tmp` — the staging plugin refuses anything else, which is why
`scratch_base` defaults live there.

## Central store

Root: `~/.radical/orbit/atomic_store` (override `$ATOMIC_STORE_ROOT`).

```
<root>/<campaign_id>/<workflow_id>/<stage>/md.json
<root>/<campaign_id>/<workflow_id>/<stage>/manifest.json
```

`manifest.json` records the provenance of one stage: `resource`,
`member`, `member_id`, `class`, `pool`, `dispatcher_sid`,
`child_endpoint`, `task_id`, `cwd`, `cmd`, `params`, `state`,
`exit_code`, the timestamps, `collected_at`, per output the `via` path
plus the errors of the paths that did not work, and — for a stage with
inputs — `inputs_staged: [{name, via: "submit", size}]`.

## Environment

| variable | meaning | default |
|---|---|---|
| `ATOMIC_CAMPAIGN_STATE` | directory holding `state.json` | `~/.radical/orbit/atomic_campaign` |
| `ATOMIC_STORE_ROOT` | central result store root | `~/.radical/orbit/atomic_store` |
| `ATOMIC_TOOL_PREFIX` | bin directory prepended to a bare `atomic-fake-*` command | unset |
| `ATOMIC_CAMPAIGN_UNREACHABLE_SEC` | how long the federation may be unreachable before a stage fails | `300` |
| `ATOMIC_CAMPAIGN_ENDPOINT` | (CLI) the endpoint hosting the plugin | `broker` |

`ATOMIC_TOOL_PREFIX` exists because the synthetic workload is only on
`PATH` inside a pilot whose venv has `atomic-wm` installed. Where it is
not, point it at the bin directory (e.g. `/…/ve3/bin`) **in the process
hosting the plugin** and the runner rewrites `argv[0]` to an absolute path. Commands
that already carry a path, and anything not named `atomic-fake-*`, are left
alone.

State is persisted to `<state root>/state.json` after every transition
(atomic temp-file + rename). On restart, campaigns that were still running
come back as `INTERRUPTED` — there is no resume; re-submit. Unfinished
campaigns are always kept; the **50 most recently finished** ones are kept
too, older ones are dropped from the state file and the listing.

Plugin construction knobs: `state_root`, `store_root`,
`max_concurrent_workflows`, `stage_timeout_sec`, `poll_interval_sec`,
`poll_max_interval_sec`, `unreachable_budget_sec`, `drain_interval_sec`. `max_concurrent_workflows` and
`stage_timeout_sec` can also be overridden per campaign in the submit body
(a non-positive value is a 400).

## CLI

```
atomic-campaign submit SPEC.json --sweep temperature=300,600,900
                                 [--wait [--timeout SEC]]
atomic-campaign status  CID [--json]
atomic-campaign results CID [--json]
atomic-campaign list    [--json]
atomic-campaign cancel  CID
```

`SPEC.json` is either a bare workflow spec (`examples/workflow_vacancy
.json`) or a full campaign request (`examples/campaign_sweep.json`, whose
`sweep` is used when `--sweep` is absent). `--sweep` may be repeated for a
multi-parameter sweep; values are parsed as JSON where possible, so numbers
stay numbers. Connection flags are the shared `--broker` / `--token` /
`--cert` (defaults `$RADICAL_ORBIT_BROKER_URL`, `$RADICAL_ORBIT_TOKEN`,
`$RADICAL_ORBIT_BROKER_CERT`), plus `--campaign-endpoint`
(`$ATOMIC_CAMPAIGN_ENDPOINT`, default `broker`) for a plugin on `ep_cm`.

`status` prints one row per workflow — id, parameters, state, and the
per-stage `stage:STATE@resource/member` chips (the placement is the one
the last poll reported; a stage the class pool has not placed yet shows
neither). The campaign's own `reason` is printed above the table, and each
`FAILED`/`CANCELED` workflow gets one indented `  wf-000: <reason>` line
below it — prose in a fifth column would push the placements off a demo
screen. `--wait` polls until the campaign
is terminal; `--timeout SEC` bounds the wait so a script never hangs.

Exit codes: `0` success · `1` error or a campaign that did not finish
`DONE` · `2` usage · `3` `--wait` timed out · `130` interrupted.

## Limitations (and what to do about them on Tuesday)

- **Inputs are base64 in a JSON body.** `inputs_b64` rides the broker
  frame path at ~1.33× the file size and is capped dispatcher-side (a
  `413` beyond it). Right for the demo's few-KB JSON, wrong for a
  multi-MB restart file — bulk input stays a pilot-staging job. Outputs
  are unaffected; they still come back through the pilot's own `staging`
  plugin first.
- **Placement is late.** The submit answer can only name an *advisory*
  resource, so a chip may change once when the first poll reports the
  member the dispatcher actually chose. `resource: null` is normal for a
  queued task and for one whose resource left the federation; the runner
  treats it as "not placed", never as a failure.
- **Executor detection.** Pilots are submitted by the *dispatcher*, on the
  broker host (`detect_batch_system().psij_executor`). That is correct for
  localhost and for allocation mode; a login-mode resource served from a
  non-Slurm broker host is a known gap (see `plans/00-overview.md`).
- **No task-level retry.** A failed stage fails its workflow. The campaign
  keeps the reason and the other workflows continue.
- **Cancel is cooperative.** `POST cancel` sets a flag: the driver stops
  before the next stage and, while polling, asks the dispatcher to cancel
  the running task (retried, see *Broker outages and cancels*). It does
  not tear down pilots.
- **`GET store/{sid}` walks the filesystem** on every call. Fine for a demo
  (tens of files), not a paging API.

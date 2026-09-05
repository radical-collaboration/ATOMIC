# The `atomic_campaign` plugin

A **campaign** is one workflow specification plus a parameter sweep. The
plugin expands it into one *workflow instance* per sweep point, runs each
instance's *stages* sequentially through the resource federation, collects
every declared output into a central store on the broker host, and exposes
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

It is a broker-hosted plugin (`is_enabled` is broker-only) and talks to its
sibling plugins **in process**, never over HTTP:
`app.state.endpoint_service.handle_request('POST', '/federation/submit/
default', …)`. Only the CLI and the Explorer UI use HTTP.

## Routes

All routes live under `/broker/atomic_campaign/…` through the gateway and
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
           state, reason, detail, task_id, resource, pool, dispatcher_sid,
           child_endpoint, cwd, exit_code, submitted_at, started_at,
           finished_at, outputs[{name, size, via, path, errors}]
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
| `no resource satisfies the stage requirements` | federation `submit` said 409 |
| `the stage could not be started` | any other submit failure |
| `the stage failed on resource <name>` | the task ended FAILED / non-zero |
| `the stage status could not be read` | task 404, or 10 unreadable polls |
| `the input file could not be placed on the resource` | `stage_in` failed |
| `output(s) not collected: …` | a declared output was nowhere to be found |
| `stage timed out after N s (last state: …)` | the per-stage timeout |
| `the campaign was stopped` | cancel, from the route or the driver |
| `the service was restarted while this was running` | shutdown / restart |
| `the stage did not complete` | anything unexpected |

Whatever ORBIT actually said (a task's `error`, a plugin's `detail`, an
exception) is kept in the sibling **`detail`** field and in the log — never
in `reason`. `StageRun.to_dict()` also exposes `reason` as `error` for the
Explorer module. Keep new failure paths inside this set.

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
   - `POST federation/submit/default` with the stage's `requirements` →
     `{task, resource, pool, dispatcher_sid}`; the task's `cwd` is the
     pool's task scratch directory;
   - the previous stage's outputs are staged in through the dispatcher
     (`stage_in/{dispatcher_sid}/{task_id}` with `{pool, filename,
     content_b64, overwrite}`) — see *Limitations* for the ordering caveat;
   - poll `GET federation/task/default/{task_id}` every 1 s, backing off to
     3 s, until the task state is terminal. The per-stage timeout (default
     15 min) counts **task** state only — a pilot that takes a minute to
     boot never fails a stage. A **404** fails the stage at once (the task
     is gone; waiting cannot help), and 10 consecutive unreadable polls do
     the same rather than burning the whole timeout;
   - **if `push_inputs` is on**, once the task's `child_endpoint` is known
     the inputs are additionally *pushed* to that pilot's own `staging`
     plugin (best effort, logged). It is **off by default**: the put
     overwrites, so on a shared filesystem it would rewrite the very file
     the running task is reading. Turn it on for a cross-host setup, where
     the dispatcher's `stage_in` wrote on the wrong host;
   - at the terminal state the declared outputs are collected
     **immediately** (before the pilot can go away) and a `manifest.json`
     is written.
4. **Finish.** A stage failure fails its workflow (`reason` kept) and skips
   its remaining stages; other workflows carry on. The campaign is `DONE`
   when every workflow is, `FAILED`/`CANCELED` otherwise. An orderly
   service shutdown stamps every unfinished campaign `INTERRUPTED` *before*
   cancelling its driver, so a restart shows "interrupted", not "stopped" —
   nobody asked for it to stop.

### Output collection order

Per declared output, the first path that delivers bytes wins; the winner is
recorded as `via` in the stage record and the manifest:

| `via` | how | when it works |
|---|---|---|
| `pilot_staging` | `POST /<child_endpoint>/staging/get/<sid>` `{"filename": "<cwd>/<name>"}` → base64 in the **`content`** key | always, incl. no shared filesystem — needs the pilot to be alive |
| `dispatcher_stage_out` | `GET task_dispatcher/stage_out/{dispatcher_sid}/{task_id}/{name}` → base64 in **`content_b64`** | broker and pilot share a filesystem |
| `broker_local` | direct read of `<cwd>/<name>` | localhost |

The pilot path uses the broker caller (a *remote* participant, unlike the
in-process sibling plugins) with orbit's own `StagingClient`; it registers
and unregisters its staging session per call. Paths must resolve under `~`
or `/tmp` — the staging plugin refuses anything else, which is why
`scratch_base` defaults live there.

## Central store

Root: `~/.radical/orbit/atomic_store` (override `$ATOMIC_STORE_ROOT`).

```
<root>/<campaign_id>/<workflow_id>/<stage>/md.json
<root>/<campaign_id>/<workflow_id>/<stage>/manifest.json
```

`manifest.json` records the provenance of one stage: `resource`, `pool`,
`dispatcher_sid`, `child_endpoint`, `task_id`, `cwd`, `cmd`, `params`,
`state`, `exit_code`, the timestamps, `collected_at`, and per output the
`via` path plus the errors of the paths that did not work.

## Environment

| variable | meaning | default |
|---|---|---|
| `ATOMIC_CAMPAIGN_STATE` | directory holding `state.json` | `~/.radical/orbit/atomic_campaign` |
| `ATOMIC_STORE_ROOT` | central result store root | `~/.radical/orbit/atomic_store` |
| `ATOMIC_TOOL_PREFIX` | bin directory prepended to a bare `atomic-fake-*` command | unset |

`ATOMIC_TOOL_PREFIX` exists because the synthetic workload is only on
`PATH` inside a pilot whose venv has `atomic-wm` installed. Where it is
not, point it at the bin directory (e.g. `/…/ve3/bin`) **in the broker
process** and the runner rewrites `argv[0]` to an absolute path. Commands
that already carry a path, and anything not named `atomic-fake-*`, are left
alone.

State is persisted to `<state root>/state.json` after every transition
(atomic temp-file + rename). On restart, campaigns that were still running
come back as `INTERRUPTED` — there is no resume; re-submit. Unfinished
campaigns are always kept; the **50 most recently finished** ones are kept
too, older ones are dropped from the state file and the listing.

Plugin construction knobs (broker config): `state_root`, `store_root`,
`max_concurrent_workflows`, `stage_timeout_sec`, `poll_interval_sec`,
`poll_max_interval_sec`, `push_inputs`. `max_concurrent_workflows` and
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
`$RADICAL_ORBIT_BROKER_CERT`).

`status` prints one row per workflow — id, parameters, state, and the
per-stage `stage:STATE@resource` chips. `--wait` polls until the campaign
is terminal; `--timeout SEC` bounds the wait so a script never hangs.

Exit codes: `0` success · `1` error or a campaign that did not finish
`DONE` · `2` usage · `3` `--wait` timed out · `130` interrupted.

## Limitations (and what to do about them on Tuesday)

- **Cross-host `stage_in`.** The dispatcher's `stage_in` writes on the
  *broker host*; it only reaches the task when broker and pilot share a
  filesystem. For a genuinely cross-host setup turn on `push_inputs`: the
  runner then also pushes the inputs to the target pilot's `staging`
  plugin once its child endpoint is known — best effort, and only *after*
  the task was submitted. It is off by default because that put
  (`overwrite=True`) races a task that is already reading the file on a
  shared filesystem. Keep stage inputs small either way.
- **Submit-then-stage ordering.** `dispatcher_sid` and `pool` only exist
  after the federation submit, so inputs are staged a moment *after* the
  task is queued. The dispatcher's conservative policy needs seconds to
  place a pilot, so the file is always there in time locally; a
  fully warm pool could in principle race. The fix (staging before submit)
  needs a federation route that reserves a placement — out of scope here.
- **Executor detection.** Pilots are submitted by the *dispatcher*, on the
  broker host (`detect_batch_system().psij_executor`). That is correct for
  localhost and for allocation mode; a login-mode resource served from a
  non-Slurm broker host is a known gap (see `plans/00-overview.md`).
- **No task-level retry.** A failed stage fails its workflow. The campaign
  keeps the reason and the other workflows continue.
- **Cancel is cooperative.** `POST cancel` sets a flag: the driver stops
  before the next stage and, while polling, asks the dispatcher to cancel
  the running task. It does not tear down pilots.
- **`GET store/{sid}` walks the filesystem** on every call. Fine for a demo
  (tens of files), not a paging API.

# ATOMIC WM demo — build plan overview

Target (this pass): the complete demo runs on **localhost** — one broker,
three local endpoints joined into a resource federation with different
declared capabilities, a campaign fanned into three workflows and executed
across them, results collected centrally and plotted in a demo UI.
Tuesday's real resources (Perlmutter / Bridges-or-other / radical.3) reuse
the same code with different join arguments.

Decisions already taken with Andre (do not relitigate):
- Routing sits **above** the task dispatcher. Dispatcher internals stay
  single-endpoint-per-pool. A new broker-side `federation` plugin owns the
  resource registry, capability + budget bookkeeping, and the pick.
- Capabilities and node-hour budget are **declared at join time** — no
  protocol change (`protocol.py` models are `extra='forbid'`).
- Campaign manager is **deliberately fake**: one workflow spec → N copies
  over a parameter sweep. The seam is documented so Masha's / RADICAL's
  `campaign_manager` (checkout: `radical.orbit/cm/`) can replace it later.
- Workloads are **synthetic** (numbers, not science) but emit real metric
  series so the UI can plot them.
- Tasks emit JSON; the UI plots client-side (inline SVG, no external libs —
  Explorer must work offline). Fan-out lives broker-side, not in a client.
- Two join modes, both must exist: `allocation` (endpoint runs inside a
  compute allocation; the whole allocation is the resource) and `login`
  (endpoint on a login node; declares queue/account/size/node-hour budget;
  pilots are launched via psij on demand). On localhost both modes run;
  `login` uses psij's `local` executor.

## Repos, branches, conventions

| Repo | Path | Branch | Notes |
|---|---|---|---|
| radical.orbit | `/home/merzky/radical/radical.orbit` | `feature/atomic-federation` | generic `federation` plugin (+ minimal dispatcher touches if unavoidable) |
| atomic | `/home/merzky/projects/atomic` | `feature/demo-wm` | everything ATOMIC-specific: package `atomic_wm` |

- Orbit venv: `ve3/` — **never modify it, never `pip install -e`**. Run
  orbit from source: `PYTHONPATH=/home/merzky/radical/radical.orbit/src`.
  Tests: `PYTHONPATH=src ve3/bin/python -m pytest tests/unittests/ -q`;
  lint: `ve3/bin/flake8 src/ bin/` (must stay clean).
- The atomic package installs into `ve3` with `ve3/bin/pip install
  /home/merzky/projects/atomic` (non-editable; re-run after edits). Its
  plugins register through the `radical.orbit.plugins` entry-point group
  (see `plugin_host_base._discover_entry_points`, docs/runtime_embedding.md
  §"Developing external plugins").
- Sub-agents do **not** run git. The supervisor commits per piece.
- Every piece: unit tests + docs + a line in `plans/STATUS.md`.
- Untracked files already in the atomic repo (slides, D1 plan, figures,
  `one_drive/`, `ve/`) are not part of this work — leave them alone.

## Facts about Orbit that shape the design (verified 2026-09-05)

- Dispatcher pools are declared per **session** via
  `POST /broker/task_dispatcher/register_session` with `{"pools": [...]}`;
  there is no add-pool route. Pool identity = `(owning_sid, name)`.
  → The federation creates **one dispatcher session per joined resource**,
  holding exactly one pool. Routing itself needs no dispatcher change; two
  small, sanctioned dispatcher touches are listed in 01 (min_pilots floor,
  pilot `finished_at` + history in the verbose pool summary).
- **Broker-hosted plugins call each other in-process, never via the
  broker caller**: `BrokerCaller` resolves `dst` through the participant
  registry and raises `RuntimeError("endpoint 'broker' unknown")` for the
  broker itself (`broker.py:886-888`). The supported path is
  `host = app.state.endpoint_service` (the `BrokerPluginHost`,
  `broker_plugin_host.py:41`) then
  `await host.handle_request('POST', '/task_dispatcher/register_session',
  {}, body_bytes)` (`broker_plugin_host.py:103-139`) — same event loop,
  exact route semantics incl. `HTTPException` codes, no token. Look the
  target plugin up lazily at call time (plugins load in filter order) and
  answer 503 if it is absent. This applies to federation→dispatcher and to
  atomic_campaign→federation.
- **Plugin sessions expire.** A session registered without an owner
  (server-side `handle_request` carries no `x-orbit-src`) is ephemeral and
  is swept `session_ttl` (3600 s) after `last_access`; dispatcher routes do
  not bump `last_access`, and sweeping a dispatcher session cancels its
  pools/pilots (`plugin_base.py:787-831`, `plugin_task_dispatcher.py:
  2083-2111`). Rules: (i) the federation registers each dispatcher session
  with `{"pools": [...], "lifetime": "persistent"}` (`plugin_base.py:479`)
  and unregisters explicitly on leave; (ii) **all federation and
  atomic_campaign routes are used with the reserved sid `default`**
  (always present and persistent, `plugin_base.py:468-511`; handlers call
  `self._ensure_default_session()` before checking `self._sessions`).
  CLIs and the UI never register their own federation/campaign sessions.
- **Pilot records**: `PilotRecord` has `submitted_at`, `active_at`,
  `walltime_deadline` but no end timestamp, and `fleet/{sid}` /
  `pool/{sid}/{name}` list **live pilots only** — finished pilots vanish
  from the API. Hence dispatcher touch #2 in 01.
- Child pilot endpoint name is `f'{pool}_{pid}'` (`plugin_task_dispatcher.
  py:1558`); a task's `pilot_id` maps to `pilots[].child_endpoint_name` in
  the verbose pool summary **while the pilot is live**.
- Task dict = `asdict(TaskRecord)`: `state`, `exit_code`, `error`,
  `finished_at`, `pilot_id`. **Terminal** = `state ∈ {DONE, FAILED,
  CANCELED}`; **success** = `state == DONE and exit_code in (0, None)`.
- Conservative policy defaults: tick 5 s, `min_dwell_sec 30`,
  `max_in_flight_submissions 2`, handshake timeout 300 s. Federation-made
  pools set `strategy_config: {"min_dwell_sec": 5,
  "max_in_flight_submissions": 1}`. Runners time out on **task** state,
  never on pilot state, and budget 60–90 s pilot warm-up per resource.
- `detect_batch_system().psij_executor` for pilot submission runs on the
  **broker host** (`plugin_task_dispatcher.py:1565`), not on the endpoint.
  Correct for localhost and allocation mode; a Tuesday gap for login mode
  from a non-Slurm broker host — record, do not fix now.
- `PoolConfig.queue` must be non-empty and not the sentinel `"default"`;
  allocation-mode pools use `queue: "allocation", account: null`.
- A pilot is a psij job running `radical-orbit-endpoint-wrapper.sh -n
  <child> --plugins default`; the child endpoint dials the broker and hosts
  `rhapsody` (backend from `PilotSize.rhapsody_backend`; use `concurrent`
  locally). Locally `detect_batch_system()` → NullBatchSystem, psij
  executor `local`, role `standalone`.
- Pool-mode staging assumes a **shared filesystem between broker host and
  pilot**: `stage_in` writes to `<scratch_base>/<task_id>/` on the broker
  host and the pilot runs with that cwd. Fine on localhost; on Tuesday's
  cross-host setup, result collection must instead pull files through the
  pilot's own `staging` plugin (`get`), which the pilot hosts by default.
  The campaign plugin therefore collects results via the pilot's staging
  plugin first and falls back to dispatcher `stage_out`.
- Endpoint-mode submit (`endpoint=` instead of `pool=`) proxies straight to
  the endpoint's rhapsody plugin and has **no** staging support. Fallback
  only if the spike shows the pool path is broken locally.
- Capability data exists per endpoint on demand: `sysinfo` (cores, memory,
  GPUs), `queue_info` (partitions, `job_allocation` → `{n_nodes, runtime}`
  when inside an allocation). Nothing aggregates it; the federation does.
- Accounting does not exist anywhere. Node-hours are derivable: pilot
  `nodes × (ended_at − started_at)` from dispatcher pilot records
  (`GET fleet/{sid}` / `GET pool/{sid}/{name}`).
- Explorer: single-file SPA (`data/orbit_explorer.html`); a plugin exposes
  a UI by setting `ui_module = <abs path to .js>` (served at
  `/plugins/<file>.js`); modules export `name`, `template()`, `css()` and
  render hooks. Pattern: `data/plugins/task_dispatcher.js` (188 lines),
  `sysinfo.js`. Broker-hosted plugin UI files are read off disk via
  `broker_plugin_host.get_ui_modules()`.
- Test harness for broker-hosted plugins: `tests/unittests/
  test_task_dispatcher_broker.py` (fake pilot plugin, `make_runtime`), e2e
  harness `tests/unittests/test_runtime.py`.

## Pieces and dependencies

```
 spike (local pool path)  ──gates──▶  everything below
 P1 orbit: federation plugin ─────┐
 P3 atomic: workload             │  (parallel; P2/P4/P5 build against the
 P2 atomic: join CLI + bootstrap ├──  contract in this file, with fakes)
 P4 atomic: campaign plugin      │
 P5 atomic: demo UI              ┘
 P6 local e2e (up/down/smoke)  ◀── needs P1..P5
```

Per-piece plans: `01-orbit-federation.md`, `02-atomic-join-cli.md`,
`03-atomic-workload.md`, `04-atomic-campaign.md`, `05-atomic-ui.md`,
`06-local-e2e.md`. Review protocol: `07-review-protocol.md`.

## The contract (frozen — change only via a plan revision)

All broker-hosted routes live under the plugin instance namespace,
reached through the gateway as `/broker/<instance>/<route>`. Every plugin
already gets `register_session` / `unregister_session/{sid}` / `health` /
`version` / `list_sessions` for free (Plugin base). **`{sid}` is always
the reserved `default` session for federation and atomic_campaign routes**
(see Facts); the dispatcher sessions the federation creates internally are
per-resource and persistent.

Explorer / ui_module facts (verified): a broker-hosted plugin's
`ui_module` may be any absolute `.js` path; it is served at
`/plugins/<pname>.js` where `<pname>` is the plugin's load-filter key (so
the file must be named `atomic_campaign.js`; regex
`^[a-z_][a-z0-9_.]*\.js$`). The gateway caches the JS until a miss —
**restart the broker after editing the module**. Module hooks:
`export const name`, `template()`, `css()`, `init(page, api)`, `onShow`,
`onNotification` (`orbit_explorer.html:1244-1259, 2551-2607`). `api.fetch`
is namespaced to the module's own plugin; cross-plugin calls use
`api.fetchRaw('/broker/federation/...')` with
`api.getSession('federation', {sid: 'default'})`. `quickjs` is not in
`ve3`; node v22 is on the machine → JS tests are node-based.

### federation (orbit, instance name `federation`)

Resource record (JSON):
```json
{
  "name": "perlmutter_a",             // unique in the federation
  "endpoint": "ep_perlmutter_a",      // connected endpoint that serves it
  "mode": "allocation" | "login",
  "site": "NERSC",                    // free text
  "kind": "hpc" | "cluster" | "workstation",
  "capabilities": {"cores": 128, "gpus": 4, "mem_gb": 256,
                   "software": ["lammps", "pytorch"]},
  "budget": {"node_hours": 40.0},     // declared allowance for this join
  "scratch_base": "/tmp/atomic-demo/perlmutter_a",  // optional; default
                                      // ~/.radical/orbit/federation/scratch/<name>
                                      // (must lie under ~ or /tmp — staging plugin rule)
  "pool": {                           // login mode only; allocation mode derives it
    "queue": "regular", "account": "m1234",
    "nodes": 1, "cpus_per_node": 128, "gpus_per_node": 4,
    "walltime_sec": 3600, "max_pilots": 2,
    "rhapsody_backend": "concurrent"
  },
  // server-filled:
  "joined_at": 1757100000.0,
  "dispatcher_sid": "…", "pool_name": "fed-perlmutter_a",
  "usage": {"node_hours_used": 1.25, "node_hours_remaining": 38.75,
            "pilots_active": 1, "tasks_running": 3, "tasks_done": 12},
  "liveness": "ok" | "suspect" | "lost"
}
```
Routes:
- `POST join/{sid}` body = resource record (client fields) → 200 full
  record. 409 if name exists; 404 if `endpoint` is not a connected
  participant; 503 if the dispatcher plugin is not hosted. Creates a
  **persistent** dispatcher session + one pool `fed-<name>` bound to
  `endpoint` (in-process `handle_request`, see Facts). In `allocation`
  mode the pool is `{queue: "allocation", account: null, min_pilots: 1,
  max_pilots: 1, nodes/cpus/gpus from capabilities or queue_info
  job_allocation, walltime = remaining allocation or 3600}` — the
  allocation's pilot starts at join (dispatcher touch #1); budget defaults
  to `nodes × walltime_h` if not declared.
- `POST leave/{sid}/{name}` → 200; cancels the pool's tasks, unregisters
  the dispatcher session.
- `GET resources/{sid}` → `{"resources": [record, …]}`. Usage refreshed on
  each call (cached ≤2 s): node-hours from the dispatcher verbose pool
  summary's pilot history (`finished_at` — touch #2; live pilots use now);
  `tasks_running/done` from the federation's **own submit ledger** (the
  dispatcher's `recent_tasks` is capped at 50).
- `GET resource/{sid}/{name}` → record.
- `POST pick/{sid}` body `{"requirements": {"cores": 4, "gpus": 0,
  "software": ["lammps"], "node_hours": 0.1}}` → `{"resource": name,
  "pool": pool_name, "dispatcher_sid": sid, "score": float}` or 409 `no
  resource satisfies requirements` with the per-resource rejection reasons.
  Policy v1 (`FederationPolicy.pick`): filter by capability ⊇ requirements
  and remaining budget ≥ requested and liveness ok; score = remaining
  budget fraction − load (tasks_running / cores); deterministic tie-break
  by name. Policy class is pluggable (module:Class in plugin config), same
  spirit as `task_dispatcher_policy.py`.
- `POST submit/{sid}` body `{"task": {task_id, cmd, cwd?, inputs, outputs,
  priority}, "requirements": {...}}` → picks, forwards to the dispatcher
  `submit` on the chosen pool (cwd defaults to the pool task scratch), and
  returns `{"task": <dispatcher task dict>, "resource": name, "pool": …,
  "dispatcher_sid": …}`. This is the single call the campaign plugin uses.
- `GET task/{sid}/{task_id}` → dispatcher task dict + `resource`.
- Persistence: `~/.radical/orbit/federation/<instance>/state.json`
  (resources + their dispatcher sids + submit ledger); on restart, for each
  resource whose endpoint is connected, re-register the dispatcher session
  with the **stored sid, the identical pool and `lifetime: persistent`** —
  the dispatcher has already replayed the pool state for that sid and
  `_materialise_pool` returns it (verified, `plugin_task_dispatcher.py:
  584-640`); others are marked `lost`.
- Env override for state root (tests + demo isolation):
  `RADICAL_ORBIT_STATE_ROOT` if the dispatcher already supports one (check
  `_state_root` construction) — otherwise the federation and campaign
  plugins honour `RADICAL_ORBIT_FEDERATION_STATE` / `ATOMIC_CAMPAIGN_STATE`
  and the demo backs up + clears the dispatcher state dir.
- UI hook: `ui_config` minimal (a resources monitor) so the plain Explorer
  shows the federation without the ATOMIC UI.

### atomic_campaign (atomic repo, broker-hosted, instance `atomic_campaign`)

Workflow spec (JSON, the thing the "portal" submits):
```json
{
  "name": "vacancy-classifier",
  "params": {"temperature": 300},           // filled per copy by the planner
  "stages": [
    {"name": "md",    "type": "simulation",  "cmd": ["atomic-fake-md", "--temperature", "{temperature}", "--steps", "200", "--out", "md.json"],
     "requirements": {"cores": 2, "software": ["lammps"]}, "inputs": [], "outputs": ["md.json"]},
    {"name": "train", "type": "ml_training", "cmd": ["atomic-fake-train", "--in", "md.json", "--epochs", "20", "--out", "model.json"],
     "requirements": {"cores": 2, "gpus": 0, "software": ["pytorch"]}, "inputs": ["md.json"], "outputs": ["model.json"]}
  ]
}
```
Campaign request: `{"workflow": <spec>, "sweep": {"temperature": [300, 600, 900]}}`
→ planner expands to one workflow per sweep point (cartesian if several
keys). Stages within a workflow run sequentially (outputs of stage k are
inputs of k+1); workflows run concurrently.
Routes:
- `POST campaigns/{sid}` → `{"campaign_id", "workflows": [{id, params,
  stages:[{name, state, resource?, task_id?}]}], "state": "RUNNING"}`
- `GET campaigns/{sid}` → list summaries.
- `GET campaign/{sid}/{cid}` → full state incl. per-stage resource,
  task_id, timestamps, exit codes.
- `GET results/{sid}/{cid}` → `{"workflows": [{id, params, metrics:
  {stage: <parsed JSON from the stage's declared *.json outputs>}}]}` —
  what the UI plots.
- `POST cancel/{sid}/{cid}`.
- `GET store/{sid}` → central store layout for the UI to link.
- Central store: `~/.radical/orbit/atomic_store/<cid>/<wf_id>/<stage>/…`
  on the broker host; each collected output is copied there, plus
  `manifest.json` (resource, task_id, timings, collection path used).
- The campaign plugin talks to the federation **in-process**
  (`host.handle_request('POST', '/federation/submit/default', …)`), never
  over HTTP; only the CLI and UI use HTTP.
- Campaign driver = one asyncio task per campaign inside the plugin;
  state persisted to `~/.radical/orbit/atomic_campaign/state.json`; on
  restart, running campaigns are marked `INTERRUPTED` (no resume needed).
- Planner seam: `class CampaignPlanner: def expand(self, workflow, sweep)
  -> list[WorkflowInstance]` (fake: sweep). Executor seam: `class
  StageRunner` wrapping federation submit/poll/collect. Both documented in
  `docs/campaign_seam.md` with the mapping to `cm/` concepts (campaign =
  DAG of workflow groups; our sweep = one group, N instances).

### atomic_wm CLI (atomic repo)

- `atomic-join` — start an endpoint and join it (see 02). Console script.
- `atomic-leave NAME`, `atomic-resources` (table incl. node-hours),
  `atomic-campaign submit spec.json --sweep temperature=300,600,900`,
  `atomic-campaign status CID`, `atomic-campaign results CID --json`.
- All talk HTTP to the gateway: `--broker URL` (default
  `$RADICAL_ORBIT_BROKER_URL`), `--token` (default
  `$RADICAL_ORBIT_TOKEN`, may be empty with `--no-auth` brokers),
  `--cert` (default `$RADICAL_ORBIT_BROKER_CERT`; TLS is always on).
  They use sid `default` for federation/campaign routes and register
  short-lived sessions only for endpoint-side plugins they query
  (`sysinfo` metrics are session-scoped; `queue_info/job_allocation` is
  session-less).
- Shared-file ownership: P3 creates `pyproject.toml` (with **all**
  console-script and entry-point lines up front) and `atomic_wm/__init__.py`;
  the supervisor seeds `atomic_wm/client.py` with the contract's function
  signatures before P2/P4/P5 start; P2 implements it; P4/P5 only call it
  (P4's tests mock it). Nobody else edits `pyproject.toml` without a
  STATUS.md handoff line.

### Fake workload (atomic repo, console scripts)

- `atomic-fake-md --temperature T --steps N --out md.json [--seed S]
  [--duration-sec D]` → JSON `{"type":"simulation","temperature":T,
  "series":{"step":[…],"energy":[…],"temperature":[…]},"summary":{…}}`.
  Deterministic: the default seed is a stable hash of the tool's own
  parameters (temperature, steps), so identical params ⇒ identical output
  without `--seed`; runtime ≈ D (default 5 s).
- `atomic-fake-train --in md.json --epochs E --out model.json` → JSON
  `{"type":"ml_training","series":{"epoch":[…],"loss":[…],"accuracy":[…]},
  "summary":{"final_accuracy":…}}`; accuracy plateau is a monotone
  function of input temperature (e.g. 0.99 @300 K, 0.93 @600 K, 0.85
  @900 K) with noise amplitude far below the plateau gaps, so
  `final_accuracy` is **strictly decreasing** in temperature by
  construction (the smoke test asserts this).
- `atomic-fake-descriptors --in md.json --out desc.json` (optional third
  stage type `analysis`).

## Spike result (2026-09-06 00:10) — local pool path WORKS

Proven three times on localhost: broker `--no-auth` + one standalone
endpoint + dispatcher pool (`endpoint_name` bound, `rhapsody_backend:
concurrent`, psij `local` executor) → pilot ACTIVE 4.6 s after submit,
task DONE + `stage_out` at 5.1 s (warm broker: 2.8 s). Endpoint-mode also
works when the endpoint process has `RADICAL_ORBIT_RHAPSODY_BACKEND=
concurrent` exported (otherwise rhapsody defaults to `dragon_v3` and
fails). Environment rules every runner script must follow:

- `PATH` must include `ve3/bin` **in the endpoint process** (psij resolves
  `radical-orbit-endpoint-wrapper.sh` by name).
- `PYTHONPATH=$ORBIT_SRC/src` for broker/endpoint/clients when running
  from source; it does **not** reach pilots (wrapper prepends ve3
  site-packages). `ve3` currently holds a stale radical.orbit **0.3.0**
  (src is 0.8.0) plus a pre-#121 wrapper → the local run script runs
  `ve3/bin/pip install /home/merzky/radical/radical.orbit` first (allowed,
  non-editable) so pilots run current code.
- `unset RADICAL_LOG_LVL` (the user's shell exports `DEBUG_9`, which the
  endpoint's `--log-level` default rejects); use `RADICAL_ORBIT_LOG_LVL`.
  Do **not** export `RADICAL_ORBIT_LOG_FILE` (pilots would inherit it).
- TLS is mandatory even with `--no-auth`: broker needs
  `~/.radical/orbit/broker_cert.pem` + `broker_key.pem` (present here);
  clients/endpoints use `https://127.0.0.1:<port>` and
  `RADICAL_ORBIT_BROKER_CERT=~/.radical/orbit/broker_cert.pem`.
- Broker `--host 127.0.0.1` (the advertised URL is the literal bind host;
  `0.0.0.0` would advertise the FQDN to pilots). Use a non-default port
  (e.g. 8010) to avoid the user's usual broker on 8000/8003.
- Pool `queue` must not be the literal string `"default"` (dispatcher
  sentinel); anything else is fine locally.
- Wipe/isolate `~/.radical/orbit/task_dispatcher/state/` for demo runs
  (stale sessions are replayed at broker start) — prefer pointing state
  dirs at `/tmp/atomic-demo/...` if the plugins allow configuring them;
  otherwise back up and clear.
- The conservative policy submits a pilot ~3.5 s after the first task
  arrives; `min_pilots` is parsed by config but not obviously honoured by
  the policy — P1 must verify and, if unhonoured, either implement
  "start `min_pilots` pilots at pool materialisation" in the policy hook
  (`on_tick` may call `submit_pilot`) or accept first-task start-up
  (5 s) and say so in docs.

## Risks and their answers

- Pilot child runs the **installed** orbit from `ve3` (see above) — the
  local run script refreshes ve3 from the feature branch; broker-side
  changes are exercised from src regardless.
- Three endpoints on one host: unique `--name`, separate scratch dirs
  (`scratch_base` per pool under `/tmp/atomic-demo/<name>`), no port
  clashes (endpoints dial out; only the broker listens).
- Review loops are bounded (see 07). Unresolved non-blocking findings are
  logged in STATUS.md, not fixed forever.

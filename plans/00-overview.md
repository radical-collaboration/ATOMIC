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
  there is no add-pool route. Pool identity = `(name, endpoint_name)`.
  → The federation creates **one dispatcher session per joined resource**,
  holding exactly one pool. Zero dispatcher changes needed for routing.
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
`version` / `list_sessions` for free (Plugin base). `sid` below is the
caller's plugin session id.

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
  record. 409 if name exists. Creates a dispatcher session + one pool
  `fed-<name>` bound to `endpoint`. In `allocation` mode the pool is
  `{max_pilots: 1, nodes/cpus/gpus from capabilities or queue_info
  job_allocation, walltime = remaining allocation or 3600}`; budget
  defaults to `nodes × walltime_h` if not declared.
- `POST leave/{sid}/{name}` → 200; cancels the pool's tasks, unregisters
  the dispatcher session.
- `GET resources/{sid}` → `{"resources": [record, …]}` (usage refreshed
  from dispatcher fleet/pool state on each call, cached ≤2 s).
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
  (resources + their dispatcher sids); on restart, resources whose
  endpoint is connected are re-attached, others marked `lost`.
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
  `manifest.json` (resource, task_id, timings).
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
  `$RADICAL_ORBIT_TOKEN`, may be empty with `--no-auth` brokers).

### Fake workload (atomic repo, console scripts)

- `atomic-fake-md --temperature T --steps N --out md.json [--seed S]
  [--duration-sec D]` → JSON `{"type":"simulation","temperature":T,
  "series":{"step":[…],"energy":[…],"temperature":[…]},"summary":{…}}`.
  Deterministic given seed; runtime ≈ D (default 5 s).
- `atomic-fake-train --in md.json --epochs E --out model.json` → JSON
  `{"type":"ml_training","series":{"epoch":[…],"loss":[…],"accuracy":[…]},
  "summary":{"final_accuracy":…}}`; accuracy trend depends on input
  temperature (higher T → lower plateau) so plots differ per workflow.
- `atomic-fake-descriptors --in md.json --out desc.json` (optional third
  stage type `analysis`).

## Risks and their answers

- Local pool path unproven → spike runs first; fallback = endpoint mode via
  rhapsody plugin + endpoint `staging` plugin for outputs.
- Pilot child runs the **installed** orbit from `ve3` (wrapper prepends
  site-packages). Endpoint-side code changes need `ve3/bin/pip install
  /home/merzky/radical/radical.orbit`; broker-side changes run from src.
  Prefer broker-side changes only.
- Three endpoints on one host: unique `--name`, separate scratch dirs
  (`scratch_base` per pool under `/tmp/atomic-demo/<name>`), no port
  clashes (endpoints dial out; only the broker listens).
- Review loops are bounded (see 07). Unresolved non-blocking findings are
  logged in STATUS.md, not fixed forever.

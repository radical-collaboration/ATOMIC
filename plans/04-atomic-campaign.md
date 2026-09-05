# P4 — `atomic_campaign` plugin (atomic repo, broker-hosted)

Repo/branch: `/home/merzky/projects/atomic` @ `feature/demo-wm`.
Contract: `00-overview.md` §"atomic_campaign". Depends on P1's federation
routes — build against the contract with a fake federation client in
tests; integrate for real in P6.

## Files

- `atomic_wm/plugins/campaign.py` — `PluginAtomicCampaign(radical.orbit.Plugin)`,
  `plugin_name = 'atomic_campaign'`, broker-only, `ui_module` pointing at
  the packaged `atomic_wm/ui/atomic.js` (absolute path via
  `importlib.resources`; P5 writes the file — until then a stub module
  exporting `name/template/css` so the broker starts).
- `atomic_wm/campaign/planner.py` — `CampaignPlanner` (abstract) +
  `SweepPlanner` (cartesian product of `sweep`, substitutes `{param}` in
  `cmd` strings; validates every placeholder is provided).
- `atomic_wm/campaign/runner.py` — `StageRunner`: for one workflow
  instance, drives stages sequentially: create task_id
  `<cid>-<wf>-<stage>`, `stage_in` previous outputs (bytes from the
  store), federation `submit` with the stage's requirements, poll
  `task/{sid}/{task_id}` (interval 1 s, backoff to 3 s) until terminal,
  collect declared outputs (see below), write `manifest.json`. Failure of a
  stage fails the workflow (state FAILED, reason kept); other workflows
  continue. Concurrency: `asyncio.gather` over workflows, semaphore
  `max_concurrent_workflows` (default 8).
- `atomic_wm/campaign/state.py` — dataclasses (`Campaign`,
  `WorkflowInstance`, `StageRun`) + JSON persistence.
- `atomic_wm/campaign/store.py` — central store layout + `collect()`.
- `atomic_wm/cli/campaign.py` — `atomic-campaign submit|status|results|
  cancel|list` over HTTP.
- `docs/campaign.md` (routes, lifecycle, store layout, CLI) and
  `docs/campaign_seam.md` (how to replace `SweepPlanner`/`StageRunner`
  with the real Campaign Manager in `radical.orbit/cm/` — map: CM
  campaign = DAG of workflow groups; our sweep = one group with N
  instances; CM's executor/resource pool ↔ federation `pick/submit`; CM
  `on_replica_done` ↔ our stage-done hook).
- `tests/test_planner.py`, `test_runner.py` (fake federation client with
  scripted task-state transitions and staged output bytes; asserts
  sequential stages, concurrent workflows, failure isolation, store
  layout, manifest content), `test_plugin_campaign.py` — mount the plugin
  the way `radical.orbit/tests/unittests/test_plugin_task_dispatcher.py`
  does (`_make_plugin`/`_register`: bare `FastAPI()`,
  `app.state.is_broker = True`, broker state attributes,
  `starlette.testclient.TestClient(plugin._app)`), with a fake
  `_FederationAPI`.
- Plugin config: state root `~/.radical/orbit/atomic_campaign/`
  overridable by `ATOMIC_CAMPAIGN_STATE`; store root by
  `ATOMIC_STORE_ROOT`. `ui_module` path must be named
  `atomic_campaign.js` (served at `/plugins/atomic_campaign.js`).

## Talking to the federation

In-process only: `host = self._app.state.endpoint_service`;
`await host.handle_request('POST', '/federation/submit/default', {},
body)` etc. (see 00 Facts). Wrap in `_FederationAPI` (submit, task,
resources) so tests can substitute a fake. 503 if the federation plugin is
not hosted. All campaign routes use sid `default`
(`self._ensure_default_session()` first).

## Output collection (important for Tuesday)

Collect **immediately when the task reaches a terminal state**, before
the pilot can end (pilot walltime / allocation end makes path 1
disappear). Order of attempts per declared output:
1. Pilot-side `staging` plugin, works without a shared filesystem. The
   child endpoint name comes back from federation `task/default/<task_id>`
   as `child_endpoint` while the pilot is live (dispatcher child name
   pattern `f'{pool}_{pid}'`). Call `POST /<child_endpoint>/staging/get/
   {staging_sid}` with body `{"filename": "<abs cwd>/<output>"}` → response
   `{"path", "size", "content"}` — the key is `content` (base64), **not**
   `content_b64`. Needs its own staging session on that child (register,
   use, unregister; one per task is fine). Paths must resolve under `~` or
   `/tmp` (staging plugin rule) — hence `scratch_base` defaults.
   In-process from the broker, this is a broker-caller call to a remote
   participant (`self._broker_caller` / `_make_child_client` pattern from
   the dispatcher, `plugin_task_dispatcher.py:780`), which IS supported
   for endpoints.
2. Dispatcher `stage_out/{dispatcher_sid}/{task_id}/{filename}` (shared-FS
   case; key `content_b64`). The `dispatcher_sid` is in the federation
   submit response.
3. Direct read of `<cwd>/<output>` on the broker host (localhost case).
Record which path succeeded in `manifest.json`.

Inputs for stage k+1: `stage_in/{dispatcher_sid}/{task_id}` writes on the
**broker host** only (`plugin_task_dispatcher.py:1281-1289`). Fine on
localhost and shared-FS setups; cross-host input staging is a documented
gap for Tuesday (`docs/campaign.md` §Limitations) — the demo's workflow
must therefore keep stage inputs small and, if the two stages of a
workflow may land on different hosts, the runner additionally pushes
inputs to the *target* pilot via its staging plugin `put` once the task's
`child_endpoint` is known (best effort, logged). Implement path 1 +
`put` best-effort now; do not over-engineer.

## Task outcome

Terminal = `state ∈ {DONE, FAILED, CANCELED}`; success = `state == DONE
and exit_code in (0, None)`. Poll interval 1 s → 3 s backoff; per-stage
timeout configurable (default 15 min) counted on **task** state only.

## Results endpoint

`GET results/{sid}/{cid}` parses every collected `*.json` output and
returns `{"workflows": [{"id", "params", "state", "metrics": {stage:
envelope}}]}`; non-JSON outputs are listed by name/size only.

## Acceptance

- Tests green, flake8 clean, docs written.
- With P1+P3+P6 in place: `atomic-campaign submit examples/workflow_vacancy.json
  --sweep temperature=300,600,900` runs three workflows, each two stages,
  placed across ≥2 distinct local resources (visible in `status`), and
  `results --json` returns three metric sets with different
  `final_accuracy`.

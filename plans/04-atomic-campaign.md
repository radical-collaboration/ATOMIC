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
  layout, manifest content), `test_plugin_campaign.py` (routes via an
  in-process FastAPI app if orbit's Plugin can be mounted standalone —
  check how `test_plugin_task_dispatcher.py` does it; else test the
  handlers directly).

## Output collection (important for Tuesday)

Order of attempts per declared output:
1. Pilot-side `staging` plugin `get` (path = task cwd/output) through the
   gateway — works when broker and pilot do **not** share a filesystem.
   The pilot endpoint name is available from the dispatcher task/pilot
   records (`pilot_id`/child endpoint); document how it is resolved.
2. Dispatcher `stage_out/{sid}/{task_id}/{filename}` (shared-FS case).
3. Direct read of `cwd/output` on the broker host (localhost case).
Record which path succeeded in the manifest.

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

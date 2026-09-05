# The campaign seam — replacing the fake with the real Campaign Manager

`atomic_wm.campaign` is a **deliberately simple** campaign manager: one
workflow spec fanned into N copies over a parameter sweep. It exists so the
demo has a credible top layer over the resource federation, and it is built
around two seams so RADICAL's real Campaign Manager (checkout:
`radical.orbit/cm/`, package `campaign`, class `AsyncCampaignManager`) can
take its place without touching the plugin, the CLI, or the UI.

## The two seams

```python
class CampaignPlanner:                       # atomic_wm/campaign/planner.py
    def expand(self, workflow, sweep) -> list[WorkflowInstance]: ...

class FederationAPI:                         # atomic_wm/campaign/runner.py
    async def submit(self, task, requirements) -> dict
    async def task(self, task_id) -> dict
    async def resources(self) -> list[dict]
    async def stage_in(self, dispatcher_sid, pool, task_id, name, data)
    async def stage_out(self, dispatcher_sid, task_id, name) -> bytes | None
    async def staging_get(self, endpoint, path) -> bytes | None
    async def staging_put(self, endpoint, path, data) -> bool
    async def cancel_task(self, dispatcher_sid, task_id) -> bool
```

- **What to run** is `CampaignPlanner` (`SweepPlanner` is the fake).
- **How to run it** is `StageRunner` / `CampaignRunner` over a
  `FederationAPI` (`_FederationAPI` in `atomic_wm/plugins/campaign.py` is
  the orbit-backed implementation; the tests use a scripted fake).

The plugin owns state, persistence and the REST surface, and knows nothing
about *how* the plan was produced.

## Concept mapping

| Campaign Manager (`cm/`) | this demo | note |
|---|---|---|
| **campaign** = DAG of workflow *groups* (wired in YAML: chains, fan-out, fan-in, diamonds) | one campaign = **one group** with N instances | our "DAG" has a single node; the stage sequence inside a workflow is not a CM DAG, it is one workflow's internal pipeline |
| `_WorkflowInfo` (group: `replicas`, `dependencies`, `priority`, `required_cpus/gpus/memory`, `concurrency_floor/cap`, `status`) | `Campaign.workflows` + `max_concurrent_workflows` | our per-stage `requirements` (`cores`, `gpus`, `software`) are the analogue of the group's resource requirements — but they are *matched by the federation*, not by a local pool |
| `BaseWorkflow.run(replica_id)` — a python callable per replica | one `WorkflowInstance` whose stages are **task command lines** | a CM workflow is in-process python; our stage is a task submitted to a remote resource |
| `ResourcePool` (total CPUs/GPUs/memory tracked locally, two-pass greedy scheduler) | the `federation` plugin's registry + `pick`/`submit` (capabilities, node-hour budget, liveness, score) | this is the substantive difference: placement moves from a local counter to a broker-side federation across *sites* |
| `ExecutorMixin._run_replica` (allocate → entry point → `_handle_replica_done`) | `StageRunner._run_stage` (submit → stage-in → poll → collect → manifest) | same shape, different execution substrate |
| `on_replica_done(replica_id, cm, final_state)` hook, `_signal_done()` / `_trigger_dependent()` | our stage-done hook: the terminal branch of `_run_stage` (collect + manifest + feed `produced` into the next stage) plus the `on_change` callback the plugin persists on | a CM `on_replica_done` that calls `trigger_dependent` is exactly where "run the next group" would hook in |
| `campaign_target: N` / ADR goals (stop when enough is done) | none — every sweep point runs to the end | an adaptive stop is the first thing the real CM would add |
| `cm.status()` / `stats()` / `CampaignMetrics` | `GET campaign/{sid}/{cid}`, `GET results/{sid}/{cid}` | our records are JSON-first because the UI reads them directly |

## How to swap in the real Campaign Manager

1. **Planner.** Implement `CampaignPlanner.expand()` on top of the CM's
   plan: build a `CampaignPlan` / group config from the request and return
   one `WorkflowInstance` per replica the CM would start. The sweep becomes
   one group with `replicas = len(points)`; a multi-group DAG returns the
   instances of every group, with the group name folded into `wf.id`.
   Nothing else in the plugin changes — it only ever sees
   `WorkflowInstance`s.

2. **Executor.** Give the CM a workflow class whose `run()` calls this
   package's `StageRunner._run_stage` (or, more directly, a
   `FederationAPI`) instead of doing local work, and let the CM own
   concurrency:

   ```python
   class FederatedWorkflow(BaseWorkflow):
       async def run(self, replica_id):
           await stage_runner.run_workflow(campaign, instances[replica_id])
       async def on_replica_done(self, replica_id, cm, final_state):
           await self._trigger_dependent('analysis', replicas=1)
   ```

   `CampaignRunner` (semaphore over `asyncio.gather`) is then replaced by
   `AsyncCampaignManager.start()/wait()`, and `ResourcePool` should be left
   **unlimited** — the federation, not the CM, decides where a task lands.

3. **Resources.** The CM's resource pool and our federation overlap: the
   CM counts local CPUs/GPUs, the federation matches declared capabilities
   and node-hour budget across sites. Keep exactly one of them authoritative
   (the federation) or the two will fight over placement.

4. **State and UI.** Keep writing `Campaign`/`WorkflowInstance`/`StageRun`
   records — the persistence, the routes, the store layout and the Explorer
   module are written against them, not against the planner.

## What the demo deliberately does not do

- no DAG between workflow groups (no fan-in, no diamonds),
- no adaptive scheduling / stopping goals / ADR layer, no bandit or
  surrogate scoring, no backpressure or budget controller,
- no retries, no replanning, no candidate triage,
- no in-process python workflows: every stage is a task command line, which
  is what makes the federation the interesting part.

Those are exactly the features `cm/` already has — which is why the seam is
drawn where it is: the demo owns *placement across resources and the data
path*, the real CM owns *what to run next and when to stop*.

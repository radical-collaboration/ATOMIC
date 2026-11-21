# P11 — Campaign Manager on ORBIT (service endpoint next to the broker)

Repos: `/home/merzky/projects/atomic` (campaign plugin, runner, UI),
`/home/merzky/radical/radical.orbit` (gateway, federation, dispatcher), and
Masha's AuCaM, `github.com/radical-project/campaign_manager` @ `devel`
(clone `~/radical/campaign_manager`; the plan targets 6842888, which holds the
former `aucam-v2`). Draft of 2026-11-06, revised 2026-11-19 and 2026-11-21
after the plan review (verdict "revise", findings in §7).

Read first: `04-atomic-campaign.md` (the campaign plugin as built),
`08-federation-class-pools.md` (federation, class pools), AuCaM's
`CLAUDE.md`, `docs/architecture-overview.md` and `docs/known-gaps.md`.

**The change in one paragraph.** The ATOMIC portal and the Campaign Manager
run in one service process next to the broker: an ORBIT **endpoint**
(`ep_cm`) hosting the campaign plugin. The broker keeps routing, the
federation and the dispatcher. It no longer runs campaign steering. The
plugin keeps its REST surface and Explorer UI, which the gateway reaches
through its proxy. Campaigns are planned and steered by AuCaM, one
`CampaignManager` per campaign. Every frame is one AuCaM workflow whose body
drives ATOMIC's `StageRunner` through the federation. The existing sweep
planner stays behind a switch as a debugging baseline. Campaigns survive a
broker restart; a CM restart is not covered.

---

## 1. Decisions (Andre, 2026-10-27 .. 2026-11-21)

| Decision | Consequence |
|---|---|
| CM is an ORBIT plugin on a service endpoint, not in the broker process | controller code and campaign load are isolated from the shared control plane; same plugin code either way |
| One broker for all of ATOMIC; must scale to many concurrent workflows over dynamic resources | push notifications over polling (after the demo); bulk data never through the broker |
| Campaigns must survive a broker restart; CM restart is out of scope for now (2026-11-11) | the runner pulls state; transient errors are budgeted, not fatal (§3.3) |
| The 12-04 demo must show the AuCaM integration (2026-11-21) | AuCaM is on the critical path; the go/no-go is about real bigsim vs synthetic stages, not about AuCaM |
| Controllers: expected stable across ATOMIC workflows (curated) | ship a `BigsimController`; endpoint placement keeps user code possible later |
| One shared ATOMIC identity for now | no per-user authz in this plan |

## 2. Spike — the plugin on an endpoint (2026-11-06)

Local broker + endpoint `ep_cm` hosting `atomic_campaign`, ORBIT `devel`
@ a843434; the broker-only gate patched from outside the repo.

| Question | Result | Evidence |
|---|---|---|
| Plugin loads on an endpoint | no: broker-only by design | `atomic_wm/plugins/campaign.py:379` (`is_enabled` -> `host_role == 'broker'`) |
| REST through the gateway proxy | works: `GET /ep_cm/atomic_campaign/campaigns/default` -> 200 | gateway catch-all `/{endpoint}/{ns}/...` |
| Explorer JS for an endpoint-hosted plugin | works since radical.orbit #169 (merged 2026-11-11), opt-in via `$RADICAL_ORBIT_ENDPOINT_UI_MODULES=atomic_campaign` on the broker | `gateway.py` `_route_plugin_js` / `_endpoint_ui_module` |
| Explorer API calls once the JS loads | go to the hosting endpoint (`createPluginAPI(endpointName, pluginName)`) | `data/orbit_explorer.html` |
| Submit from the endpoint | not exercised: the plugin reaches the federation in-process, which exists only on the broker | `campaign.py:103-145` (`_FederationAPI`) |

## 3. Design

### 3.1 Placement and the remote federation API

- `PluginAtomicCampaign.is_enabled`: endpoint hosts allowed. `ep_cm` is a
  participant with no compute; it never joins the federation and runs with
  `--plugins atomic_campaign` only.
- A remote `FederationAPI` next to the in-process one (which stays for
  broker hosting and tests):
  - Every call goes through `EndpointRuntime.call` **in a thread**
    (`asyncio.to_thread`). `call` is synchronous and blocks on a future
    scheduled on the endpoint's own work loop (`runtime.py:1033-1048`); called
    from a plugin coroutine it deadlocks.
  - Every call carries an explicit timeout of about 15 s. Pending calls
    survive a reconnect by design (`runtime.py:706-712`) with the 600 s
    default (`runtime.py:94`), so without it one poll can hang for 10 min.
  - Routes: the federation `submit` / `task` / `cancel` / `resources` on
    `dst='broker'`, the federation's reserved `default` session (reachable
    remotely, `plugin_base.py:178-181`).
- Staging: `staging_get` needs `app.state.broker_caller`
  (`campaign.py:317-336`), which an endpoint does not have
  (`runtime.py:228-233`); `stage_out` reads the scratch directory on the
  broker host. Remote replacement: `StagingClient` over
  `_RuntimeHTTP(runtime, child_endpoint)`, in a thread.
- Client paths: `client.py:40` and `:375` hard-code `/broker/atomic_campaign`;
  the endpoint name becomes a setting (default `broker` for today's setup).

### 3.2 AuCaM integration

AuCaM is pinned to `devel` @ 6842888 for the demo. Its API changed several
times since this plan was first written; the demo cannot absorb another
change.

- **One `CampaignManager` per campaign.** `run()` refuses a second run
  (`manager.py:62-63`). All managers share one RHAPSODY backend passed in, so
  each does not build its own thread pool. The CM venv needs Python >= 3.11.
- **Signal handlers are moot here.** The endpoint's loop runs in the
  `orbit-work` thread (`runtime.py:426-431, 582-591`);
  `loop.add_signal_handler` raises off the main thread and AuCaM swallows it
  (`manager.py:165-167`). The endpoint owns shutdown and calls `stop()`.
- **A frame is one AuCaM Python workflow.** Its body drives `StageRunner` for
  that frame. It reports per-stage metrics through AuCaM's in-process data
  backend; there is no workflow-side network path to the CM.
- **Budget values** (required since 5b779bf, `spec.py:156-175`): per-workflow
  `spending_limit={"node_hours": X}` and `resources`, the campaign
  `capacity` = frames × resources. The block reports running `node_hours`
  totals from the federation's task info. Otherwise AuCaM charges resources ×
  wall time, which includes the batch-queue wait (`campaign.py:2122-2125`).
- **AuCaM timeouts:** `max_wall_time_s` and `stale_timeout_s` are None, or above
  queue wait plus a broker outage. Otherwise AuCaM cancels frames that are
  queued or waiting through an outage.
- **`BigsimController`** puts the per-frame score into `obs`, because goals
  read top-level `obs` keys (`campaign.py:2863`). It cancels poor frames,
  stops at the budget or goal, and returns a summary.
- **Task ids unique per launch**, derived from AuCaM's `wf_id`. Today's
  `<cid>-<wf.id>-<stage>` (`runner.py:328`) assumes the sweep's fixed
  instances. AuCaM relaunches workflows (for example after a limit cancel),
  and the dispatcher answers a known DONE id with the cached result and
  attaches to a RUNNING one (`plugin_task_dispatcher.py:2073-2088`).
- **Cancel reaches ORBIT.** AuCaM cancels through the block future and frees
  the slot at once (asyncflow `workflow_manager.py:1787-1789`), while the ORBIT
  task may still run. `StageRunner` gets a shielded cancel with retries plus a
  persisted pending-cancel list, drained after a reconnect. This also fixes
  today's asyncio-cancel path, which marks the stage CANCELED without
  cancelling the task (`runner.py:295`).
- **UI:** AuCaM launches map onto ATOMIC's `Campaign` / `WorkflowInstance`
  state, so the Explorer panel shows them like sweep instances.
- **Planner switch** `sweep|aucam` in the campaign submit. Both share §3.1
  and §3.3. The sweep is the debugging baseline only; the demo fallback is a
  screencast of a successful AuCaM rehearsal.

### 3.3 Surviving a broker restart

What the code does today, and what changes:

- The runner pulls state: it polls every task it owns (`runner.py`
  `_poll_task`). Notifications are not needed for correctness: cancel is
  fire-and-forget, and AuCaM's metrics are in-process.
- **The replay race (ORBIT).** The federation sets `_replayed=True` before it
  awaits `_replay_attachments` (`plugin_federation.py:1938-1940`). Polls during
  that await reach the dispatcher before the `fed` session is re-registered
  and get `404 unknown session: fed` (`plugin_task_dispatcher.py:3755-3759`).
  The runner turns that into `TaskNotFound` and fails the stage
  (`runner.py:578-582`). A failed `_register_fed` is never retried
  (`plugin_federation.py:2024-2030`). This is ORBIT demo item 3 (§4).
- **Transient vs fatal.** A time-based unreachable budget (about 5 min)
  replaces `MAX_POLL_FAILURES = 10` (`runner.py:52`, ~20 s) for these
  transient errors:
  - transport errors: ConnectionError, TimeoutError;
  - a 503 with Retry-After (radical.orbit #166);
  - a 410 "shutting down" (radical.orbit #165, `plugin_base.py:432-434`);
  - a 404 whose detail reads "unknown session".

  Any other 404 still fails fast.
- **Submit retry.** Today submit has no retry (`runner.py:345`). It gets one,
  safe together with ORBIT demo item 2 (a known task_id goes to its recorded
  pool; the ledger is persisted in the federation's `state.json` and saved on
  every submit).
- **Lost pilot events (to be measured).** The dispatcher's `task` route answers
  from its own record (`plugin_task_dispatcher.py:2337-2341`), which advances
  only on pilot events. Events emitted during an outage wait in the pilot
  runtime's queue and mostly arrive later (`runtime.py:598, 888-896`). Two
  cases are lost:
  - the frame in flight when the connection drops;
  - dispatcher persists that were coalesced but not yet written when the
    broker is killed.

  A task hit by either stays RUNNING until the stage timeout (900 s). The
  rehearsal decides whether ORBIT adds the #149 slice "re-query RUNNING tasks
  when a pilot reconnects".
- **CM crash.** `load_campaigns` marks running campaigns INTERRUPTED without
  cancelling anything (`state.py:366`), which orphans HPC jobs. Fix: a
  best-effort cancel of the recorded task ids. Plugin `shutdown()` (cancelling
  the drivers, `campaign.py:660-664`) must decide explicitly whether it
  cancels the ORBIT tasks.

### 3.4 Scale and data (after the demo)

- Push, not poll: dispatcher task notifications are still session-less
  broadcasts. ORBIT follow-up: stamp them with the submitting session on top
  of #146/#147. At demo scale polling is fine; at about 1000 concurrent
  stages it is several hundred requests/s through the broker.
- Bulk data site to site; the broker carries references only. Today inputs ride
  base64 in the submit body, now with an extra hop (CM → broker), and outputs
  return through the broker, bounded by the 4 MiB frame cap
  (`protocol.py:56`).
- Load test with synthetic stages, 100 and 1000 workflows.

## 4. Dependencies

**ORBIT, worked in the ORBIT session.** Demo scope agreed 2026-11-21:
1. The dispatcher requeues on a retryable 503 instead of failing the task.
2. The federation submit routes a known task_id to its recorded pool, with an
   explicit error if that pool is gone.
3. The federation replay race (§3.3): wait for the replay, retry
   `_register_fed`.
4. Pilots replayed after a broker restart are paused until their endpoint
   reconnects (it is unknown to the new broker for up to ~10 s of backoff).
   After 300 s absent, the pilot is failed and its tasks requeue elsewhere.
   Without this, queued and new tasks fail in that window. (Found by the
   ORBIT session, 2026-11-21.)
5. Pending the rehearsal: the #149 slice for lost pilot events.

Contract for the runner after these ORBIT PRs:
- a federation submit with a known task_id returns the existing record
  (DONE / QUEUED / RUNNING); FAILED / CANCELED re-runs in the recorded pool;
- 409 means a known id whose class has no members now: fail the stage;
- 503 is transient everywhere: replay pending, or a suspect endpoint.

Staging-put is on the demo path: Perlmutter and Odo join with
`shared_fs=false` (`demo/2026_09_08/env.sh:1134-1207`), and inputs travel as
`inputs_b64` in the submit (`runner.py:351-361`). Paused or requeued tasks must
keep their inputs across a restart.

Deferred: #149 in full, #148 generic idempotent submit, session-scoped
dispatcher events, #150.

**AuCaM.** A PR in its own session adds a signal-handler opt-out and goals
evaluated on workflow metrics. Neither is on the demo's critical path (§3.2).
The demo pins 6842888.

**Bigsim.** The prerequisites are open as of 2026-11-21: the validated
scripts, a test frame plus model files, and accounts. Without them the demo
runs synthetic stages only.

## 5. Synthetic workload for the demo

AuCaM must have something to steer. Synthetic stages produce scores with a
spread, some frames clearly poor, and node-hour costs, so a run shows:
- poor frames cancelled;
- the campaign stopping at its budget or goal;
- a summary in the outcome.

Build it from the existing workload tools (`atomic_wm/workload/`) with a seed
per frame, so a rehearsal is reproducible.

## 6. Build order and schedule (demo 2026-12-04)

| By | Step |
|---|---|
| 11-25 | (a) §3.1 plumbing: the existing runner works on `ep_cm` (remote FederationAPI, staging, client endpoint path, error classes and unreachable budget, submit retry). (b) In parallel, the AuCaM adapter against a fake federation: one manager per campaign, the frame block, `BigsimController`, unique task ids, the cancel fix, `node_hours` reporting, the synthetic workload. |
| 11-26 | Broker-restart rehearsal on synthetic stages with the sweep baseline: a graceful stop and a `kill -9`, a task finishing during the outage, about 10 concurrent polls. The result fixes the final ORBIT scope. |
| 12-01 | AuCaM on `ep_cm` over the federation; rehearsal including a broker restart; record the screencast. |
| 12-03 | Demo prep and dry run: deck, real resources. One real bigsim frame only if it passed by hand. |

11-26 to 11-29 is Thanksgiving; little US-side availability. 12-01 is the
tight step. If it slips, the screencast of the last good rehearsal is what
the demo shows.

Can slip past 12-04: push notifications, the site-to-site data path, the load
test, per-user auth, the AuCaM PR. Cannot slip: broker-restart survival and
the AuCaM integration.

## 7. Plan review (2026-11-21) — dispositions

Verdict: revise. All findings are taken into §3–§6:

| Finding | Where |
|---|---|
| BLOCKING: the replay race fails stages after a broker restart | §3.3, §4 ORBIT item 3 |
| BLOCKING: the remote-call mechanics (thread, timeouts, transient errors, submit retry) | §3.1, §3.3 |
| BLOCKING: staging does not work from an endpoint | §3.1 |
| SHOULD: polling assumes no lost pilot events | §3.3, rehearsal in §6 |
| SHOULD: task ids and cancel under AuCaM, CM crash orphans | §3.2, §3.3 |
| SHOULD: the plan was out of date with current AuCaM | §3.2 |
| SHOULD: the client hard-codes `/broker/atomic_campaign` | §3.1 |
| NIT: frame cap, stale refs, schedule, Thanksgiving | §3.4, header, §6 |

Recommendation taken with one change: AuCaM is not behind a go/no-go
(decision of 2026-11-21); the sweep planner stays as the debugging baseline.

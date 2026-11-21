# P11 — Campaign Manager on ORBIT (service endpoint next to the broker)

Repos: `/home/merzky/projects/atomic` (campaign plugin, runner, UI),
`/home/merzky/radical/radical.orbit` (gateway), and Masha's
`github.com/radical-project/campaign_manager` @ `aucam-v2` (AuCaM; reviewed
at 3dd14de; unchanged as of 2026-11-19). Draft of 2026-11-06, revised
2026-11-19 -- spike results plus the design skeleton; under review (see
`07-review-protocol.md`).

Read first: `04-atomic-campaign.md` (the campaign plugin as built),
`08-federation-class-pools.md` (federation, class pools), AuCaM's
`docs/architecture-overview.md` and `docs/known-gaps.md`.

**The change in one paragraph.** The ATOMIC gateway/portal and the Campaign
Manager (AuCaM) run in one service process next to the broker: an ORBIT
**endpoint** hosting the campaign plugin. The broker keeps routing, the
federation and the dispatcher; it no longer runs campaign steering. The
plugin keeps its REST surface and Explorer UI (the gateway reaches both
through its proxy); AuCaM replaces the `SweepPlanner` / `CampaignRunner`
loop; every bigsim frame is one AuCaM workflow whose stages run through the
federation. Campaigns survive a broker restart; a CM restart is not covered
yet.

---

## 1. Decisions (Andre, 2026-10-27 .. 2026-11-05)

| Decision | Consequence |
|---|---|
| CM is an ORBIT plugin on a service endpoint, not in the broker process | controller code and campaign load are isolated from the shared control plane; same plugin code either way |
| One broker for all of ATOMIC; must scale to many concurrent workflows over dynamic resources | push notifications over polling; bulk data never through the broker |
| Campaigns must survive a broker restart; CM restart is out of scope for now (2026-11-11) | reconcile on reconnect, idempotent submit; the task-id mapping in the CM checkpoint (AuCaM R1) is deferred |
| Controllers: expected stable across ATOMIC workflows (curated), not certain | ship a bigsim controller; endpoint placement keeps user code possible later |
| One shared ATOMIC identity for now | no per-user authz in this plan |

## 2. Spike — the plugin on an endpoint (2026-11-06)

Local broker + endpoint `ep_cm` hosting `atomic_campaign`, ORBIT `devel`
@ a843434; the broker-only gate patched from outside the repo.

| Question | Result | Evidence |
|---|---|---|
| Plugin loads on an endpoint | no: broker-only by design | `atomic_wm/plugins/campaign.py:379` (`is_enabled` -> `host_role == 'broker'`) |
| REST through the gateway proxy | works: `GET /ep_cm/atomic_campaign/campaigns/default` -> 200 | gateway catch-all `/{endpoint}/{ns}/...` |
| Explorer JS for an endpoint-hosted plugin | 404 before the ORBIT change below; 200 and byte-identical with it | `gateway.py:538` `_route_plugin_js`, `:565` `_endpoint_ui_module` (branch `feature/endpoint-ui-modules`) |
| Explorer API calls once the JS loads | go to the hosting endpoint (`createPluginAPI(endpointName, pluginName)`) | `data/orbit_explorer.html` (`fetch` builds `${ns}/${endpoint}`) |
| Submit from the endpoint | not exercised: the plugin reaches the federation in-process, which exists only on the broker | `campaign.py:103-145` (`_FederationAPI` via `app.state.endpoint_service.handle_request`) |
| Regression found on the way | ORBIT #157 added `Plugin._timeout()`; the plugin's stage-timeout attribute shadowed it -> every POST 500 | fixed on ATOMIC PR #1, ec16ea6 |

**ORBIT change** (radical.orbit #169, merged 2026-11-11 as 2835b38):
built-in `GET {namespace}/ui_module` on every plugin, `ui_module: true|false`
in the topology entry, gateway fallback after packaged and broker-hosted JS,
opt-in via `$RADICAL_ORBIT_ENDPOINT_UI_MODULES` (endpoint JS runs under the
broker's origin). The ATOMIC broker sets it to `atomic_campaign`.

## 3. Design

### 3.1 Placement and wiring

- `PluginAtomicCampaign.is_enabled`: endpoint hosts allowed (the CM endpoint
  is a participant with no compute; it must never join the federation).
- `_FederationAPI` gets a remote implementation over the broker client
  (`dst='broker'`, the same federation / dispatcher / staging routes); the
  in-process one stays for broker hosting and tests. Every call is a network
  round trip that can fail: bounded retries, and see 3.3.

### 3.2 AuCaM integration

- One `CampaignManager` per plugin session; it does not install signal
  handlers (AuCaM change, `manager.py:489`), the endpoint owns shutdown.
- A bigsim frame is one AuCaM Python workflow whose body drives the ATOMIC
  `StageRunner` for that frame; per-stage metrics go through AuCaM's
  in-process data backend (no workflow-side network path to the CM).
- `BigsimController`: puts per-frame metrics into `obs` (goals read `obs`
  top-level keys only, `campaign.py:1556`), cancels poor frames, summary.
- Spending: a `node_hours` dimension from the federation's task info, declared
  estimate as fallback.
- Cancel reaches the ORBIT task: today an asyncio cancel of the stage marks it
  CANCELED and re-raises without cancelling the task (`runner.py:295`).

### 3.3 Surviving restarts

- **CM restart (deferred, 2026-11-11).** AuCaM re-enqueues running workflows at HIGH priority
  (`campaign.py:1066`, requester `cm:recovery`) -- with ORBIT the tasks are
  still running, so that duplicates work. The checkpoint must carry, per
  running workflow, its ORBIT task ids (the ATOMIC store already records
  `stage.task_id`); restore rejoins them by polling instead of relaunching
  (AuCaM gap R1).
- **Broker restart.** The dispatcher persists pools and in-flight tasks
  (`plugin_task_dispatcher.py:1125`); the federation replays its registry.
  The CM endpoint loses its socket and misses notifications (the broker keeps
  none; `replay` is optional and bounded, `plugin_replay.py:29`). Rule:
  notifications are hints, state is pulled.
  - ATOMIC's runner already pulls: it polls every task it owns
    (`runner.py` `_poll_task`), so it reconciles by construction once the
    broker is back. What breaks today is the failure budget:
    `MAX_POLL_FAILURES = 10` consecutive failed polls at 1-3 s fail the stage
    after ~20 s, shorter than a broker restart. Change: a time-based
    unreachable budget (e.g. 5 min) for transport failures, separate from
    the 404 fast-fail.
  - The generic ORBIT-side reconcile (client re-reads task state after a
    reconnect or a dropped notification) is ORBIT #149, worked in an ORBIT
    session; this plan does not depend on it while the runner polls.
- **Idempotent submit.** A submit whose reply is lost must not become a second
  task. The runner's task ids are deterministic (`<cid>-wf-NNN-<stage>`).
  Verified 2026-11-21 (with the ORBIT session): the dispatcher pool submit is
  idempotent by task_id, but the federation `_route_submit` re-picks a class
  pool before consulting its ledger, so a retry can duplicate across pools.
  ORBIT fix for the demo: route a known task_id to its recorded pool (the
  ledger is persisted in the federation's state.json). Generic #148 is
  deferred; the CM does not use RhapsodyClient multi-batch submits.

### 3.4 Scale and data

- Push, not poll -- after the demo: the dispatcher's task notifications are
  still session-less broadcasts (ORBIT follow-up: stamp them with the
  submitting session on top of #146/#147). At demo scale (~10 frames) 1-3 s
  polling is fine; at ~1000 concurrent stages it is several hundred
  requests/s through the broker.
- Bulk data site to site (Globus / staging between resources); the broker
  carries references only. Today inputs ride base64 in the submit body and
  outputs return through the broker, bounded by the ~8 MB frame cap.
- Load test before the integration run: synthetic stages, 100 and 1000
  workflows, notifications vs polling, and the cost of the dispatcher's and
  the plugin's state-file writes.

## 4. Changes needed from AuCaM (mail to Masha drafted 2026-11-19, not sent)

1. (deferred) Restart rejoin by external task id (R1).
2. Opt-out of signal-handler installation when embedded.
3. Goals evaluated on metrics the controller exposes (or documented: put them
   into `obs`).
4. Guard around `decide()` (C1).

## 5. Open items

- Verify the ORBIT broker-restart path live (the r3 re-verify TODO of plan
  122 is still open).
- bigsim standalone run and its workflow form are prerequisites (status
  unknown as of 2026-11-19).
- ORBIT scope for 12-04, agreed with the ORBIT session 2026-11-21:
  (1) the dispatcher requeues on a retryable 503 instead of failing the task;
  (2) the federation submit routes a known task_id to its recorded pool.
  Deferred past the demo: #149 reconcile (the runner polls), #148 generic
  idempotent submit, session-scoped dispatcher events, #150 subscribe dedup.
  The live broker-restart rehearsal decides whether anything else is in the
  way.
- AuCaM's asks (section 4) unanswered; the plan works around 1 (signal
  handlers: patch or subclass) and 2 (controller fills `obs`) if needed.

## 6. Schedule (demo 2026-12-04)

The original schedule (integration run 11-20) has slipped. Proposed
(2026-11-19, not yet agreed):

| By | Step |
|---|---|
| 11-20 | this plan reviewed; AuCaM mail sent |
| 11-25 | CM endpoint plugin: remote federation API, AuCaM driving the runner, cancel fix, time-based unreachable budget; restart rehearsal on a synthetic campaign |
| 11-27 | integration run on synthetic stages, 2 resources, one planned broker restart; bigsim level 1 (one frame by hand) if the prerequisites arrived |
| 12-02 | demo prep: deck, rehearsal on real resources, screencast fallback |

Proposed scope cut for 12-04: AuCaM steering synthetic stages on the
federation, plus one real bigsim frame only if it passed level 1 by 11-27.
Can slip past 12-04: push notifications, the site-to-site data path, the
load test, per-user auth. Cannot slip: broker-restart survival.

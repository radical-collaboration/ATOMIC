# P1 — `federation` plugin (radical.orbit, broker-hosted)

Repo/branch: `/home/merzky/radical/radical.orbit` @ `feature/atomic-federation`.
Contract + verified facts: `00-overview.md` (read the Facts section in
full — every design choice below follows from a verified constraint).
This plugin is **generic Orbit** — nothing ATOMIC-specific in names, docs
or defaults.

## Sanctioned dispatcher touches (the only ones)

1. **`min_pilots` floor** in `task_dispatcher_strategy_conservative.py`
   `on_tick` — exact guard structure (verified against lines 134-163):
   compute `live_count` first (move the computation now at ~:158 above
   both early returns); `need_floor = live_count < pool.min_pilots`;
   replace `if not pending: return` (:134) with `if not pending and not
   need_floor: return`; replace `if len(pending) <= free_capacity:
   return` (:149) with `if not need_floor and len(pending) <=
   free_capacity: return`. Backoff (:139), in-flight (:153),
   `max_pilots` (:158) and dwell (:163) guards stay as they are.
   `_submit_pilot`/`_do_pilot_submit` have no backlog check, so nothing
   else refuses. Tests in `test_task_dispatcher_strategy_conservative.py`:
   empty queue + `min_pilots=1` → exactly one submit; `min_pilots=0` →
   none; `min_pilots=1, max_pilots=1` with one live PENDING pilot → no
   second submit. Rationale: "the allocation's pilot starts at join" is
   demo step 2.
2. **Pilot history**: add `finished_at: float | None = None` to
   `PilotRecord` (`task_dispatcher_state.py:74-90`; `record_from_dict`
   drops unknown keys, so old state files load fine); set it in
   `_finalize_pilot` (the single terminal path — verified). In the
   verbose pool summary add `summary['pilot_history'] =
   [self._pilot_dict(p) for p in ps.pilots.values()]` (all `asdict`
   fields incl. `size_key`, `active_at`, `finished_at`,
   `child_endpoint_name`); the federation resolves nodes via
   `summary['pilot_sizes'][size_key]['nodes']`. Two lines. Tests: state
   round-trip with `finished_at`; summary includes a FAILED pilot after
   `_finalize_pilot`.
3. **Orphan-pool guard** (one line): in `_housekeeping`, skip `on_tick`
   for pools whose `owning_sid not in self._sessions` — `_replay_state`
   re-materialises every state dir at broker start with no session, and
   with touch 1 an orphan `min_pilots=1` pool whose endpoint is gone would
   otherwise submit a pilot per backoff window forever. Test: replayed
   pool without session gets no tick.

Nothing else in the dispatcher changes. Existing tests must stay green.
`DEFAULT_PLUGINS_BY_ROLE` lives in `plugin_host_base.py:23`; the
`__init__.py` edit is only the import line.

## Files

- `src/radical/orbit/plugin_federation.py` — `PluginFederation(Plugin)`,
  `plugin_name = 'federation'`, broker-only (`is_enabled(app)` as
  `iri_connect` does). Register in `__init__.py`; add to
  `DEFAULT_PLUGINS_BY_ROLE['broker']`.
- `src/radical/orbit/federation_state.py` — `ResourceRecord`,
  `ResourceUsage`, `SubmitLedgerEntry`, `FederationState` (JSON persist /
  load with atomic rewrite, as `task_dispatcher_state.py`).
- `src/radical/orbit/federation_policy.py` — `FederationPolicy` base
  (`pick(requirements, resources) -> (record, score) | None`,
  `explain(requirements, resources) -> {name: reason}`), default
  `BudgetLoadPolicy`, `make_policy(spec)` accepting `module:Class` (same
  spirit as `task_dispatcher_policy._import`).
- `src/radical/orbit/data/plugins/federation.js` — minimal Explorer page
  (resources table: name, endpoint, mode, site, cores/gpus/mem, software,
  node-hours used/remaining, pilots active, tasks running/done,
  liveness); poll every 3 s; structure after `task_dispatcher.js`.
- `docs/plugin_federation.md`; `docs/rest_api.md` gains the routes;
  `mkdocs.yml` nav if plugin docs are listed there.
- Tests: `tests/unittests/test_federation_policy.py`,
  `test_federation_state.py`, `test_plugin_federation.py`, plus the two
  dispatcher-touch tests above.

## Behaviour

- **Sessions**: define `session_class = FederationSession` (a trivial
  `PluginSession` subclass, ctor `(sid)`) — `_ensure_default_session`
  raises without one. All routes use sid `default`; handlers call
  `self._ensure_default_session()` first. Dispatcher sessions created by
  the federation are registered with `{"sid": "fed-<name>", "pools":
  [pool], "lifetime": "persistent"}` (verified: `Plugin.register_session`
  reads `sid`/`lifetime` from the body and returns `{"sid": ...}`;
  re-sending the identical body reconnects, 409 only if lifetime differs)
  and unregistered explicitly on leave. Test: after a join, monkeypatch
  `time.time` forward by > 3600 s, run `_cleanup_expired_sessions()`,
  assert session and pool survive (a plain call would pass vacuously).
- **Calling the dispatcher**: `host = self._app.state.endpoint_service`;
  `resp = await host.handle_request(method, '/task_dispatcher/<route>',
  {}, body_bytes)` — signature `(method, path, headers, body_bytes,
  query_string='')`, path is endpoint-relative (no `/broker`), returns a
  starlette `JSONResponse` (decode `resp.body`), raises `HTTPException`
  on errors (404 unknown route) — map codes through.
  Resolve lazily per call; if `task_dispatcher` is not hosted → 503
  `dispatcher plugin not available`. Wrap in a small `_DispatcherAPI`
  helper (register_session, submit, task, pool_detail, cancel_all,
  unregister_session) so tests can substitute a fake.
- **join**: validate the record (name pattern `^[a-z0-9_.-]+$`, mode,
  capability types, `scratch_base` under `~` or `/tmp`); 409 on duplicate
  name; 404 if `endpoint` is not a connected participant (topology from
  `on_topology_change`); build the `PoolConfig`:
  - common: `name=f'fed-{name}'`, `endpoint_name=endpoint`,
    `scratch_base=record.scratch_base or ~/.radical/orbit/federation/
    scratch/<name>`, `strategy='conservative'`, `strategy_config=
    {'min_dwell_sec': 5, 'max_in_flight_submissions': 1}`,
    `rhapsody_backend` default `'concurrent'`.
  - `allocation`: `queue='allocation'`, `account=None`, `min_pilots=1`,
    `max_pilots=1`, size from the endpoint's `queue_info/job_allocation`
    (`n_nodes`, remaining `runtime`) when present, else `nodes=1`,
    `walltime_sec=3600`; `cpus_per_node`/`gpus_per_node` from declared
    capabilities (fall back to `sysinfo` metrics — register a sysinfo
    session, `GET metrics/{sid}`, unregister). Budget default
    `nodes × walltime_sec/3600`.
  - `login`: everything from `record.pool` (validate required: queue,
    account may be null, nodes, cpus_per_node, walltime_sec); defaults
    `min_pilots=0`, `max_pilots=1`; budget **required**.
  Then register the dispatcher session, store `dispatcher_sid`,
  `pool_name`, persist, return the full record.
- **usage** (on `resources`/`resource`/`pick`, cached 2 s, async with a
  3 s timeout; on failure keep last values and set `"stale": true`):
  `node_hours_used = Σ over pilot_history entries that reached ACTIVE of
  nodes × ((finished_at or now) − active_at) / 3600`; `pilots_active` =
  live pilots; `tasks_running/tasks_done/tasks_failed` from the
  federation's own submit ledger (updated when `task/{sid}/{task_id}` is
  polled or on `pilot_status`/`task_status` notifications if the plugin
  subscribes to `app.state.broker_tap` the way the dispatcher does —
  optional, polling is enough for v1).
- **pick / submit / task**: per contract. `submit` creates
  `<scratch_base>/<task_id>` up front (cwd default), forwards to the
  dispatcher `submit/{dispatcher_sid}` with `pool`, records the ledger
  entry `{task_id, resource, submitted_at, state}`, returns
  `{"task", "resource", "pool", "dispatcher_sid"}`. `task/{sid}/{task_id}`
  looks the resource up in the ledger, proxies the dispatcher's
  `task/{dispatcher_sid}/{task_id}`, updates the ledger state, and
  returns the task dict plus `"resource"` and, while the pilot is live,
  `"child_endpoint"` (from the verbose pool summary via `pilot_id`) — the
  campaign plugin needs it for pilot-side output collection.
- **leave**: first `POST cancel/{dsid}/{task_id}` for every non-terminal
  task in the ledger (session teardown would otherwise re-queue RUNNING
  tasks and persist them, and a later `join` of the same name would
  dispatch those stale tasks to the new pilot), then `cancel_all`,
  `unregister_session`, remove record + ledger, persist.
- **liveness**: from topology; a resource inherits its endpoint's
  liveness; `lost` excluded by the policy.
- **restart**: load state; re-register **every** stored resource's
  dispatcher session first (`{"sid": stored, "pools": [identical],
  "lifetime": "persistent"}` — the dispatcher already replayed the pool
  state), then `unregister_session` those whose endpoint is not connected
  (teardown drops the pool from `_pool_states`, so no ticks) and mark
  them `lost`. Test it with the dispatcher's own replay path.
- **state root**: `~/.radical/orbit/federation/<instance>/`, overridable
  by `RADICAL_ORBIT_FEDERATION_STATE` (tests + demo isolation).

## Tests

- policy: capability filtering (cores/gpus/software ⊆), budget
  exhaustion excludes, liveness excludes, load term orders two otherwise
  equal resources, deterministic tie-break by name, `explain` reasons.
- state: persistence round-trip; usage arithmetic (ACTIVE without end
  uses now; never negative; pilots that never reached ACTIVE count 0).
- plugin (mirror `_make_plugin`/`_register` from
  `test_plugin_task_dispatcher.py`; `app.state.is_broker=True`; fake
  `_DispatcherAPI` for unit tests, real co-hosted dispatcher + fake pilot
  from `test_task_dispatcher_broker.py` for one integration test): join
  allocation + login → dispatcher session exists, pool bound to endpoint,
  `min_pilots` honoured for allocation; duplicate 409; unknown endpoint
  404; no dispatcher 503; `resources` lists both with zero usage;
  `pick` chooses by capability and rejects with reasons; `submit` lands
  the task in the right pool; `task` proxies + ledger updates; `leave`
  removes and cancels; expired-session sweep does not kill pools;
  restart re-attaches.

## Acceptance

- `PYTHONPATH=src ve3/bin/python -m pytest tests/unittests/ -q` green
  (existing + new); `ve3/bin/flake8 src/ bin/` clean.
- Docs complete (incl. "Known limitations": executor detected on the
  broker host; pool staging assumes shared FS; `recent_tasks` cap).
- Manual: broker `--host 127.0.0.1 --port 8010 --no-auth --plugins
  task_dispatcher,federation`, one local endpoint, `curl` join in
  allocation mode → a pilot becomes ACTIVE without any task; resource
  visible in the Explorer federation page with node-hours ticking.

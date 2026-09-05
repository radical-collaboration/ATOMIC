# P1 — `federation` plugin (radical.orbit, broker-hosted)

Repo/branch: `/home/merzky/radical/radical.orbit` @ `feature/atomic-federation`.
Contract: `00-overview.md` §"federation". This plugin is **generic Orbit**
— nothing ATOMIC-specific in names, docs or defaults.

## Files

- `src/radical/orbit/plugin_federation.py` — `PluginFederation(Plugin)`,
  `plugin_name = 'federation'`, broker-only (`is_enabled(app)` true only
  for role `broker`, like `iri_connect`). Add to `__init__.py` imports and
  to `DEFAULT_PLUGINS_BY_ROLE['broker']`.
- `src/radical/orbit/federation_state.py` — dataclasses `ResourceRecord`,
  `ResourceUsage`, `FederationState` (persist/load `state.json`, atomic
  rewrite as in `task_dispatcher_state.py`).
- `src/radical/orbit/federation_policy.py` — `FederationPolicy` base
  (`pick(requirements, resources) -> (record, score) | None` plus
  `explain()` returning per-resource rejection reasons), `BudgetLoadPolicy`
  default, `make_policy(config)` accepting `module:Class` like
  `task_dispatcher_policy._import`.
- `src/radical/orbit/data/plugins/federation.js` — minimal Explorer page:
  resources table (name, endpoint, mode, site, cores/gpus/mem, software,
  node-hours used/remaining, pilots, tasks, liveness). Follow
  `task_dispatcher.js` structure; polling refresh; no external libs.
- `docs/plugin_federation.md` — what it is, join modes, routes, policy
  extension, accounting semantics, limitations (shared-FS staging note).
- `tests/unittests/test_federation_policy.py`, `test_federation_state.py`,
  `test_plugin_federation.py`.

## Behaviour

- **join**: validate record; reject duplicate name (409) and unknown /
  disconnected endpoint (404, use the broker topology the plugin already
  receives via `on_topology_change`). Build one `PoolConfig` (`fed-<name>`,
  `endpoint_name=endpoint`, `scratch_base` default
  `~/.radical/orbit/federation/scratch/<name>`), then create a dedicated
  dispatcher session: call the co-hosted `task_dispatcher` plugin's
  `register_session` with `{"pools": [pool]}`. Calling convention: reuse
  the mechanism the dispatcher uses for other plugins
  (`_make_child_client` / broker caller with `dst` = broker participant
  name); if broker→broker calls are not supported, use loopback HTTP to the
  gateway (`http://127.0.0.1:<port>`) with the broker token from the app
  state — pick whichever works, document the choice in the module
  docstring. Store `dispatcher_sid` + `pool_name` in the record.
- **allocation mode sizing**: if the endpoint's `queue_info` reports
  `job_allocation`, use `{nodes: n_nodes, walltime_sec: remaining runtime}`
  and `cpus/gpus` from `sysinfo`; otherwise from declared `capabilities`
  (cores → cpus_per_node, gpus) with `nodes=1, walltime 3600`.
  `max_pilots=1`, `min_pilots=1` so the allocation's pilot starts at join
  (that is what "the allocation joins" means). Budget default
  `nodes × walltime_sec / 3600`.
- **login mode**: pool from `record.pool` (required fields validated;
  sensible defaults: `max_pilots 1`, `rhapsody_backend 'concurrent'`,
  `min_pilots 0`). Budget must be declared.
- **usage**: on `resources`/`pick`, refresh from the dispatcher
  (`pool/{sid}/{name}` or `fleet/{sid}`): `node_hours_used = Σ pilots
  nodes × (ended_at or now − started_at)/3600` over ACTIVE/DONE/FAILED
  pilots that reached ACTIVE; `tasks_running/done` from task records;
  `pilots_active`. Cache 2 s. Never block the event loop on these calls
  (async, with timeouts; on failure keep last usage and flag `stale`).
- **pick/submit**: as per contract. `submit` composes the dispatcher
  submit body (`pool`, `task_id`, `cmd`, `cwd` default `<scratch>/<task_id>`
  created up front, `inputs`, `outputs`, `priority`) and forwards on the
  chosen `dispatcher_sid`. Response includes the resource so callers can
  show placement.
- **leave**: `cancel_all` on the dispatcher session, `unregister_session`,
  remove record, persist.
- **liveness**: from topology (`on_topology_change`) — resource inherits
  its endpoint's liveness; `lost` resources are excluded by the policy.
- **restart**: load state; for each record whose endpoint is connected,
  re-register the dispatcher session (dispatcher persists pools per sid —
  check whether re-registering the same sid reattaches or whether a fresh
  sid is needed; implement whichever the dispatcher supports and test it).

## Tests (pytest, no network beyond the in-process harness)

- policy: capability filtering (cores/gpus/software subset), budget
  exhaustion excludes, liveness excludes, deterministic tie-break, `explain`
  lists reasons per resource.
- state: round-trip persistence; usage arithmetic on synthetic pilot
  records (ACTIVE without end uses now; never negative).
- plugin: using the `test_task_dispatcher_broker.py` harness (fake pilot
  plugin, `make_runtime`): join allocation + login mode → dispatcher
  session exists with pool bound to the endpoint; duplicate join 409;
  join on unknown endpoint 404; `resources` lists both with usage zeros;
  `pick` chooses by capability; `submit` lands a task in the right pool
  (assert via dispatcher `task/{sid}/{id}`); `leave` removes and cancels.

## Acceptance

- `PYTHONPATH=src ve3/bin/python -m pytest tests/unittests/ -q` all green
  (existing + new); `ve3/bin/flake8 src/ bin/` clean.
- `docs/plugin_federation.md` complete; `docs/rest_api.md` gains the
  federation routes; `mkdocs.yml` nav updated if it lists plugin docs.
- Manual: broker `--no-auth --plugins task_dispatcher,federation`, one
  local endpoint, `curl` join → resource visible in Explorer's federation
  page.

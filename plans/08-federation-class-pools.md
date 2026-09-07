# P8 — Federation onto capability-class pools

Repo/branch for the code: `/home/merzky/radical/radical.orbit` @
`feature/atomic-federation` (federation plugin, its JS, its docs) **plus**
`/home/merzky/projects/atomic` @ `feature/demo-wm` (CLI, campaign runner,
demo UI, `demo/local`). Planned here because it consumes
`radical.orbit/plans/121-multi-member-pools.md`.

Read first: `00-overview.md` (contract + verified facts),
`01-orbit-federation.md` (what was built), `STATUS.md` (2026-09-07 line),
and **121**. This plan does not restate 121's schema — it states what the
federation *sends* and *reads*.

**The change in one paragraph.** A dispatcher pool stops being "one joined
resource" and becomes a **capability class** (`fed-cpu`, `fed-gpu`). Joining
declares one or more **members** — one per resource shape the operator is
willing to run — and each member is added to its class pool. The federation
keeps exactly **one** persistent dispatcher session, `fed`, holding every
class pool. `pick` chooses a *class*, not a site; the dispatcher chooses the
member at dispatch time. Budget and node-hours move from the resource to the
member. The REST route names do not change.

> **Scope (Andre, 2026-09-07).** Per-task requirements are *wired, persisted,
> validated and forwarded* only — no dispatcher-side core/GPU reservation and
> no in-pilot pinning this round (see the header of
> `radical.orbit/plans/120-task-requirements-passthrough.md` and 121 §1.1).
> Pilot capacity stays task-count based. Routing is therefore by **declared**
> member attributes; nothing in this plan may claim a GPU is reserved.

---

## 1. Status check — the federation as built (file:line)

All paths under `/home/merzky/radical/radical.orbit/src/radical/orbit/`
unless stated.

| Claim | Evidence |
|---|---|
| One dispatcher session **and** one pool per joined resource, both named `fed-<name>` | `plugin_federation.py:428-442` (`pool_name_for`, `dispatcher_sid_for`), join `:841-842` |
| The pool declaration is built per mode and stored verbatim for replay | `:598-621` (`_build_pool`), `:651-690` (`_allocation_pool`), `:692-754` (`_login_pool`), stored `:828` |
| Login-mode `pool` block is strictly validated, one size key `'default'` | `:693-754`, `_LOGIN_POOL_KEYS :143-146`, `_SIZE_KEY :135` |
| The record's capabilities are flat and resource-wide | `federation_state.py:117-146` (`ResourceRecord`), `validate_capabilities :391-418` |
| Budget is per resource | `federation_state.py:131`, `budget_node_hours :150-155`, default derived at join `plugin_federation.py:830-837` |
| Usage is derived per pool from `pilot_history` + `pilot_sizes` | `plugin_federation.py:1109-1136`, `federation_state.py:205-244` |
| Task counts come from the plugin's own ledger | `federation_state.py:296-313`, ledger written at submit `plugin_federation.py:995-1002` |
| `pick` filters capability/budget/liveness then scores budget − load | `federation_policy.py:79-175` |
| `submit` computes `cwd` broker-side and forwards to the resource's pool | `plugin_federation.py:965-993` |
| `submit` returns `{task, resource, pool, dispatcher_sid}` | `:1004-1007` |
| `task` proxies the dispatcher record and adds `resource` + `child_endpoint` | `:1009-1043` |
| `leave` cancels every non-terminal ledger task, then `cancel_all` + `unregister_session` | `:856-895` |
| Restart re-attach re-registers every stored resource's session, once | `:1175-1208`; liveness sync `:1210-1247` |
| The dispatcher seam is six verbs on `_DispatcherAPI`, method-generic | `:162-259` (`_call :220-228` takes any HTTP method) |
| The Explorer page is one flat resource table | `data/plugins/federation.js:145-194` |

ATOMIC side (`/home/merzky/projects/atomic/`):

| Claim | Evidence |
|---|---|
| Every federation/campaign call is hardwired to sid `default` | `atomic_wm/client.py:263-300`, `_fed :336-337` |
| `atomic-join` has no member concept; login pool comes from 7 flat flags | `atomic_wm/cli/join.py:143-160`, record assembly `:388-441`, login pool block `:429-439` |
| `atomic-resources` columns | `atomic_wm/cli/resources.py:25-35`, rows `:105-125`, legend `:146-149` |
| The campaign plugin talks to the federation in-process on three routes | `atomic_wm/plugins/campaign.py:161-195` (`submit`, `task`, `resources`) and to the dispatcher for staging `:197-228` |
| The runner takes placement from the **submit** response | `atomic_wm/campaign/runner.py:372-384` (`_record_submit`: `resource`, `pool`, `dispatcher_sid`, `task.cwd`) |
| …and refreshes it from every poll | `:540-557` (`_observe`: `state`, `child_endpoint`, fallback `'%s_%s' % (pool, pilot_id)` `:545-547`, `cwd` `:551-552`) |
| Output collection already prefers the pilot's own staging plugin | `:611-635` (`pilot_staging` → `dispatcher_stage_out` → `broker_local`) |
| The demo UI chips show `stage.resource` only | `atomic_wm/ui/atomic_campaign.js:1123-1166`, resource table `:840-902` |
| `demo/local/env.sh` joins three resources, `local_b` advertising both software tags | `demo/local/env.sh:207-267` |
| Smoke asserts software-compatible placement and ≥2 distinct resources | `demo/local/smoke.py:448-490`, `--min-resources` default 2 `:780-782` |

---

## 2. Model and naming

```
resource  local_b  ── members ──┬── local_b/cpu   class cpu  → pool fed-cpu
                                └── local_b/gpu   class gpu  → pool fed-gpu
resource  local_a  ── members ──── local_a/_      class cpu  → pool fed-cpu   (allocation: exactly one)
```

- **member short name**: `MEMBER_NAME_RE = ^[a-z0-9][a-z0-9_-]*$` — **no
  dot**, so the dot in the member id is unambiguously the separator. A
  resource name may contain dots (`NAME_RE`, `federation_state.py:65`), so
  the split is `resource, _, member = mid.rpartition('.')`, never
  `partition`.
- **member id** (the id the dispatcher sees) = `f'{resource}.{member}'`,
  e.g. `local_b.gpu`; matches 121's `MEMBER_RE` and is unique across the
  federation. An allocation-mode / single-member join uses member name
  `default` → `local_a.default`.
- **class** = `member.class` if declared, else `gpu` when the member's
  `gpus_per_node > 0`, else `cpu`. Class name is an explicit field on the
  member record; the derivation is a *default*, so new classes need no code
  change. A declared class must match `^[a-z0-9][a-z0-9_-]*$` — a name that
  does not is a `400`, **not** lower-cased or otherwise coerced (121 §3.2
  applies the same rule to `pool_class`). Silently accepting `GPU` would
  create a second, invisible `fed-GPU` pool.
- **pool name** = `f'fed-{class}'`; created on first use.
- **dispatcher session** = the single constant `fed`
  (`FED_SESSION_SID = 'fed'`). `dispatcher_sid_for()`
  (`plugin_federation.py:433-442`) becomes a constant and `pool_name_for()`
  becomes `pool_name_for_class(cls)`.

---

## 3. Record schema (`federation_state.py`)

```python
@dataclass
class MemberRecord:
    member         : str                  # short name, unique within the resource
    member_id      : str                  # '<resource>.<member>', server-filled
    cls            : str                  # 'cpu' | 'gpu' | … ; wire key "class"
    pool_name      : str                  # 'fed-<class>', server-filled
    queue          : str
    account        : str | None = None
    nodes          : int = 1
    cpus_per_node  : int = 1
    gpus_per_node  : int = 0
    walltime_sec   : int = 3600
    min_pilots     : int = 0
    max_pilots     : int = 1
    rhapsody_backend: str = 'concurrent'
    scratch_base   : str | None = None    # on the MEMBER's host
    shared_fs      : bool = True
    software       : list[str] = field(default_factory=list)
    attributes     : dict = field(default_factory=dict)   # site, mem_gb_per_node, free labels
    budget         : dict = field(default_factory=dict)   # {'node_hours': x}
    usage          : ResourceUsage = field(default_factory=ResourceUsage)
    liveness       : str = LIVENESS_OK    # inherited from the resource's endpoint

@dataclass
class ResourceRecord:
    ...                                   # existing fields :124-146 unchanged
    members : dict[str, MemberRecord] = field(default_factory=dict)  # keyed by short name
```

`class` is a Python keyword, so the dataclass field is `cls` and
`to_wire()` renames it to `"class"` (and `record_from_dict` accepts both).

**Single-member derivation (backward compatibility, required).**
`record_from_dict` (`federation_state.py:177-191`) — a persisted record
without `members` synthesises exactly one:

```
member        = 'default'
queue/account/nodes/cpus_per_node/gpus_per_node/walltime_sec/
  min_pilots/max_pilots/rhapsody_backend   ← rec.pool_config['pilot_sizes']['default']
                                             and rec.pool_config's scalars (:828)
software      ← rec.capabilities.get('software', [])
attributes    ← {'site': rec.site, 'kind': rec.kind,
                 'mem_gb_per_node': rec.capabilities.get('mem_gb')}
budget        ← rec.budget
scratch_base  ← rec.scratch_base;  shared_fs = True
cls           ← 'gpu' if gpus_per_node else 'cpu'
```

The resource-level `capabilities` / `budget` / `pool` fields stay on the
record (never removed) and become the **aggregate view**: `cores` = Σ
`nodes × cpus_per_node`, `gpus` = Σ `nodes × gpus_per_node`, `software` =
union, `budget.node_hours` = Σ member budgets. That keeps
`atomic-resources --json` consumers, `smoke.py:160` (`capabilities.software`)
and `atomic_campaign.js:851-902` working unchanged while the member view is
added alongside.

---

## 4. Routes — behaviour against the 121 dispatcher

Route names, sids (`default`) and the client (`FederationClient`,
`plugin_federation.py:277-333`) are unchanged. `_DispatcherAPI`
(`:162-259`) gains three verbs on the existing `_call` (`:220-228`):

```python
async def add_member(self, sid, pool, member: dict) -> dict      # POST  pool/{sid}/{pool}/members
async def del_member(self, sid, pool, mid, *, cancel_tasks=False,
                     force=False) -> dict                        # DELETE pool/{sid}/{pool}/members/{mid}
async def pool_detail(self, sid, pool) -> dict                   # unchanged (:241-243)
```

### `POST join/{sid}`

Body = today's record plus an optional `members` list; everything already
accepted stays accepted.

```jsonc
{"name": "bridges", "endpoint": "ep_br", "mode": "login",
 "site": "PSC", "kind": "hpc",
 "members": [
   {"member": "cpu", "queue": "RM",  "account": "abc123",
    "nodes": 1, "cpus_per_node": 128, "walltime_sec": 3600,
    "max_pilots": 2, "software": ["lammps"],
    "budget": {"node_hours": 20}},
   {"member": "gpu", "queue": "GPU", "account": "abc123",
    "nodes": 1, "cpus_per_node": 64, "gpus_per_node": 8,
    "walltime_sec": 3600, "max_pilots": 1, "software": ["pytorch"],
    "budget": {"node_hours": 8}, "class": "gpu"}]}
```

Steps (order matters — nothing that can be rejected touches the dispatcher):

1. Existing validation (`:773-806`): name, mode, endpoint connected, not the
   broker, capabilities, scratch.
2. Build the member list:
   - `mode == 'login'` **and** `members` present → validate each with the
     existing integer/queue validators (`validate_pool_int
     :363-388`, sentinel check `:717-719`), reusing `_login_pool`'s rules
     per member; a member's `min_pilots` defaults 0, `max_pilots` defaults 1.
   - `mode == 'login'` **without** `members` → one member `default` from the
     flat `pool` block (today's `_login_pool`, `:692-754`) — the join body
     that works today keeps working.
   - `mode == 'allocation'` → **exactly one** member `default`, from
     `_allocation_pool` (`:651-690`), with `min_pilots = max_pilots = 1`
     and `shared_fs = True`. Declaring `members` in allocation mode is a
     `400` (the allocation *is* the resource).
   - Duplicate member names, or an empty `members` list → `400`.
3. Assign `member_id` and `cls`/`pool_name` per §2.
4. `register_session('fed', pools=_class_pool_decls())` — **always the FULL
   list**, never a delta: `parse_pools` rejects an empty `pools` list
   (`task_dispatcher_config.py:113-114`) and `_materialise_pool` is
   idempotent by name (`plugin_task_dispatcher.py:584-587`), so re-sending
   every pool is both required and free.
   `_class_pool_decls()` is **one helper**, used by `join`,
   `_replay_attachments` and `_sync_attachments` (§6) — three callers, one
   declaration shape, no drift. It emits, per distinct class across all
   stored resources plus the one being joined:

   ```python
   {'name'           : f'fed-{cls}',
    'pool_class'     : cls,
    'multi_member'   : True,
    'members'        : [<121 member decl>, ...],   # every member of that class
    'strategy'       : 'conservative',
    'strategy_config': dict(_STRATEGY_CONFIG)}     # :132
   ```

   `members` is carried in the declaration for the fresh-dispatcher case;
   step 5 is what actually reconciles membership against a dispatcher that
   already has the pool (a re-declaration of an existing pool is ignored,
   `:584-587` — that is the whole reason the member routes exist).
5. For **every** member: `add_member('fed', pool_name, <121 member decl>)`.
   Idempotent by 121 §4.1; this is the single code path used by join *and*
   by restart replay, so there is one thing to get right.
6. Persist, return `rec.to_wire()` — now with `members` and, per member,
   `pool_name`, `class`, `member_id`.

Failure handling: if step 5 fails for member *k*, `del_member(...,
cancel_tasks=False, force=True)` members 0..k-1 for this resource —
**`force=True` matters**: the first member of a brand-new class pool is also
its last, and 121 §4.2 refuses to remove a last member without it — then
return the dispatcher's status/detail. A join is all-or-nothing.

Errors: `409` duplicate resource name (`:793-795`); `404` endpoint not
connected (`:796-799`); `503` no dispatcher (`_DispatcherAPI._host
:188-201`); `400` everything else.

### `POST leave/{sid}/{name}`

Body (optional) `{"cancel_tasks": false}`.

1. For each member of the resource: `del_member('fed', m.pool_name,
   m.member_id, cancel_tasks=<flag>, force=True,
   fail_unsatisfiable=True)`. 121 cancels that member's pilots and re-queues
   their RUNNING tasks **once**; a re-queued task that no remaining member
   can satisfy is failed by the dispatcher with
   `no member satisfies task requirements` (121 §7). `fail_unsatisfiable`
   is left at its default here — an explicit `leave` means the resource is
   gone, so a task only it could run should fail now rather than wait
   forever. (The liveness path in §6 passes `false`; that is the difference
   between "gone" and "blinked".)
2. **Do not** blanket-cancel the ledger any more (`:869-880`). That was
   correct when a pool served exactly one resource; with class pools a queued
   task can legitimately run elsewhere. `cancel_tasks: true` restores the old
   behaviour for a full teardown (`demo/local/down.sh`).
3. **Do not** `unregister_session` (`:881-887`) — the `fed` session holds
   every other resource's pools. `cancel_all` is likewise gone from this path.
4. **Keep the non-terminal ledger entries** (BLOCKING fix). `drop_resource`
   (`federation_state.py:315-319`) deletes every ledger entry naming the
   resource — but with class pools a re-queued task *keeps running*, on
   another member, and `GET task/{sid}/{task_id}` looks the task up in the
   ledger and 404s without an entry (`plugin_federation.py:1019-1022`),
   which the campaign runner turns into a hard `TaskNotFound` stage failure
   (`atomic_wm/campaign/runner.py:509-537`, `MAX_POLL_FAILURES` bypassed —
   a 404 fails on the first poll). So `leave` drops only the **terminal**
   entries and re-points the rest: `entry.resource = None`,
   `entry.member_id = None`, keeping `pool` and `dispatcher_sid` (both still
   valid — the class pool and the `fed` session outlive the resource). The
   next poll fills the real placement back in from the dispatcher's
   `member_id`. Add `drop_resource(name, keep_active=True)` rather than a
   second code path.

   Precisely, per ledger entry of the leaving resource:
   - already terminal → dropped, as today;
   - `cancel_tasks: true` → 121 cancelled or failed it, so it is terminal by
     the time this step runs → dropped;
   - RUNNING on one of this resource's pilots → 121 re-queued it; the entry
     stays with `resource = None`;
   - QUEUED, merely *attributed* to this resource by the advisory value the
     submit returned → it was never bound to a member at all and may run
     anywhere in the class pool; the entry stays with `resource = None`.

   The response key for tasks the dispatcher failed is 121's own name,
   `tasks_failed` — do not invent a second vocabulary for the same number.
5. Drop the record, drop the detail-cache entries for the affected pools,
   persist.
6. An emptied class pool is left in place (inert: no members ⇒ no scale-up,
   no pilots ⇒ no dispatch) and is reused by the next join of that class.

Return `{"resource": name, "ok": true, "members_removed": n,
"tasks_requeued": n, "tasks_failed": n}` — the last two summed over the
per-member `del_member` responses (121 §4.2).

### `GET resources/{sid}` / `GET resource/{sid}/{name}`

Same shape plus the `members` array, each member carrying its own `usage`.
Refresh (`_refresh_usage :1109-1136`) becomes:

- one `pool_detail('fed', pool_name)` per **distinct class pool** (cached 2 s
  as today, `_detail_cache :413`, `_USAGE_CACHE_SEC :126`) — with N resources
  in 2 classes this is 2 dispatcher round-trips instead of N. The plugin's
  own helper `_pool_detail(rec: ResourceRecord)` (`:1085-1107`) therefore
  becomes **`_pool_detail(pool_name: str)`**: a pool is no longer a property
  of one resource, and every caller (`_refresh_usage`, the `child_endpoint`
  lookup in `_route_task :1038`) already has, or can trivially get, the pool
  name. `_detail_cache` stays keyed by pool name — which it already is
  (`:1094`), it was simply reached through a record;
- per member: read the matching entry from 121's `summary['members']`
  (§9 of 121) → `node_hours_used`, `node_hours_remaining`, `pilots_active`
  straight off the dispatcher, no arithmetic here;
- per member task counts: the ledger gains `member_id` (see `submit`) and
  `task_counts` (`federation_state.py:302-313`) gains a `member_id` variant;
  a task not yet dispatched has no member, so it counts on the **resource**
  only (`tasks_pending`).
- resource-level `usage` = the sum over its members (so today's
  `atomic-resources` and `atomic_campaign.js` keep rendering).
- `stale` semantics unchanged (`:1122-1125`).

### `POST pick/{sid}`

Returns the **class**, plus which members the dispatcher would consider —
for display only:

```json
{"pool": "fed-gpu", "class": "gpu", "dispatcher_sid": "fed",
 "members": [{"member_id": "bridges.gpu", "resource": "bridges",
              "score": 0.83, "reason": null},
             {"member_id": "local_b.gpu", "resource": "local_b",
              "score": 0.4, "reason": null}],
 "resource": "bridges"}
```

`resource` is kept as the highest-scoring member's resource so today's
callers (`atomic_wm/client.py:286-289`, tests) still read something sensible;
it is explicitly documented as **advisory** — the binding placement is made
by the dispatcher at dispatch and reported by `task`.

`409` body keeps its shape (`_no_resource :1057-1081`) but the `reasons` map
is now keyed by `member_id`.

### `POST submit/{sid}`

1. `requirements` validated as today (`:954-957`) and passed **through** to
   the dispatcher — the 120 payload (`cores`, `gpus`, `mem_gb`, `ranks`,
   `mpi`) plus `software` and `labels` (121 §1.1/§3.3). **`node_hours` is
   federation-only and must be stripped before forwarding** — 120's
   validator rejects unknown keys with a `400`. The dispatcher answers
   `400` when no member can satisfy the shape or the software (121 §4.3);
   map that to today's `REASON_NO_RESOURCE` vocabulary in the campaign
   runner (`runner.py:47`, which currently only maps `409`).
   This **supersedes 120 §PR1.6's "software … never forwarded"** — with
   class pools the dispatcher is the component that matches software
   against a pilot's member attributes (121 §1.1, amendment 2).
2. Choose the class (§5). `409 + reasons` when no class has an eligible
   member.
3. `cwd`: **always omitted.** 121 §8 assigns it at dispatch from the actual
   member's `scratch_base`, which is the only correct answer once a class
   pool can mix members. The round-1 "keep the old broker-side cwd when every
   member is shared" branch is dropped: it is a second code path that is
   wrong the moment a non-shared member joins that class, and it buys
   nothing — with inputs riding the submit (§4 step 4) no client needs the
   cwd before dispatch, and `stage_out` still works because it resolves
   `Path(rec.cwd)` from the record the poll reports. The `cwd` validation and
   `mkdir` at `:965-983` go away with it (a client-supplied `task.cwd` is now
   refused with `400`, since the federation cannot honour it across members).
4. **`inputs_b64` rides through.** A submit body may carry
   `{"task": {..., "inputs_b64": {"md.json": "<b64>"}}}`; the federation
   forwards it verbatim into the dispatcher payload (121 §4.3), which
   spools it and places it wherever the task lands. This is what removes
   the client's need to know the placement before the placement exists.
   The federation itself neither decodes nor stores it (it is already
   size-capped dispatcher-side, `413`); it only refuses a non-object with
   `400`.
5. `dispatcher.submit('fed', {'pool': 'fed-<class>', ...})`.
6. Ledger entry gains `pool` = class pool, `member_id = None` (filled on the
   first poll that reports one), `class`.
7. Response — **superset** of today's:

```json
{"task": {...}, "pool": "fed-gpu", "class": "gpu",
 "dispatcher_sid": "fed", "resource": "bridges", "member": null,
 "members_eligible": ["bridges.gpu", "local_b.gpu"]}
```

`resource` = the advisory top-scoring member's resource (so
`runner.py:378` keeps working and the UI chip is populated immediately);
`member` is `null` until dispatch.

### `GET task/{sid}/{task_id}`

Today's dict plus `member_id`, `member`, and a corrected `resource`:

- read `task['member_id']` from the 121 dispatcher record (set in `_claim`);
- map it back to `(resource, member)` with `member_id.rpartition('.')` and
  **update the ledger** — this is the authoritative placement, and it is also
  what re-points an entry whose resource left (`leave` step 4);
- `resource` in the response becomes the real one once dispatched (before
  dispatch it stays the advisory value from submit, and it may be `null` for
  a task whose original resource has left and which has not been re-dispatched
  yet — the runner treats a missing resource as "not placed", it does not
  fail);
- `child_endpoint` lookup (`:1036-1042`) is unchanged — it already reads
  `pilots[].child_endpoint_name` from the verbose summary, and 121 changed
  only how that name is *built*.

---

## 5. `FederationPolicy` v2 (`federation_policy.py`)

The policy shrinks: it no longer picks a site, it picks a **class** and
explains member eligibility.

```python
class FederationPolicy:
    def pick_class(self, requirements: dict,
                   classes: dict[str, list[MemberRecord]]
                   ) -> tuple[str, float] | None: ...
    def eligible(self, requirements: dict,
                 members: Iterable[MemberRecord]
                 ) -> list[tuple[MemberRecord, float]]: ...
    def explain(self, requirements: dict,
                members: Iterable[MemberRecord]) -> dict[str, str]: ...
```

`BudgetLoadPolicy` v2:

- **semantic change, state it in the docs**: today `reject_reason` compares
  `cores` against the resource's *total* declared capability
  (`federation_policy.py:134-155`, and 120 §PR1.6 calls this widening out).
  Against a member it is compared **per node** against the member's pilot
  size, which is what the dispatcher will do at submit (121 §4.3) — so
  federation and dispatcher now agree instead of the federation being
  laxer. `mem_gb` likewise moves to `attributes.mem_gb_per_node`.
- **member filter** — reuse `radical.orbit.task_dispatcher_match.satisfies`
  (121 §3.3) against the member's `attributes` + its default `PilotSize`, then
  the two federation-only checks that remain here: `liveness == ok`
  (`:131-132`) and `node_hours` budget (`:156-163`). One matcher, one
  vocabulary, dispatcher and federation agree by construction.
- **score** — unchanged formula (`:166-175`) evaluated per member:
  `remaining_budget_fraction − load`, `load = tasks_running / (nodes ×
  cpus_per_node)`; ties break on `member_id`.
- **class choice** — the class of the highest-scoring eligible member. When
  a requirement fits two classes (e.g. a CPU task and a GPU member that also
  has the software), prefer the class with the **cheapest** eligible member,
  where cheap = fewest `gpus_per_node`, then fewest `cpus_per_node`, then
  class name — so a CPU task never burns a GPU allocation while a CPU one
  is available. Deterministic, one line, and testable — the test that matters
  is a **CPU task that also fits a GPU member**: it must land in `fed-cpu`.
- The `module:Class` loader (`:185-213`) is unchanged.

**No compatibility wrappers.** The old `pick(requirements, resources)` /
`explain(requirements, resources)` pair is **replaced**, not kept beside the
new API. There is exactly one in-tree policy and no out-of-tree one; two
parallel entry points would guarantee they drift. `plugin_federation._pick`
(`:1047-1055`) calls the new methods, and a custom policy loaded through
`policy=module:Class` that still implements the old pair fails loudly at
plugin construction — which is the right time to find out.

---

## 6. Restart re-attach — the single `fed` session

**One rule governs every call: always register `fed` with the FULL list of
class-pool declarations.** `parse_pools` rejects an empty `pools` list
(`task_dispatcher_config.py:113-114`) and `_materialise_pool` is idempotent
by name (`plugin_task_dispatcher.py:584-587`), so re-sending every pool is
both required and free. A federation with no resources at all simply does
not call `register_session`.

`_replay_attachments` (`:1175-1208`) becomes, once, at first topology or
first route (`_require_session :450-470` — keep that trigger):

1. **Upgrade path from pre-08 state** (do it first, and in this order):
   - every stored resource carries a per-resource `dispatcher_sid`
     (`fed-<name>`). If the broker is being upgraded in place, the dispatcher
     has replayed those old pools off disk — but **owner-less**: a replayed
     pool has no session, and `_housekeeping` skips it
     (`plugin_task_dispatcher.py:707-711`) while `unregister_session` on a
     sid the dispatcher does not know is a `404`, tearing down nothing.
     So for each distinct non-`fed` `dispatcher_sid`, do what
     `_replay_attachments` already does today (`:1200-1203`):
     **`register_session(old_sid, [rec.pool_config])` FIRST, then
     `unregister_session(old_sid)`** — re-own the pool, then release it
     through the ordinary teardown so its pilots are actually cancelled
     (`_teardown_session_pools:2151-2179`) instead of lingering as orphans
     holding a psij job and a child endpoint. This is exactly the
     register-then-release dance the current code uses for a resource whose
     endpoint is gone; the upgrade is the same shape, applied to every
     resource at once.
   - then fail every non-terminal ledger entry whose `dispatcher_sid` is not
     `fed`: its pool has just been torn down, so the task cannot be
     recovered — mark it `FAILED` with
     `detail = 'the federation was upgraded'` and let the campaign layer
     report it through its existing vocabulary. Terminal entries keep their
     history.
   - the member records themselves are derived from the stored record
     (§3), so nothing else is lost.
2. `register_session('fed', pools=_class_pool_decls())` — the same helper as
   `join` step 4, the full list. The dispatcher has already replayed those
   pools with their persisted members (121 §10) and returns them as-is.
3. For **every** member of **every** stored record: `add_member(...)`.
   Idempotent when the dispatcher still has it; it is the recovery path when
   the dispatcher's own state was wiped. This is why 121 §4.1 must be a
   no-op for an identical re-POST.
4. Records stay `lost` until topology promotes them (`:397-398` — keep).

`_attached` (`:404`) becomes a set of **`member_id`s**, not resource names:
attachment is now per member, and a resource whose two members live behind
one endpoint still needs both tracked (and one of them can fail to attach
while the other succeeds).

`_sync_attachments` (`:1210-1247`) moves to member granularity:

| endpoint liveness | action |
|---|---|
| `present`, member not attached | `add_member(...)` (via the same declaration `_class_pool_decls()` builds), mark `ok`. If its class pool no longer exists — every member of that class left while the endpoint was down — re-`register_session('fed', _class_pool_decls())` first |
| `suspect` | mark `suspect`, **do nothing else**. The federation policy already refuses to route to a non-`ok` member (`federation_policy.py:131-132`), which is the whole point of `suspect`; a blip must not touch the dispatcher |
| `lost`, member attached | `del_member(..., cancel_tasks=False, force=True, **fail_unsatisfiable=False**)`; mark `lost`. Its pilots are gone with the endpoint anyway; its RUNNING tasks are re-queued once by the dispatcher and can land on another member — and a task only *this* member could run stays QUEUED instead of being failed, because a lost endpoint is very often back in a minute and `_sync_attachments` will re-`add_member` it (121 §4.2) |

There is **no quiesce mechanism** and no partial member update: 121's
"identical re-POST = no-op, differing = 409" stands, member `max_pilots`
keeps the `>= 1` rule, and changing a member's budget or size means
`del_member` + `add_member` (i.e. re-joining the resource). Budget top-up
is out of scope for this round.

---

## 7. CLI

### `atomic-join` (`atomic_wm/cli/join.py`)

New repeatable flag, added to the "login mode" group (`:143-160`):

```
--member NAME:queue=…,account=…,nodes=…,cpus=…,gpus=…,walltime=…,
              max_pilots=…,min_pilots=…,node_hours=…,software=a,b,
              site=…,class=…,scratch=…,shared_fs=false,backend=…
```

- **Parsing rule** (`parse_member(spec) -> dict`, next to `parse_declare
  :184-210` and `parse_software :213-224`): split once on `:` → `name`,
  `rest`; split `rest` on `,`; a fragment **without** `=` is appended to the
  previous key's value, turning it into a list. So `software=a,b` yields
  `software: ['a','b']` and `site=NERSC` stays scalar. A leading fragment
  with no `=` and no previous key is a `UsageError`
  (`"--member local_b:a,b: 'a' is not key=value"`), never a silently
  dropped token. `software` is **always** normalised to a list, so
  `software=lammps` gives `['lammps']` and the record shape does not depend
  on how many tags were typed. `name` is validated against
  `MEMBER_NAME_RE` (§2). Documented in `--help` and `docs/join.md`, because
  it is the one non-obvious bit.
- **Known keys** → the member's pool fields: `queue`, `account`, `nodes`,
  `cpus` (→ `cpus_per_node`), `gpus` (→ `gpus_per_node`), `walltime`
  (→ `walltime_sec`), `min_pilots`, `max_pilots`, `node_hours`
  (→ `budget.node_hours`), `software`, `class`, `scratch`
  (→ `scratch_base`), `shared_fs`, `backend` (→ `rhapsody_backend`).
  **Every other key becomes an attribute** (`site=NERSC` →
  `attributes.site`), which is how free-form labels reach the dispatcher.
  `atomic-join` echoes the parsed members back on success
  (`_report_joined :774-798`) so a typo is visible rather than silent.
- **Validation** (`_validate_login :283-306`): with `--member` present, the
  seven flat login flags are rejected as mutually exclusive
  (`_login_flags :271-280` already enumerates them); each member requires
  `queue`, `nodes`, `cpus`, `walltime`, `node_hours`; `queue != 'default'`
  (`:304`). Without `--member`, everything behaves exactly as today.
- **Allocation mode** (`_validate_allocation :309-317`): `--member` is an
  error — one implicit member.
- **Record assembly** (`assemble_record :388-441`): add
  `record['members'] = [...]` in login mode when `--member` was given;
  the flat `record['pool']` block is then omitted. `capabilities` keeps being
  filled (aggregate: `cores = Σ nodes×cpus`, `gpus = Σ nodes×gpus`,
  `software` = union) so `connect_and_join`'s `'cores' in capabilities`
  guard (`:657-660`) and every existing consumer still work.

**`atomic-leave --cancel-tasks`** is a three-line chain, listed so nobody
half-wires it: `atomic_wm/cli/leave.py` adds the flag →
`do_leave(client, name, cancel_tasks=False)` (`join.py:548-558`, shared by
`atomic-leave` and `atomic-join`'s own teardown at `:561-577`) →
`Client.fed_leave(name, cancel_tasks=False)` (`client.py:269-272`) posts the
body. `demo/local/down.sh` passes `--cancel-tasks` (a teardown wants the
work stopped, not re-queued onto a resource that is about to leave too);
`atomic-join`'s SIGINT path does **not** (a single resource leaving a live
federation should let its work migrate).

### `atomic-resources` (`atomic_wm/cli/resources.py`)

Two-level table: one row per resource, then indented member rows.

```
RESOURCE   MEMBER  CLASS/POOL   SITE    SIZE          SOFTWARE        NODE-H       PILOTS  TASKS  LIVENESS
local_b                         NERSC   2 members     lammps,pytorch  0.31/3.69    1       2/5    ok
  └ cpu    cpu     cpu/fed-cpu  NERSC   1x2c          lammps,pytorch  0.20/1.80    1       2/3    ok
  └ gpu    gpu     gpu/fed-gpu  NERSC   1x2c+1g       pytorch         0.11/1.89    0       0/2    ok
```

- `COLUMNS` (`:25-35`) becomes `RESOURCE, MEMBER, CLASS/POOL, SITE, SIZE,
  SOFTWARE, NODE-H, PILOTS, TASKS, LIVENESS`. The `LIVENESS` header is
  **kept as-is** — renaming it to `LIVE` is a cosmetic change with its own
  (small) blast radius on docs and the demo README, and it does not belong
  in this plan.
- `SIZE` = `"{nodes}x{cpus}c"` + `"+{gpus}g"` when non-zero; the resource row
  shows `"N members"`.
- `node_hours()` (`:79-102`) is reused per member (member `usage` has the
  same keys); the resource row sums. The `*` stale marker and the legend
  (`:146-149`) stay, with `SIZE: nodes x cpus/node (+gpus/node)` added.
- `--json` (`:176`) still dumps the raw records — which now carry `members`.
- A record without `members` (a broker that has not been upgraded) renders
  exactly as today: derive one member client-side, print only the resource
  row.

### `atomic_wm/client.py`

No signature changes (`:263-300`). `fed_leave` gains an optional
`cancel_tasks: bool = False` body argument. Everything stays on sid
`default`.

---

## 8. UI

### `data/plugins/federation.js` (orbit, the plain Explorer page)

`renderTable` (`:145-163`) keeps its header row and gains, per resource, a
member sub-row block (`<tr class="fed-member">` with a leading `└`), columns:
member, class/pool, endpoint, size, software, node-hours bar, pilots, tasks,
liveness. `renderRow` (`:165-194`) is split into `renderResourceRow` (the
aggregate, unchanged arithmetic) and `renderMemberRow`. Add `.fed-member td
{ padding-left: 18px; color: var(--muted) }`. A record without `members`
renders exactly as today (guard on `(r.members || []).length`).

### `atomic_wm/ui/atomic_campaign.js` (the demo UI)

- Resources panel (`renderResources :803-849`, `renderResourceRow :851-902`):
  keep the current ten columns for the resource row; add member sub-rows
  (member, class, size, software, node-hours, active work). The existing
  `capabilities`/`usage` reads (`:857-899`) keep working off the aggregate.
- Placement chips (`renderWorkflowRow :1123-1166`): the sub-label becomes
  `resource/member` when the stage has a member, else `resource`, else the
  state word — i.e. one extra term in the `bits` array at `:1147-1155`. The
  class goes in the tooltip (`title`), not on the chip: the chip is already
  tight at 720p (P5 review finding).
- Nothing else moves; `s.pool` / `s.class` stay unread by the renderer.

---

## 9. Campaign plugin and runner touch points

Small, and enumerated so nobody re-derives them:

1. `atomic_wm/plugins/campaign.py:161-174` (`submit`) — no change; the
   response is a superset.
2. `atomic_wm/campaign/runner.py:372-384` (`_record_submit`) — also store
   `stage.cls` and treat `stage.resource` as **advisory**. Add
   `member: Optional[str]` and `cls: Optional[str]` to `StageRun`
   (`atomic_wm/campaign/state.py:110-115`) and to `to_dict()`.
3. `runner.py:540-557` (`_observe`) — read `member` / `member_id` /
   `resource` from the poll and overwrite the advisory values. This is the
   one behavioural change: **placement is what the poll says**, not what the
   submit said.
4. `runner.py:545-547` — the `child_endpoint` fallback
   `'%s_%s' % (stage.pool, task['pilot_id'])` must become
   `'%s_%s_%s' % (pool, member_id, pilot_id)` when a `member_id` is present
   (121 §6). The primary path (the `child_endpoint` field reported by
   `task`) is unaffected, so this only matters for a pilot that finished
   between two polls.
5. **Inputs move into the submit** (supervisor decision, review round 1).
   `_run_stage` (`:316-369`) reads each declared input from the store and
   puts it in the task body as `inputs_b64` (121 §4.3); the dispatcher
   spools it and places it wherever the task lands.
   - `_stage_inputs` (`:387-431`, dispatcher `stage_in`) and `_push_inputs`
     (`:434-461`, pilot `staging.put` once `child_endpoint` appears) are
     **both dropped** for class pools, together with the lazy push in
     `_poll_task` (`:492`) and the `push_inputs` constructor flag (added in
     the P4 review round). One path, executed before the task exists,
     instead of two racing ones executed after it does — this also removes
     the shared-FS `overwrite=True` race that `push_inputs` was gated on.
   - `stage.outputs[].via` keeps its vocabulary; the input side loses
     `dispatcher_stage_in` and gains `submit` in the manifest.
   - Failure to *read* an input from the store fails the stage with the
     existing `REASON_STAGE_IN` phrase (`runner.py:52`) — unchanged
     user-visible vocabulary. Failure to *place* it is now the dispatcher's
     and surfaces as a task failure with its own reason.
   - Keep the old two-path code only if a fallback for a **non-class** pool
     is still needed; since the federation only ever submits to class pools
     after this plan, delete it.
6. `runner.py:611-635` (`_sources`) — unchanged; `pilot_staging` is already
   first and is now the only path that works for non-shared members
   (outputs, unlike inputs, still have no dispatcher-side answer).
7. `smoke.py:148-151` (`resource_of`) — extend to read `member` too.

---

## 10. `demo/local`

- `env.sh:226-267` (`demo_join_args`):
  - `local_a` — unchanged (allocation, `software lammps`) → one implicit
    member, class `cpu`.
  - `local_b` — **switches to `--mode login` with two members**:
    ```
    --member cpu:queue=local,nodes=1,cpus=2,walltime=1800,node_hours=2,
                 software=lammps,pytorch,site=NERSC
    --member gpu:queue=local,nodes=1,cpus=1,gpus=1,walltime=1800,node_hours=1,
                 max_pilots=1,software=pytorch,site=NERSC
    ```
    The "GPU" is fake (psij `local`, `rhapsody_backend: concurrent`); only
    the *declaration* matters for routing. Note `cpus=1` and
    `max_pilots=1` — see the determinism note below.
  - `local_c` — login, now **two** members as well: its existing CPU one
    (`software pytorch`) plus
    ```
    --member gpu:queue=local,nodes=1,cpus=1,gpus=1,walltime=1800,node_hours=1,
                 max_pilots=1,software=pytorch,site=PSC
    ```
    so `fed-gpu` has **two members from two "sites"** and the demo actually
    shows a class pool spreading work across sites — the point of the whole
    re-architecture. With one GPU member the routing story is
    indistinguishable from today's one-pool-per-resource.
  - Result: `fed-cpu` has 3 members (`local_a.default`, `local_b.cpu`,
    `local_c.cpu`), `fed-gpu` has 2 (`local_b.gpu`, `local_c.gpu`).
- `examples/workflow_vacancy.json` — the `train` stage's requirements become
  `{"cores": 1, "gpus": 1, "software": ["pytorch"]}` so the campaign actually
  exercises class routing (`cores: 1` because a GPU member declares
  `cpus_per_node: 1`, see below); `md` stays `{"cores": 2, "software":
  ["lammps"]}`. `train`'s cmd gains `--duration-sec 10`.

**Making the two-GPU-member spread deterministic** (supervisor decision,
review round 2). "Three tasks, two members, expect ≥2 used" is a race unless
the pool is *forced* to use both: with a fat GPU member all three `train`
tasks fit one pilot and a correct scheduler would put them there. So the
demo declares each GPU member with `cpus=1` → pilot `capacity = nodes ×
cpus_per_node = 1` (`plugin_task_dispatcher.py:1424`) → **one concurrent
task per GPU pilot** — and `max_pilots=1`, so a member cannot answer the
backlog by growing instead of sharing. Three concurrent `train` tasks then
*need* both members: one runs on `local_b.gpu`, one on `local_c.gpu`, the
third waits. `--duration-sec 10` on the train stage keeps each task alive
well past the ~5 s pilot spin-up measured in the spike
(`00-overview.md:337-344`), so the second member's pilot is genuinely
started rather than the queue draining serially onto the first. The
`--min-gpu-members` default stays 2. Write this reasoning into `env.sh`
next to the member lines and into the README — it is the kind of parameter
choice that looks arbitrary and gets "tidied" away six months later.
- `env.sh:272` `ATOMIC_DEMO_PILOT_RESOURCES` — `local_a` only (every other
  member is login-mode with `min_pilots=0`, so no pilot exists before the
  first task).
- `smoke.py` new/changed assertions (next to `check_placement :448-490`):
  - every stage's `pool` equals `fed-<expected class>` for its requirements
    (`gpus>0 → fed-gpu`), message
    `'stage %s of workflow %s ran in pool %r, expected %r'`;
  - every stage requiring a GPU ran on a member with `gpus_per_node > 0`,
    message `'stage %s ran on member %r, which declares no GPU'`;
  - the existing software check (`:478-482`) is re-pointed at the **member's**
    software, not the resource's;
  - `--min-resources` (default 2, `:780-782`) is joined by
    `--min-members` (default 2) and the ≥2-distinct check (`:485-488`) runs
    over members;
  - **the `train` stages spread over ≥2 GPU members** — with a 3-point sweep,
    two GPU members of capacity 1 and `max_pilots=1`, three concurrent
    `train` tasks cannot all run on one member, so this is a deterministic
    assertion rather than a timing hope. Message: `'the %d train stage(s)
    used %d GPU member(s) (%s), expected at least 2 — the class pool did not
    spread across sites'`. Guard it with `--min-gpu-members` (default 2) so a
    single-GPU-member federation can still run the smoke test;
  - the placement table (`render_placements :323`) gains a `member` column.
- **State the expected placement explicitly** in `smoke.py`'s docstring
  (`:9-12`) and in the README, so a failure is read against an intent rather
  than a shrug: `md` (lammps, no GPU) → `fed-cpu`, on `local_a.default` or
  `local_b.cpu` (not `local_c.cpu`, which has no lammps); `train`
  (pytorch, 1 GPU) → `fed-gpu`, spread over `local_b.gpu` and
  `local_c.gpu`.
- `README.md` — the routing story: two classes, five members, two sites in
  `fed-gpu`; `train` needs a GPU so it can only land on a GPU member, and
  the dispatcher — not the federation — decides which one.

---

## 11. Docs

| File | Change |
|---|---|
| `radical.orbit/docs/plugin_federation.md` | §Resource (`:35-68`) → resource + members; §Join modes (`:69-116`) → the `members` list and the allocation-mode single-member rule; §Usage (`:126-149`) → per member, read off the dispatcher; §Policy (`:160-194`) → class choice + member eligibility; §Routes (`:195-250`) → the new response fields; §"How the pieces are wired" (`:251-312`) → one `fed` session, class pools, member routes; §Known limitations (`:354-390`) → `shared_fs`, advisory `resource` at submit |
| `radical.orbit/docs/rest_api.md` | federation response fields |
| `atomic/docs/join.md` | `--member` syntax incl. the list-append rule, worked examples for both modes |
| `atomic/docs/campaign.md`, `campaign_seam.md` | placement is `resource/member` and comes from the poll |
| `atomic/demo/local/README.md` | as above |
| `plans/00-overview.md` | a "superseded by 08/121" note on the federation contract block (`:176-251`) — do not delete it, it documents what shipped |

---

## 12. Tests

**orbit** (`tests/unittests/`):

- `test_federation_state.py`: `MemberRecord` round-trip; **a pre-08
  `state.json` loads and derives exactly one member** with the right class,
  budget and software; aggregate capabilities/budget arithmetic;
  `to_wire()` renames `cls` → `class`; ledger `member_id`.
- `test_federation_policy.py`: member-level filter reuses
  `task_dispatcher_match` (software/gpus/labels); budget exhaustion excludes
  a member but not its sibling; **a CPU-only task that also fits a GPU
  member lands in `fed-cpu`** (the cheapest-class rule); deterministic
  tie-break on `member_id`; `explain` keyed by `member_id`. The old
  `pick()/explain()` pair is gone — assert the new API only.
- `test_plugin_federation.py` (fake `_DispatcherAPI` as today): join with
  two members → one `register_session('fed', …)` + two `add_member` calls
  with the right pool names; join without `members` → one member, byte-same
  pool declaration as today; allocation + `members` → 400; join failure
  mid-way rolls back added members **with `force=True`**; `leave` calls
  `del_member` per member, **does not** unregister `fed`, and **keeps
  non-terminal ledger entries with `resource=None`** (then a poll re-points
  them); `resources` reports per-member usage from a canned 121 summary;
  `pick` returns class + eligible members; `submit` targets `fed-<class>`,
  **never** sends `cwd`, rejects a client-supplied `task.cwd` with `400`,
  strips `node_hours` and forwards `software`/`labels`/`inputs_b64`; a
  dispatcher `400` maps to `REASON_NO_RESOURCE`;
  `task` rewrites `resource` from the dispatcher's `member_id` via
  `rpartition`; restart replay re-registers `fed` once with the **full**
  pool list and re-POSTs every member; a pre-08 state file makes replay
  unregister the legacy `fed-<name>` sessions and FAIL their live ledger
  entries — **asserting the order: `register_session(old_sid, …)` then
  `unregister_session(old_sid)`**, since the reverse is a 404 that tears
  down nothing; a `suspect` endpoint changes liveness only — **no**
  dispatcher call; a `lost` endpoint calls `del_member` with
  `fail_unsatisfiable=False`.
- **Existing tests in `test_plugin_federation.py` that must change** (they
  assert the per-resource sid or the old cancel order): `:297-298`, `:302`,
  `:335`, `:589-590`, `:651-657`, `:690-691` (all `dispatcher_sid ==
  'fed-<name>'` → `'fed'`); `:815-823` (leave's cancel-then-teardown order
  → member removal, no `cancel_all`, no `unregister_session`); `:875`;
  `:1144`. Budget an hour for exactly this.
- One co-hosted integration test (real 121 dispatcher + the fake pilot and
  the **fake rhapsody plugin 121 §13 adds** to
  `test_task_dispatcher_broker.py`): two members with different `software`
  in one class pool; a task with `software=[x]` lands on member X's pilot;
  `leave` of X's resource drains it and the task re-queues.

**atomic** (`tests/`):

- `tests/test_cli_join.py` (exists): `parse_member` (list-append rule,
  `software` always a list, a leading fragment without `=` → `UsageError`,
  unknown key → attribute, bad member name, bad `NAME:`), `--member` + flat
  login flags → error, `--member` in allocation mode → error, record
  assembly with two members, aggregate capabilities, `--cancel-tasks` on
  `atomic-leave`.
- `tests/test_cli_resources.py` (**new file** — the resources CLI has no
  test module today): two-level rendering, a record without `members`
  renders one row, `--json` passthrough.
- `tests/test_runner.py` (exists — this is the runner's module, there is no
  `test_campaign_runner.py`): `_observe` overwrites the advisory resource
  with the polled member; a poll with `resource: null` does not fail the
  stage; the `child_endpoint` 3-part fallback; inputs ride in the submit
  body as `inputs_b64` and neither `stage_in` nor `staging_put` is called;
  an unreadable input still yields `REASON_STAGE_IN`.
- `tests/test_client.py` (exists): `fed_leave(name, cancel_tasks=True)`
  sends the body.
- `demo/local/test_smoke_helpers.py`: the new class/GPU assertions over
  canned campaign + resource payloads.

Green bar: orbit `PYTHONPATH=src ve3/bin/python -m pytest tests/unittests/ -q`
and `ve3/bin/flake8 src/ bin/`; atomic `pytest tests/ -q` (284 today) and
`flake8 atomic_wm`.

---

## 13. Effort and work packages

| WP | Content | Depends on | Effort |
|---|---|---|---|
| P1 | `MemberRecord`, aggregate views, single-member derivation, ledger `member_id`, state tests | — | 3 h |
| P2 | `FederationPolicy` v2 + tests | P1, 121-B (`task_dispatcher_match`) | 3 h |
| P3 | `_DispatcherAPI` member verbs + join/leave against them, incl. the ledger-keeping leave | P1, **121 §4 contract** (fake API is enough) | 5 h |
| P4 | resources/pick/submit/task + per-member usage + `inputs_b64` passthrough + 120's federation projection (§14 rule 6) | P1, P3 | 5 h |
| P5 | restart re-attach, pre-08 upgrade path, per-member `_attached` | P3 | 3 h |
| P6 | `atomic-join --member`, `atomic-leave --cancel-tasks`, `atomic-resources`, client | P1 (wire shape only) | 4 h |
| P7 | `federation.js` + `atomic_campaign.js` member rows and chips | P4 | 3 h |
| P8 | campaign runner touch points (§9), incl. deleting the two staging paths | P4, 121-G | 3 h |
| P9 | `demo/local` (two GPU members) + smoke assertions | P6, P8, 121 all | 4 h |
| P10 | docs (§11) | all | 3 h |
| P11 | Rewriting the existing federation tests listed in §12 | P3, P4 | 1 h |

≈ **37 h**.

## 14. Sequencing across 120 / 121 / 08

```
120 (requirements passthrough)  ──┐
                                  ├─▶ 121-A/B/C (schema, matcher, records)
                                  │        │            │
                                  │        │            └─────────────┐
                                  │        ├─▶ 121-D/E (routes, pilots)  ─┐
                                  │        ├─▶ 121-F   (policy v2)        ├─▶ 121-J (harness)
                                  │        └─▶ 121-G/H (cwd, inputs, summary) ┘      │
                                  │                                                  │
  08-P1 (state) + 08-P6 (CLI) ── start immediately, no dispatcher, no 121 needed      │
  08-P2 (policy) ── needs 121-B (`task_dispatcher_match`) only ◀──────────────┘       │
  08-P3/P4/P5 ── build against 121 §4's route contract with a FAKE _DispatcherAPI ────┤
                                                                                      ▼
                                                             08-P7/P8/P9 (UI, runner, demo e2e)
```

Rules that make the parallelism real:

1. **Freeze 121 §3 (schema) and §4 (routes) first — one hour of review, then
   no changes.** Everything else keys off them.
2. 120 owns `TaskRecord.requirements`, its key whitelist and the rhapsody
   mapping; 121 only *reads* requirements through
   `task_dispatcher_match.satisfies`; 08 only *forwards* them (minus
   `node_hours`). If 120 slips, 121 adds the field itself under the same
   name and 120 rebases (agreed in 121 §1.1/§1).
   **No reservation, no pinning this round** (Andre, 2026-09-07 — see the
   header of `plans/120-*.md` and 121 §1.1): pilot capacity stays task-count
   based, so the federation must not promise GPU exclusivity anywhere in
   the UI, the CLI or the demo README. Say "declared", not "reserved".
3. 08-P3/P4/P5 use a fake `_DispatcherAPI` (the existing test seam,
   `plugin_federation.py:162-176`) until 121-D lands. No integration test in
   those packages.
4. 121 §3 (schema), §4 (routes) and §9 (the verbose member dict) are
   **frozen** as of review round 2 — rounds 1 and 2 are folded in. 08-P3/P4/
   P5 may be written against them without waiting for 121 code. The member
   contract is deliberately minimal: add, remove,
   identical-re-POST-is-a-no-op. No partial updates, no quiesce, no budget
   top-up.
5. `inputs_b64` (121 §4.3) is on the critical path for 08-P8: land 121-G
   before rewriting the runner, or the runner has to keep both staging
   paths alive for a while.
6. **120's federation follow-up (its §PR1.6) is folded into 08-P4 — do not
   land it separately.** That step teaches `plugin_federation._route_submit`
   to project `requirements` into the dispatcher payload; 08-P4 rewrites the
   same function to forward the object (minus `node_hours`) to a class pool.
   Two people editing that one call in two PRs is a guaranteed conflict and
   a guaranteed half-state. 120 keeps the dispatcher side; the federation
   side is 08's.
7. Integration order at the end: 121-J (fake pilots + fake rhapsody, orbit
   only) → the 08 co-hosted test → `demo/local` full cycle → the acceptance
   run.

## 15. Risks

- **R1 — placement is late.** `submit` can no longer name the site. Every
  consumer that displayed placement from the submit response now shows an
  advisory value for up to one poll interval. Mitigated by keeping
  `resource` advisory-but-populated; visible in the UI as a chip that may
  change once.
- **R2 — `leave` no longer cancels.** A queued task from a departing
  resource can now run somewhere else, and its ledger entry survives the
  resource that submitted it (§4, leave step 4). That is the point of the
  decision, but it is a behaviour change for `down.sh` — hence
  `cancel_tasks` — and it means `GET task/…` can answer with
  `resource: null` for a while. Every consumer of that field must tolerate
  it; the runner test in §12 pins that.
- **R3 — cross-host staging.** Non-shared members can only be staged
  through the pilot's staging plugin (121 §8, R3). The demo is all-local,
  so this is exercised by unit tests only until Tuesday's real resources.
- **R4 — the single `fed` session is a single point of failure.** If it is
  ever swept or unregistered, *every* class pool dies. It is registered
  `lifetime: persistent` (`plugin_federation.py:232-235`) and nothing in the
  new `leave` path unregisters it; a test must assert exactly that (the
  existing TTL test in `test_plugin_federation.py` covers the sweep).
- **R5 — class explosion.** `cls` is free-form, so a typo would create a
  second, invisible pool. Mitigation is a **rejection**, not a coercion:
  a class name that does not match `^[a-z0-9][a-z0-9_-]*$` is a `400` (§2),
  and a join that creates a class no other member uses logs a warning. Do
  not lower-case `GPU` into `gpu` — silently accepting a spelling nobody
  else uses is how the second pool appears in the first place.
- **R6 — "GPU" means declared, not reserved.** With reservation deferred
  (scope note above), two GPU-tagged tasks can share a one-GPU pilot. For
  the demo the GPU work is synthetic, so nothing breaks; but the UI, the
  `atomic-resources` legend and the README must say *declared*. A reviewer
  seeing `fed-gpu` will assume exclusivity — pre-empt that in the docs.
- **R7 — demo blast radius.** `local_b` moving from allocation to login mode
  changes what the demo proves about allocation mode (only `local_a`
  exercises it), and `local_c` gaining a second member means five members
  and five potential pilots on one laptop. Both are called out in
  `demo/local/README.md`; watch the localhost cycle time in P9 (the
  measured baseline is up 14 s / campaign 24 s, `STATUS.md` 2026-09-06).
- **R8 — inputs in the submit body.** Base64 in a JSON body is ~1.33× the
  file size and rides the broker frame path. Fine for the demo's few-KB
  JSON files, wrong for a multi-MB restart file; the `413` cap (121 §4.3)
  makes the failure explicit rather than mysterious. A real bulk-input
  story stays a pilot-staging job.

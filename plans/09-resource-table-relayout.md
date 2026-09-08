# P9 — resource table: resources and their pilots, no "member"

Status: revised after review round 1, 2026-09-08. Repo `feature/demo-wm`.
Companion: Orbit plan 122 (payload contract; endpoint adoption).

## Problem

`atomic-resources` and both Explorer tabs (Orbit's federation tab and the
resources panel of `atomic_campaign.js`) show the dispatcher's unit, the
*member*, with its internal name (`default`, `cpu`, `gpu`) and a
class/pool column. Users read that as "what is this line?". Andre's target
layout (2026-09-08):

```
resource  site    software
  endpoint  mode        #nodes  #cpn  #gpn  #mpn  runtime  remaining  #run  #done  #failed  state

odo       OLCF    lammps, pytorch
  └ ep_odo  allocation  2       112   8     256   1.50     1.15       0     3      0        ok
```

## Rows

Two independent column sets: a **resource row** and, indented under it,
**pilot rows**.

- Resource row: name, site, software (union over its pilot rows), class
  badges (`fed-cpu`, `fed-gpu`) so the placement classes stay visible
  without a per-row column, then run/done/failed summed and the worst state
  of its pilot rows.
- Pilot row, one per federation member: name, mode, nodes, cpn, gpn, mpn,
  runtime, left, run, done, failed, state.
  - allocation mode: exactly one row, named after the endpoint (`ep_odo`,
    from the record's `endpoint`);
  - login mode: one row per pilot shape, named `<endpoint>/<member>`
    (`ep_perlmutter/gpu`); counts are summed over that shape's pilots (a
    shape may run up to `max_pilots`); a shape without a live pilot shows
    zeros and state `idle`.
- `failing` rows keep the `! pilot: <error>` line from the surfacing work.
- The word "member" does not appear in the CLI, the UI or the docs. The
  wire field stays `member` (Orbit 122: add, never rename).

## Columns and units

| column | source | unit |
|---|---|---|
| mode | record `mode` | `alloc` / `login` |
| #nodes, #cpn, #gpn | member `nodes`, `cpus_per_node`, `gpus_per_node` | integers |
| #mpn | `attributes.mem_gb_per_node` | GB, integer |
| runtime | `walltime_sec` | hours, 2 decimals |
| left | `remaining_sec` | hours, 2 decimals; `-` when null |
| #run / #done / #failed | usage `tasks_running/done/failed` | integers |
| state | `state` | ok / idle / failing / stale / lost |

Widths: with `remaining` → `left`, `allocation` → `alloc` and the class
moved to the resource row, a pilot row with a 21-character name fits in
about 105 columns; the CLI truncates the name with `…` beyond 24
characters (`atomic-join --endpoint` is free-form). Node-hours used/left
move to the tooltip (UI) and stay in `--json`.

## Three payload shapes

`pilots_of` (replacing `members_of` in `atomic_wm/client.py`) must handle:

1. Orbit 122: `member` rows with `endpoint`, `pilot`, `remaining_sec`,
   `state` incl. `idle`;
2. Orbit 121 without 122 (the broker pinned today): `members` list without
   those fields → `pilot` derived from the record's `mode` (`allocation` →
   endpoint), `remaining_sec` null, `idle` from `pilots_active == 0` and no
   failure;
3. pre-121 flat record: one derived row, as `members_of` does today.

`failed` is present in all three (`usage.tasks_failed`); pass it through.

## Pieces

1. `atomic_wm/client.py`: `pilots_of` with the three shapes; `members_of`
   kept as a thin alias for one release.
2. `atomic_wm/cli/resources.py`: the two column sets above.
3. `atomic_wm/ui/atomic_campaign.js`, resources panel (~lines 882-935):
   same layout, `.ac-pilot` rows replacing `.ac-member`, `idle` style,
   `pilotErrorRow` kept.
4. Docs: `docs/join.md` resources sample (lines ~248-256), demo README
   table sample; `join.py` prints `runtime` as "s remaining" — fix that
   label while here.

## Tests

- `pilots_of` on the three shapes (allocation + login records for shape 1
  and 2; flat for 3);
- CLI: allocation row named after the endpoint, login rows named
  `endpoint/member`, `idle` for a shape without a pilot, hours formatting,
  resource-row aggregation (sum, worst state, class badges), name
  truncation; `tests/test_cli_resources.py` header list and the `└ cpu`
  prefixes are rewritten on purpose;
- UI: `tests/test_ui_module.py` resource-panel assertions (three sub-rows
  at ~308-312) rewritten to the pilot rows;
- `--json` pass-through unchanged; `smoke.py`, `up.sh` use `--json` and
  `member`, unaffected.

## Sequencing

Shapes 2 and 3 work against the broker pinned today, so this can ship
before Orbit 122 lands; shape 1 fills in `left` and `idle` when it does.

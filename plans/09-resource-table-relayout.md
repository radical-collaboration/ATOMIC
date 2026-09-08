# P9 — resource table: resources and their pilots, no "member"

Status: draft 2026-09-08 (post demo). Repo `feature/demo-wm`.
Companion: Orbit plan 122 (payload contract; endpoint adoption).

## Problem

`atomic-resources` and the Explorer federation tab show the dispatcher's
unit, the *member*, with its internal name (`default`, `cpu`, `gpu`) and
a class/pool column. Users read that as "what is this line?". Andre's
target layout (2026-09-08):

```
resource  site    software
  endpoint  mode        #nodes  #cpn  #gpn  #mpn  runtime  remaining  #run  #done  #failed  state

odo       OLCF    lammps, pytorch
  └ ep_odo  allocation  2       112   8     256   1.50     1.15       0     3      0        ok
```

## Rows

- **Resource row**: name, site, software (union over pilots), then the
  task counts summed and the worst state of its pilot rows.
- **Pilot row** (one per federation member, from the payload of Orbit plan
  122): name, mode, nodes, cpn, gpn, mpn, runtime, remaining, run, done,
  failed, state, plus a small class badge (`fed-gpu`) in the UI and a
  `CLASS` column at the far right in the CLI.
  - allocation mode: exactly one row, named after the endpoint (`ep_odo`);
  - login mode: one row per pilot shape, named `<endpoint>/<shape>`
    (`ep_perlmutter/gpu`); a shape without a live pilot shows zeros and
    state `idle`.
- The word "member" does not appear in the CLI, the UI or the docs.

## Columns and units

| column | source | unit |
|---|---|---|
| mode | record `mode` | allocation / login |
| #nodes, #cpn, #gpn | member size | integers |
| #mpn | `mem_gb_per_node` | GB, integer |
| runtime | `walltime_sec` | hours, 2 decimals |
| remaining | `remaining_sec` | hours, 2 decimals; `-` when null |
| #run / #done / #failed | usage `tasks_running/done/failed` | integers |
| state | `state` | ok / idle / failing / stale / lost |

Node-hours used/left move to the tooltip (UI) and stay in `--json`.

## Pieces

1. `atomic_wm/client.py` `members_of` → `pilots_of`: returns the pilot
   rows from the new payload; keeps deriving one row for a pre-122 record
   (old broker) with `remaining` null and `failed` 0.
2. `atomic_wm/cli/resources.py`: the two-level table above; `failing`
   rows keep the `! pilot: <error>` line from the surfacing work.
3. `atomic_wm/ui/atomic_campaign.js` shows no resources today (verify);
   the federation tab lives in Orbit (`federation.js`, Orbit plan 122 does
   the layout there with the same columns).
4. Docs: `docs/` resources section, demo README table sample.

## Tests

- `pilots_of` on a 122 payload (allocation + login records) and on an old
  record;
- table rendering: allocation row named after the endpoint, login rows
  named `endpoint/shape`, `idle` for a shape without a pilot, hours
  formatting, resource-row aggregation (sum, worst state);
- `--json` unchanged pass-through.

## Sequencing

Payload first (Orbit 122 step 3), then this. Until the pinned Orbit branch
carries the payload, `pilots_of` falls back to the derivation, so the CLI
never breaks against an older broker.

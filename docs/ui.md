# The ATOMIC demo UI (portal stand-in)

One page, `atomic_wm/ui/atomic_campaign.js`. It is the ORBIT Explorer
plugin page of the broker-hosted `atomic_campaign` plugin, and it is the
thing on screen while the demo runs. It stands in for the science portal
a user would really drive: pick a workflow, sweep a parameter, watch the
campaign spread across resources, look at the plots.

Everything on screen speaks the language of the slides — **resources,
campaigns, workflows, stages**. The words *pilot*, *broker*, *endpoint*
and *dispatcher* never appear in visible text; the serving endpoint's
name is available as a tooltip on the resource name, and nowhere else.
A test enforces this (`tests/test_ui_module.py`).

## How to reach it

1. Start the broker with the plugins hosted (piece 06's local runner does
   this): `--plugins federation,atomic_campaign,task_dispatcher`.
2. Open the Explorer (the broker serves it) and connect.
3. In the sidebar, under the broker's own plugin list, click
   **atomic_campaign**.

The page is also deep-linkable: `#plugin/broker/atomic_campaign`.

The Explorer loads the module from `/plugins/atomic_campaign.js`. That
URL is derived from the *plugin registry name*, not from the file name —
the plugin points its `ui_module` at an absolute path
(`atomic_wm.ui.ui_module_path()`), the broker plugin host reads the file
off disk, and the gateway caches it until a cache miss. **Restart the
broker after editing the module**, or the browser keeps getting the old
one.

The module is plain ES2020: no libraries, no bundler, no build step. The
Explorer has to work offline, so the charts are hand-rolled inline SVG.

## What the page shows

### 1 · Resources — *demo step 2*

A table of the federation's resources, one row each:

| column | source |
|---|---|
| Resource (+ join mode badge) | `name`, `mode`; tooltip = serving endpoint |
| Site | `site` |
| Type | `kind` / `mode` |
| Cores · GPUs · Memory | `capabilities.cores` / `.gpus` / `.mem_gb` |
| Software | `capabilities.software` |
| Node-hours | `usage.node_hours_used` over the allowance, with a bar |
| Active work | `usage.tasks_running` / `usage.tasks_done` |
| Status | `liveness` → green (online) / amber (unsteady) / red (offline) |

Underneath each resource sit its **members**, one indented `└ name`
sub-row per entry in `members[]` — a resource declares one member per
shape of pilot it runs, and each member lives in the pool for its
capability class (`fed-cpu`, `fed-gpu`). The sub-row reuses the same ten
columns: the *Type* cell becomes `class / pool_name`, *Cores*/*GPUs* are
`nodes × cpus_per_node` / `nodes × gpus_per_node`, *Memory* is
`attributes.mem_gb_per_node`, and *Software*, *Node-hours* and *Active
work* come from the member's own `software`, `budget` and `usage`. The
member id and queue are tooltip material, and a member declaring GPUs
says so as *declared, not reserved* — nothing pins a GPU to a task this
round. A member whose `usage` carries no task counts shows `–`, not a
zero it cannot vouch for. A record with no `members` (a federation that predates class
pools) renders exactly as before: one row, no sub-rows.

The allowance is `budget.node_hours` where declared, otherwise
`node_hours_used + node_hours_remaining`. The bar turns amber→red past
85 % of the allowance. Rows appear and disappear as resources join and
leave — that is demo step 2, shown live without touching the page.

Data: `GET /broker/federation/resources/default`, through
`api.fetchRaw()`. No session is registered: the federation's `default`
session always exists, and a broker without the federation plugin simply
404s — which the panel reports as *"No federation available — no
resources have been joined yet"*. The rest of the page keeps working.

### 2 · Submit a campaign

- a **Workflow** dropdown of bundled example specs. `vacancy-classifier`
  (`md ▸ train`) is a verbatim copy of the spec in the demo contract and
  of `examples/workflow_vacancy.json` — a test compares the two.
  `vacancy-md-only` is a single-stage variant for a quick smoke.
- the spec itself in an **editable JSON textarea** — this is where a
  live demo can change `--steps`, a requirement, or a whole stage.
- a **sweep parameter** and its **values** (`temperature`,
  `300, 600, 900`). Numeric-looking values are sent as numbers, anything
  else as strings; leave both blank to run a single workflow.

`▶ Run campaign` POSTs `{"workflow": <spec>, "sweep": {param: [values]}}`
to `campaigns/default`. Invalid JSON, a spec with no stages, and a
half-filled sweep are all rejected client-side with a message under the
button — the demo never sends a request it knows is broken.

### 3 · Campaigns — *demo steps 3 and 4*

One card per campaign, newest first (at most six are detailed per poll).

- **header** — workflow name, state badge, short campaign id, elapsed
  time, workflow count, and a `⊘ Stop` button while it runs
  (`POST cancel/default/{cid}`). A campaign that ended FAILED,
  INTERRUPTED or CANCELED shows its `reason` under the header.
- **one row per workflow** — the sweep params (`temperature=600`) with
  the workflow's series colour, then the stage chips `md ▸ train`.
  Each chip is coloured by stage state and **labelled with the placement
  the stage's last poll reported** — `resource/member` once the class
  pool bound a member, `resource` before that. That is demo step 3: the
  same campaign, visibly spread across different resources *and* across
  the members of one class pool. The label is read from the polled stage
  record (`resource`, `member`), never from the submit answer, which can
  only name an advisory resource; a stage with no resource at all —
  queued in a class pool, or one whose resource left the federation —
  shows its state word instead and is not an error. Where colour alone
  would not be honest (a failed, skipped or staging chip) the state is
  spelled out next to the placement. The chip's tooltip carries the task
  id, state, capability class, exit code and the stage's `reason` — the
  class stays out of the chip itself, which is already tight at 720p.

  | state | style | word |
  |---|---|---|
  | `NEW` `PENDING` `QUEUED` `SUBMITTED` `WAITING` | amber | queued |
  | `STAGING` | indigo | staging |
  | `RUNNING` `ACTIVE` `EXECUTING` | indigo | running |
  | `DONE` `COMPLETED` `SUCCESS` | green | done |
  | `FAILED` `ERROR` | red | failed |
  | `INTERRUPTED` | red | interrupted |
  | `CANCELED` | grey | stopped |
  | `SKIPPED` | grey | skipped |

  Anything else renders grey with the raw state lowercased, so a state
  added later degrades quietly instead of breaking the page.
- **plots** — two inline SVG line charts, *energy vs step* from the `md`
  stage and *accuracy vs epoch* from the `train` stage. One series per
  workflow, shared axes so the curves are directly comparable, legend by
  sweep value, colours matched to the workflow rows above. That is demo
  step 4: accuracy falls as the sweep temperature rises. A chart with no
  data yet says *"waiting for the first md stage to finish"* while the
  campaign runs, and *"no md data was collected"* once it is terminal —
  never an empty box, and never a "waiting" that waits forever. Axes
  re-scale as data arrives.
- **collected results** — a disclosure link listing, per workflow and
  stage, the collected files (`workflows[].files[<stage>]`, each
  `{name, size, json, error?}` — the size, a *plotted* marker when the
  file was parsed into the metrics above, and a ⚠ carrying the error
  where collection or parsing failed), the summary scalars, and the
  central-store path the outputs were copied to.

Data: `GET campaigns/default` for the list, then `campaign/default/{cid}`
(states, resources, task ids) and `results/default/{cid}` (the metric
documents to plot, fetched only once a stage has finished) per campaign,
plus one `GET store/default` for the store root.

## Refresh behaviour

A single self-rescheduling timer: **2 s while any campaign is RUNNING,
5 s otherwise**. A hidden page (the Explorer keeps pages in the DOM)
skips its fetches and re-checks in 5 s, so switching away costs nothing.
A plugin notification (`onNotification`) nudges an extra refresh 250 ms
later, so a stage that finishes between ticks shows up immediately.

A refresh asked for while one is already in flight (a submit, a
notification) is not dropped — it is replayed as soon as the in-flight
one lands.

Every panel **keeps its last good data** on a failed poll and shows a
small amber *"showing the last known … — refresh failed"* strip above it,
so a blip on the demo network never blanks the table mid-sentence. Only
a panel that never had data shows the empty/unavailable note. A missing
federation, a campaign plugin that is not up yet, and an empty broker are
all normal states with their own message, not console errors.

**Raw server text is never rendered as visible text.** Error strings
from the gateway or another plugin routinely name Orbit internals
("dispatcher plugin not hosted", "Namespace not found for …"). The page
shows a fixed phrase — *resources unavailable*, *campaign service
unavailable*, *submit failed* — and puts the server's own wording in the
element's `title`, where it is available for debugging without ending up
on the projector.

The one exception is the campaign's own `reason`, which the campaign
plugin writes for a human and the page shows verbatim. **P4 should
therefore keep `reason` strings free of Orbit vocabulary** — e.g. prefer
"the service restarted" over "broker shutting down".

## Reading the code

| section | what is in it |
|---|---|
| `EXAMPLES` | the bundled workflow specs behind the dropdown |
| `template()` / `css()` | the static page and its styles |
| `init` / `onShow` / `onNotification` | Explorer hooks |
| `refresh` / `loadResources` / `loadCampaigns` | the polling loop |
| `renderResources` / `renderCampaigns` | the two data panels |
| `renderPlots` / `renderPlot` / `niceTicks` | the SVG chart helper |
| `_internals` | the pure helpers the tests exercise |

Module contract (verified against `orbit_explorer.html:1244-1290,
2551-2651`): `export const name`, `template()`, `css()`,
`init(page, api)`, `onShow(page, api)`,
`onNotification(data, page, api)` where
`data = {endpoint, plugin, topic, data}` and broker-hosted notifications
carry `endpoint: 'broker'`. `api.fetch(route)` is namespaced to this
plugin; `api.fetchRaw(path)` reaches another plugin.

Colours come from the Explorer's own CSS variables (`--bg2`, `--border`,
`--muted`, `--accent`, `--accent2`, `--warn`, `--danger`); the five
series colours are declared once as `--ac-s1 … --ac-s5` on `.ac-card`.

Everything is sized for **1280×720 on a screen share**:

- table and chip text sit a notch above the Explorer's defaults;
- chart text is sized in *viewBox units*, not pixels. Two plots share a
  row, so the 660-unit viewBox renders at roughly 410 px (scale ≈0.62):
  16-unit tick labels land near 10 px on screen, which is the floor for
  a projector. `PLOT_GEOMETRY` in `_internals` exposes the numbers, and
  a test asserts the tick size and the left padding that goes with it;
- the spec editor is deliberately short (8 rows, `min-height:140px`,
  resizable) so the campaigns panel stays above the fold. Drag its
  corner when a demo needs to edit a long spec.

## Troubleshooting

- **The page looks like a generic form, not this UI.** A syntax error in
  the module makes the Explorer's `loadPlugin()` swallow the import
  failure and silently fall back to the generic `ui_config` page. Check
  the browser console for the import error, and run
  `node --check atomic_wm/ui/atomic_campaign.js`.
- **Edits do not show up.** The gateway caches the module until a cache
  miss — restart the broker.
- **The page is empty but the CLI works.** Check the panel notes: they
  distinguish "no federation", "no resources joined yet", "campaign
  service unavailable" and "no campaigns yet". Hover a note or a warning
  strip for the server's own error text.

## Tests

`tests/test_ui_module.py` — skips cleanly when `node` is missing.

- `node --check` on the module;
- a fake-Explorer drive: a minimal `page`/`api` pair with canned JSON in
  the contract's shapes is handed to `init()`, and the HTML the module
  writes is asserted on — the resource rows, their member sub-rows and
  the node-hour bar, the stage chips with their `resource/member` labels
  and the STAGING/SKIPPED/INTERRUPTED
  vocabulary, the stage and campaign `reason`, both SVG charts and the
  terminal "no data" placeholder, the results drawer (files, sizes,
  per-file errors, store path, and that it closes again), the submit
  round-trip including the rejection of invalid JSON and the
  fixed-phrase-plus-tooltip handling of a rejected submit, the poll
  cadence (hidden → 5 s, then 2 s once a campaign is running), the
  coalescing of a notification burst into one 250 ms nudge, the
  "no federation" and "service unavailable" fallbacks, stale-data
  retention on a failed poll, and the vocabulary rule;
- an equality comparison of the bundled `vacancy-classifier` spec (dumped
  from `_internals` by node) against `examples/workflow_vacancy.json`.

## Known gaps

- Fields the frozen contract does not pin down are read defensively and
  documented here: campaign timestamps are looked for as
  `started_at` / `created_at` / `submitted_at` and
  `finished_at` / `ended_at` / `completed_at` (epoch seconds, epoch
  milliseconds or an ISO string all parse); a campaign summary may be a
  bare list or `{"campaigns": [...]}`; the store root is taken from
  `root` / `path` / `store` / `base` / `store_root` of
  `GET store/default`, and the results drawer simply omits paths when
  none of those is present.
- `GET results/{sid}/{cid}` is expected to hold the parsed metric
  documents under `workflows[].metrics[<stage>]` and the collected file
  records under `workflows[].files[<stage>]`. A chart falls back to
  matching the document's `type` (`simulation`, `ml_training`) when the
  stage was renamed; the drawer lists the union of both keys, so a stage
  with files but no parsable metrics (or the reverse) still appears.
- Failure explanations are read as `reason` (P4's field on `StageRun`,
  `WorkflowInstance` and `Campaign`), with `error` accepted as a
  fallback.
- No tooltips on the chart data points, and no zoom: three curves of a
  couple of hundred points read fine at demo size, and a hover layer is
  one more thing to go wrong on stage.

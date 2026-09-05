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
  (`POST cancel/default/{cid}`).
- **one row per workflow** — the sweep params (`temperature=600`) with
  the workflow's series colour, then the stage chips `md ▸ train`.
  Each chip is coloured by stage state (green done, indigo running,
  amber queued, red failed, grey stopped) and **labelled with the
  resource the stage ran on** — that is demo step 3: the same campaign,
  visibly spread across different resources. The chip's tooltip carries
  the task id, state, exit code and error, for when something goes wrong
  on stage.
- **plots** — two inline SVG line charts, *energy vs step* from the `md`
  stage and *accuracy vs epoch* from the `train` stage. One series per
  workflow, shared axes so the curves are directly comparable, legend by
  sweep value, colours matched to the workflow rows above. That is demo
  step 4: accuracy falls as the sweep temperature rises. A chart with no
  finished stage yet shows *"waiting for the first md stage to finish"*
  rather than an empty box, and the axes re-scale as data arrives.
- **collected results** — a disclosure link listing, per workflow and
  stage, the summary scalars and the central-store path the outputs were
  copied to.

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

Every panel keeps the last good data on a failed poll and reports the
failure in place; a missing federation, a campaign plugin that is not up
yet, and an empty broker are all normal states with their own message,
not console errors.

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
Font sizes are deliberately a notch above the Explorer's defaults: the
page has to be readable at 1280×720 over a screen share.

## Tests

`tests/test_ui_module.py` — skips cleanly when `node` is missing.

- `node --check` on the module;
- a fake-Explorer drive: a minimal `page`/`api` pair with canned JSON in
  the contract's shapes is handed to `init()`, and the HTML the module
  writes is asserted on — the resource rows and node-hour bar, the stage
  chips and their resource labels, both SVG charts, the submit
  round-trip (including the rejection of invalid JSON), the 2 s poll
  interval while a campaign runs, the "no federation" and "service
  unavailable" fallbacks, and the vocabulary rule;
- a comparison of the bundled `vacancy-classifier` spec against
  `examples/workflow_vacancy.json`.

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
  documents under `workflows[].metrics[<stage>]`. A chart falls back to
  matching the document's `type` (`simulation`, `ml_training`) when the
  stage was renamed.
- No tooltips on the chart data points, and no zoom: three curves of a
  couple of hundred points read fine at demo size, and a hover layer is
  one more thing to go wrong on stage.

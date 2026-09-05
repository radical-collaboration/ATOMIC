# P5 — demo UI / portal stand-in (atomic repo)

Repo/branch: `/home/merzky/projects/atomic` @ `feature/demo-wm`.
One Explorer plugin page, served as the `ui_module` of `atomic_campaign`.
It is the thing on screen during the demo, so it must be calm, legible at
1280×720 over screen-share, and never show Orbit internals.

## Constraints

- Plain ES module, no external libraries, no build step. File name is
  fixed: `atomic_wm/ui/atomic_campaign.js` (served at
  `/plugins/atomic_campaign.js`; the gateway caches it until a miss →
  restart the broker after edits). Module API (verified in
  `orbit_explorer.html:1244-1259, 2551-2607`): `export const name =
  'atomic_campaign'`, `template()`, `css()`, `init(page, api)`, `onShow()`,
  `onNotification(evt)`. Study `task_dispatcher.js` / `sysinfo.js` and
  `session_util.js`.
- Data via the gateway. `api.fetch` is namespaced to this plugin
  (campaign routes: `campaigns/default`, `campaign/default/{cid}`,
  `results/default/{cid}`, `POST campaigns/default`); federation calls go
  through `api.fetchRaw('/broker/federation/resources/default')` with
  `api.getSession('federation', {sid: 'default'})`. Poll every 2 s while
  a campaign is running, 5 s otherwise; degrade gracefully when the
  federation plugin is absent ("no federation").
- Language on screen matches the slides: *resources, campaigns,
  workflows, stages*; never "pilot", "broker", "endpoint" (endpoint name
  may appear in a tooltip).

## Layout (one page, three panels top-to-bottom)

1. **Resources** — table: Resource · Site · Type/mode · Cores/GPUs/Mem ·
   Software · Node-hours (used / allowance, thin bar) · Active work
   (running/done) · Status dot. Updates live as resources join/leave (this
   is demo step 2).
2. **Submit a campaign** — form: workflow spec (dropdown of bundled
   examples + textarea to edit JSON), sweep parameter + values, submit
   button. On submit, the campaign appears below.
3. **Campaigns** — one card per campaign: header (name, state, elapsed),
   a row per workflow showing params and stage chips (`md ▸ train`) coloured
   by state, each chip labelled with the **resource** it ran on (this is
   demo step 3), and a **plots** area: for finished stages, inline SVG line
   charts — energy vs step (md), accuracy vs epoch (train) — one series per
   workflow, legend by sweep value, shared axes so the three curves are
   comparable (demo step 4). A "results" link lists collected files.

Plot helper: `atomic_wm/ui/plot.js`-style code inside `atomic.js` (single
file is fine): axes with ticks, 3–5 series, distinct colours from the
Explorer CSS variables where possible, tooltip optional.

## Files

- `atomic_wm/ui/atomic.js` (+ packaged via `pyproject` package-data).
- `docs/ui.md` — what the page shows, how it maps to demo steps, how to
  reach it (Explorer → broker → atomic_campaign).
- `tests/test_ui_module.py` — node-based (node v22 present; `quickjs` is
  not in ve3): `node --check` for syntax, then `node --input-type=module
  -e "import(...)"` asserting the exports exist and `template()`/`css()`
  return non-empty strings; skip cleanly if `node` is missing.

## Acceptance

- Page renders in Explorer against the local demo (P6) with three
  resources, a running campaign, and plots that update as stages finish;
  no console errors (check with the browser devtools or
  `read_console_messages` if Chrome automation is available).

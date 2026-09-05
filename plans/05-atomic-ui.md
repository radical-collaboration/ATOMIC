# P5 — demo UI / portal stand-in (atomic repo)

Repo/branch: `/home/merzky/projects/atomic` @ `feature/demo-wm`.
One Explorer plugin page, served as the `ui_module` of `atomic_campaign`.
It is the thing on screen during the demo, so it must be calm, legible at
1280×720 over screen-share, and never show Orbit internals.

## Constraints

- Plain ES module, no external libraries, no build step (Explorer is a
  single-file SPA served by the gateway; study
  `radical.orbit/src/radical/orbit/data/orbit_explorer.html` for the module
  API — `export const name`, `template()`, `css()`, and the render/action
  hooks used by `task_dispatcher.js` / `sysinfo.js`; reuse
  `session_util.js` helpers for session registration).
- Data via the gateway: federation `resources/{sid}`, campaign
  `campaigns/{sid}`, `campaign/{sid}/{cid}`, `results/{sid}/{cid}`. Poll
  every 2 s while a campaign is running; fall back gracefully when the
  federation plugin is absent (show "no federation").
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
- `tests/test_ui_module.py` — load the JS with `node --check` for syntax
  and, if `quickjs` is available in ve3 (orbit's `test_explorer_js.py`
  uses it), assert exports and that `template()`/`css()` return strings;
  otherwise a node-based smoke (`node -e "import(...)"`) — pick what works
  on this machine and document it.

## Acceptance

- Page renders in Explorer against the local demo (P6) with three
  resources, a running campaign, and plots that update as stages finish;
  no console errors (check with the browser devtools or
  `read_console_messages` if Chrome automation is available).

/**
 * ATOMIC campaign page for the ORBIT Explorer.
 *
 * This is the demo's "portal stand-in": the single screen shown while the
 * campaign runs.  It is served as the `ui_module` of the broker-hosted
 * `atomic_campaign` plugin (Explorer loads `/plugins/atomic_campaign.js`,
 * hence the fixed file name) and it speaks only ATOMIC vocabulary --
 * resources, campaigns, workflows, stages.
 *
 * Three panels, top to bottom:
 *
 *   1. Resources    -- the federation's resources, node-hour budget bars,
 *                      live status (GET /broker/federation/resources/default).
 *   2. Submit       -- a bundled example workflow spec (editable JSON) plus a
 *                      sweep parameter/values, POSTed to campaigns/default.
 *   3. Campaigns    -- one card per campaign: per-workflow stage chips
 *                      coloured by state and labelled with the resource each
 *                      stage ran on, plus inline SVG line charts (energy vs
 *                      step from the `md` stage, accuracy vs epoch from the
 *                      `train` stage), one series per workflow.
 *
 * Constraints honoured here: plain ES module, no external libraries, no build
 * step (the Explorer must work offline), and no Orbit internals on screen --
 * the serving endpoint's name appears in a tooltip only.
 *
 * The gateway caches the module until a miss: restart the broker after edits.
 */

export const name = 'atomic_campaign';

// ---------------------------------------------------------------------------
// constants
// ---------------------------------------------------------------------------

// every federation/campaign route is used with the reserved session
const SID = 'default';

const POLL_BUSY_MS = 2000;   // while a campaign is RUNNING
const POLL_IDLE_MS = 5000;   // otherwise

// campaign detail/results are fetched for the most recent campaigns only --
// the demo never has more, and an old broker state file should not turn one
// poll tick into fifty requests
const MAX_DETAIL = 6;

// series colours; defined as CSS variables in css() so they track the
// Explorer palette and stay legible on the dark background
const SERIES_COLOURS = ['var(--ac-s1)', 'var(--ac-s2)', 'var(--ac-s3)',
                        'var(--ac-s4)', 'var(--ac-s5)'];

// ---------------------------------------------------------------------------
// bundled example workflow specs (the dropdown)
//
// `vacancy-classifier` is a verbatim copy of the spec in the demo contract
// (plans/00-overview.md) and of examples/workflow_vacancy.json -- keep the
// three in step when the contract changes.
// ---------------------------------------------------------------------------

const EXAMPLES = [{
  key  : 'vacancy',
  label: 'vacancy-classifier  (md ▸ train)',
  sweep: {param: 'temperature', values: '300, 600, 900'},
  spec : {
    name  : 'vacancy-classifier',
    params: {temperature: 300},
    stages: [
      {name: 'md', type: 'simulation',
       cmd: ['atomic-fake-md', '--temperature', '{temperature}',
             '--steps', '200', '--out', 'md.json'],
       requirements: {cores: 2, software: ['lammps']},
       inputs: [], outputs: ['md.json']},
      {name: 'train', type: 'ml_training',
       cmd: ['atomic-fake-train', '--in', 'md.json',
             '--epochs', '20', '--duration-sec', '10', '--out', 'model.json'],
       requirements: {cores: 1, gpus: 1, software: ['pytorch']},
       inputs: ['md.json'], outputs: ['model.json']}
    ]
  }
}, {
  key  : 'md-only',
  label: 'vacancy-md-only  (md, quick smoke)',
  sweep: {param: 'temperature', values: '300, 900'},
  spec : {
    name  : 'vacancy-md-only',
    params: {temperature: 300},
    stages: [
      {name: 'md', type: 'simulation',
       cmd: ['atomic-fake-md', '--temperature', '{temperature}',
             '--steps', '120', '--out', 'md.json'],
       requirements: {cores: 1},
       inputs: [], outputs: ['md.json']}
    ]
  }
}];

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

// The Explorer's own escHtml() throws on numbers ((5||'').replace); this page
// renders plenty of numbers, so it carries its own.
function esc(s) {
  if (s === undefined || s === null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
                  .replace(/'/g, '&#39;');
}

function num(v, fallback) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function isArr(v) {
  return Array.isArray(v);
}

function trimZeros(s) {
  return s.indexOf('.') < 0 ? s : s.replace(/0+$/, '').replace(/\.$/, '');
}

function fmtNum(v, digits) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '–';
  const a = Math.abs(n);
  if (a !== 0 && (a >= 1e5 || a < 1e-3)) return n.toExponential(1);
  return trimZeros(n.toFixed(digits === undefined ? 2 : digits));
}

function fmtDuration(sec) {
  const s = Number(sec);
  if (!Number.isFinite(s) || s < 0) return '–';
  const t = Math.floor(s);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const r = t % 60;
  if (h) return h + 'h ' + String(m).padStart(2, '0') + 'm';
  return m + ':' + String(r).padStart(2, '0');
}

// Timestamps in the contract are float epoch seconds; tolerate milliseconds
// and ISO strings so a backend variation does not blank the elapsed clock.
function toEpoch(v) {
  if (v === undefined || v === null || v === '') return null;
  if (typeof v === 'number' && Number.isFinite(v)) {
    return v > 1e11 ? v / 1000 : v;
  }
  const n = Number(v);
  if (Number.isFinite(n) && String(v).trim() !== '') {
    return n > 1e11 ? n / 1000 : n;
  }
  const d = Date.parse(String(v));
  return Number.isFinite(d) ? d / 1000 : null;
}

function firstOf(obj, keys) {
  if (!obj) return undefined;
  for (const k of keys) {
    if (obj[k] !== undefined && obj[k] !== null && obj[k] !== '') return obj[k];
  }
  return undefined;
}

function shortId(id) {
  const s = String(id || '');
  return s.length > 14 ? s.slice(0, 12) + '…' : s;
}

// A sweep value is normally a scalar, but the planner takes a cartesian
// product of whatever the spec declares -- a list or an object must still
// print as something, not as "[object Object]".
function scalar(v) {
  if (v === null || v === undefined) return '–';
  const t = typeof v;
  if (t === 'string' || t === 'number' || t === 'boolean') return String(v);
  try { return JSON.stringify(v); } catch (e) { return String(v); }
}

function paramsLabel(params) {
  if (!params || typeof params !== 'object') return '';
  const keys = Object.keys(params);
  if (!keys.length) return '';
  return keys.map(k => k + '=' + scalar(params[k])).join(' · ');
}

function fmtBytes(n) {
  const b = Number(n);
  if (!Number.isFinite(b) || b < 0) return '';
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return fmtNum(b / 1024, 1) + ' kB';
  return fmtNum(b / (1024 * 1024), 1) + ' MB';
}

// ---------------------------------------------------------------------------
// state (one page instance per Explorer session, but keyed anyway)
// ---------------------------------------------------------------------------

const STATE = new WeakMap();

function stateOf(page) {
  let st = STATE.get(page);
  if (!st) {
    st = {timer      : null,
          busy       : false,
          pending    : false,    // a refresh was asked for while busy
          resources  : [],
          fedError   : null,     // last poll failed (stale data may show)
          fedEver    : false,    // the federation ever answered
          campaigns  : [],       // [{summary, detail, results}]
          campError  : null,
          campEver   : false,
          storeRoot  : null,
          openResults: {},       // cid -> bool
          lastRender : 0};
    STATE.set(page, st);
  }
  return st;
}

// ---------------------------------------------------------------------------
// template / css
// ---------------------------------------------------------------------------

export function template() {
  const options = EXAMPLES.map(
    (e, i) => `<option value="${esc(e.key)}"${i === 0 ? ' selected' : ''}>`
            + `${esc(e.label)}</option>`).join('');

  return `
    <div class="page-header">
      <div class="page-icon">⚛️</div>
      <h2>ATOMIC — campaigns across resources</h2>
      <span class="ac-summary" title="federation summary">…</span>
      <button class="btn btn-secondary btn-sm" style="margin-left:auto"
              data-action="ac-refresh">↺ Refresh</button>
    </div>

    <div class="card ac-card" id="ac-panel-resources">
      <div class="card-title">\u{1f5fa}️ Resources
        <span class="ac-hint ac-res-count"></span>
      </div>
      <div class="ac-resources-body">
        <div class="ac-note">Loading resources…</div>
      </div>
    </div>

    <div class="card ac-card" id="ac-panel-submit">
      <div class="card-title">\u{1f680} Submit a campaign</div>
      <div class="ac-submit-grid">
        <div>
          <div class="form-group">
            <label for="ac-example">Workflow</label>
            <select id="ac-example" class="ac-example">${options}</select>
          </div>
          <div class="form-group">
            <label for="ac-sweep-param">Sweep parameter</label>
            <input id="ac-sweep-param" class="ac-sweep-param" type="text"
                   value="temperature" />
          </div>
          <div class="form-group">
            <label for="ac-sweep-values">Values (comma separated)</label>
            <input id="ac-sweep-values" class="ac-sweep-values" type="text"
                   value="300, 600, 900" />
          </div>
          <button class="btn btn-primary ac-submit-btn"
                  data-action="ac-submit">▶ Run campaign</button>
          <div class="ac-submit-status"></div>
        </div>
        <div class="form-group ac-spec-group">
          <label for="ac-spec">Workflow specification (editable — drag the
            corner to enlarge)</label>
          <textarea id="ac-spec" class="ac-spec" spellcheck="false"
                    rows="8"></textarea>
        </div>
      </div>
    </div>

    <div class="card ac-card" id="ac-panel-campaigns">
      <div class="card-title">\u{1f4c8} Campaigns
        <span class="ac-hint ac-camp-count"></span>
      </div>
      <div class="ac-campaigns-body">
        <div class="ac-note">Loading campaigns…</div>
      </div>
    </div>
  `;
}

export function css() {
  return `
    /* series palette -- kept close to the Explorer accents so the charts
       look like part of the app, and legible on the dark background */
    .ac-card {
      --ac-s1: #7c83ff;
      --ac-s2: #00d4aa;
      --ac-s3: #ffb347;
      --ac-s4: #ff6b9d;
      --ac-s5: #38bdf8;
    }
    .ac-summary {
      margin-left: 12px;
      padding: 3px 12px;
      border-radius: 12px;
      font-size: .82rem;
      font-weight: 600;
      background: var(--bg2);
      color: var(--muted);
      border: 1px solid var(--border, #ccc);
    }
    .ac-card .card-title { font-size: 1rem; }
    .ac-hint {
      margin-left: 8px;
      font-size: .78rem;
      font-weight: 400;
      color: var(--muted);
    }
    .ac-note {
      color: var(--muted);
      font-size: .88rem;
      padding: 10px 2px;
    }
    .ac-note.ac-bad { color: var(--danger); }
    /* a poll failed but the panel still shows the last good data */
    .ac-stale {
      display: inline-block;
      margin-bottom: 8px;
      padding: 3px 10px;
      border-radius: 10px;
      font-size: .76rem;
      background: rgba(255, 179, 71, .12);
      color: var(--warn);
      border: 1px solid rgba(255, 179, 71, .3);
    }
    .ac-reason {
      margin: 2px 0 10px;
      font-size: .82rem;
      color: #fb7185;
    }

    /* ---- resources table ---- */
    .ac-table { width: 100%; border-collapse: collapse; }
    .ac-table th {
      font-size: .72rem;
      padding: 8px 10px;
      white-space: nowrap;
    }
    .ac-table td {
      font-size: .86rem;
      padding: 11px 10px;
      vertical-align: middle;
      font-family: 'Inter', sans-serif;
    }
    .ac-table td.ac-mono {
      font-family: 'JetBrains Mono', monospace;
      font-size: .82rem;
    }
    .ac-res-name { font-weight: 600; color: var(--text); }
    /* one indented row per member of a resource */
    .ac-member td { padding-left: 18px; color: var(--muted); }
    .ac-soft {
      display: inline-block;
      margin: 1px 4px 1px 0;
      padding: 1px 7px;
      border-radius: 8px;
      font-size: .72rem;
      background: rgba(108, 99, 255, .14);
      color: #a5b4fc;
    }
    .ac-bar {
      height: 7px;
      border-radius: 4px;
      background: var(--border);
      overflow: hidden;
      margin-top: 5px;
      min-width: 110px;
    }
    .ac-bar > i {
      display: block;
      height: 100%;
      border-radius: 4px;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
      transition: width .5s ease;
    }
    .ac-bar.ac-hot > i {
      background: linear-gradient(90deg, var(--warn), var(--danger));
    }
    .ac-dot {
      display: inline-block;
      width: 10px; height: 10px;
      border-radius: 50%;
      margin-right: 7px;
      vertical-align: -1px;
      background: var(--muted);
    }
    .ac-dot.ok      { background: var(--accent2); box-shadow: 0 0 6px rgba(0,212,170,.6); }
    .ac-dot.suspect { background: var(--warn); }
    .ac-dot.lost    { background: var(--danger); }

    /* ---- submit form ---- */
    .ac-submit-grid {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) minmax(340px, 1.35fr);
      gap: 22px;
      align-items: start;
    }
    .ac-spec-group { margin-bottom: 0; }
    /* deliberately short: the campaigns panel must stay above the fold at
       1280x720 -- the editor is resizable for when a demo needs it big */
    .ac-spec {
      min-height: 140px;
      max-height: 60vh;
      resize: vertical;
      font-size: .78rem !important;
      line-height: 1.45;
      white-space: pre;
      overflow-wrap: normal;
      overflow-x: auto;
    }
    .ac-submit-btn { width: 100%; margin-top: 4px; font-size: .9rem; }
    .ac-submit-status {
      margin-top: 10px;
      font-size: .82rem;
      color: var(--muted);
      min-height: 1.2em;
      word-break: break-word;
    }
    .ac-submit-status.ac-bad { color: var(--danger); }
    .ac-submit-status.ac-good { color: var(--accent2); }

    /* ---- campaign cards ---- */
    .ac-camp {
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--bg3);
      padding: 16px 18px;
      margin-bottom: 16px;
    }
    .ac-camp:last-child { margin-bottom: 0; }
    .ac-camp-head {
      display: flex;
      align-items: baseline;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .ac-camp-name { font-size: 1rem; font-weight: 600; }
    .ac-camp-id {
      font-family: 'JetBrains Mono', monospace;
      font-size: .75rem;
      color: var(--muted);
    }
    .ac-camp-elapsed { font-size: .82rem; color: var(--muted); }
    .ac-camp-actions { margin-left: auto; display: flex; gap: 8px; }

    .ac-wf {
      display: flex;
      align-items: center;
      gap: 14px;
      flex-wrap: wrap;
      padding: 8px 0;
      border-top: 1px solid rgba(37, 42, 56, .7);
    }
    .ac-wf-params {
      min-width: 150px;
      font-family: 'JetBrains Mono', monospace;
      font-size: .82rem;
      color: var(--text);
    }
    .ac-wf-swatch {
      display: inline-block;
      width: 10px; height: 10px;
      border-radius: 2px;
      margin-right: 7px;
      vertical-align: -1px;
    }
    .ac-chips { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
    .ac-chip {
      display: inline-flex;
      flex-direction: column;
      line-height: 1.25;
      padding: 4px 11px;
      border-radius: 9px;
      font-size: .8rem;
      font-weight: 600;
      border: 1px solid transparent;
      cursor: default;
    }
    .ac-chip small {
      font-weight: 400;
      font-size: .68rem;
      opacity: .85;
    }
    .ac-chip.st-done    { background: rgba(0,212,170,.14);  color: var(--accent2);   border-color: rgba(0,212,170,.35); }
    .ac-chip.st-run     { background: rgba(108,99,255,.20); color: #b9bcff;          border-color: rgba(108,99,255,.5); }
    .ac-chip.st-wait    { background: rgba(255,179,71,.12); color: var(--warn);      border-color: rgba(255,179,71,.3); }
    .ac-chip.st-fail    { background: rgba(255,77,109,.14); color: #fb7185;          border-color: rgba(255,77,109,.4); }
    .ac-chip.st-cancel  { background: rgba(100,116,139,.16); color: var(--muted);    border-color: rgba(100,116,139,.4); }
    .ac-chip.st-unknown { background: var(--bg2); color: var(--muted); border-color: var(--border); }
    .ac-chip.st-run small::after { content: ' \\2026'; }
    .ac-arrow { color: var(--muted); font-size: .9rem; }

    /* ---- charts ---- */
    .ac-plots {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
      gap: 18px;
      margin-top: 14px;
    }
    .ac-plot {
      background: var(--bg2);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px 10px;
    }
    .ac-plot-title {
      font-size: .84rem;
      font-weight: 600;
      color: var(--text);
      margin-bottom: 6px;
    }
    .ac-plot svg { width: 100%; height: auto; display: block; }
    .ac-legend {
      display: flex;
      flex-wrap: wrap;
      gap: 4px 14px;
      margin-top: 6px;
      font-size: .76rem;
      color: var(--muted);
    }
    .ac-legend i {
      display: inline-block;
      width: 14px; height: 3px;
      border-radius: 2px;
      margin-right: 6px;
      vertical-align: 3px;
    }
    .ac-plot-empty {
      color: var(--muted);
      font-size: .8rem;
      font-style: italic;
      padding: 26px 0;
      text-align: center;
    }
    /* Font sizes are in viewBox units: two plots side by side at 1280x720
       render the 660-unit viewBox at roughly 410 px, so 16 units lands at
       about 10 px on screen -- the floor for a screen share. */
    .ac-axis      { stroke: var(--border); stroke-width: 1.4; }
    .ac-grid      { stroke: rgba(100,116,139,.22); stroke-width: 1.2; }
    .ac-tick-text { fill: var(--muted); font-size: 16px;
                    font-family: 'JetBrains Mono', monospace; }
    .ac-axis-text { fill: #94a3b8; font-size: 17px; font-weight: 600;
                    font-family: 'Inter', sans-serif; }

    /* ---- results drawer ---- */
    .ac-results {
      margin-top: 12px;
      border-top: 1px solid rgba(37, 42, 56, .7);
      padding-top: 10px;
    }
    .ac-results-toggle {
      background: none;
      border: none;
      color: var(--accent2);
      font-size: .82rem;
      cursor: pointer;
      padding: 0;
      font-family: inherit;
    }
    .ac-results-toggle:hover { text-decoration: underline; }
    .ac-results-body { margin-top: 8px; }
    .ac-results-body table td, .ac-results-body table th { font-size: .78rem; }
    .ac-path {
      font-family: 'JetBrains Mono', monospace;
      font-size: .74rem;
      color: var(--muted);
      word-break: break-all;
    }
  `;
}

// ---------------------------------------------------------------------------
// lifecycle
// ---------------------------------------------------------------------------

export async function init(page, api) {
  const st = stateOf(page);

  applyExample(page, EXAMPLES[0]);

  const sel = page.querySelector('.ac-example');
  if (sel) {
    sel.addEventListener('change', () => {
      const ex = EXAMPLES.find(e => e.key === sel.value);
      if (ex) applyExample(page, ex);
    });
  }

  const refreshBtn = page.querySelector('[data-action="ac-refresh"]');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => refresh(page, api));
  }

  const submitBtn = page.querySelector('[data-action="ac-submit"]');
  if (submitBtn) {
    submitBtn.addEventListener('click', () => submitCampaign(page, api));
  }

  // delegated: results drawers and cancel buttons live inside re-rendered HTML
  const body = page.querySelector('.ac-campaigns-body');
  if (body) {
    body.addEventListener('click', ev => onCampaignClick(ev, page, api));
  }

  st.timer = null;
  await refresh(page, api);
}

export async function onShow(page, api) {
  await refresh(page, api);
}

// The campaign plugin may push stage-state notifications; any of them means
// "something moved", so pull a fresh snapshot rather than trusting the event
// payload shape (which is not part of the frozen contract).
export function onNotification(evt, page, api) {
  if (!page || !api) return;
  const st = stateOf(page);
  if (st.nudge) return;
  st.nudge = setTimeout(() => {
    st.nudge = null;
    refresh(page, api);
  }, 250);
}

function schedule(page, api, delay) {
  const st = stateOf(page);
  if (st.timer) clearTimeout(st.timer);
  st.timer = setTimeout(() => refresh(page, api), delay);
}

// ---------------------------------------------------------------------------
// data refresh
// ---------------------------------------------------------------------------

async function refresh(page, api) {
  const st = stateOf(page);
  // a refresh asked for mid-poll (a submit, a notification) is not dropped:
  // it is replayed once the in-flight one lands
  if (st.busy) { st.pending = true; return; }

  // The Explorer keeps hidden pages in the DOM; do not poll what nobody sees,
  // but keep the timer alive so the page is current the moment it is shown.
  if (!page.isConnected) return;
  if (!page.classList.contains('active')) {
    schedule(page, api, POLL_IDLE_MS);
    return;
  }

  st.busy = true;
  try {
    await Promise.all([loadResources(page, api), loadCampaigns(page, api)]);
    render(page);
  } catch (e) {
    // loadX already record their own errors; this catches render bugs
    // eslint-disable-next-line no-console
    console.error('atomic_campaign: refresh failed', e);
  } finally {
    st.busy = false;
  }

  schedule(page, api, anyRunning(st) ? POLL_BUSY_MS : POLL_IDLE_MS);

  if (st.pending) {
    st.pending = false;
    await refresh(page, api);
  }
}

function anyRunning(st) {
  return st.campaigns.some(c => campaignState(c) === 'RUNNING');
}

async function loadResources(page, api) {
  const st = stateOf(page);
  try {
    // No session registration: the federation's `default` session always
    // exists, and a missing federation plugin simply 404s here -- which is
    // exactly the "no federation" case, handled below.
    const r = await api.fetchRaw(`/broker/federation/resources/${SID}`,
                                 {quiet: true});
    st.resources = isArr(r && r.resources) ? r.resources : [];
    st.fedError  = null;
    st.fedEver   = true;
  } catch (e) {
    // keep the last good resource list: one failed poll on a demo network
    // must not blank the table mid-sentence
    st.fedError = shortError(e);
  }
}

async function loadCampaigns(page, api) {
  const st = stateOf(page);
  let list;
  try {
    const r = await api.fetch(`campaigns/${SID}`, {quiet: true});
    list = isArr(r) ? r
         : isArr(r && r.campaigns) ? r.campaigns
         : [];
    st.campError = null;
    st.campEver  = true;
  } catch (e) {
    // as above: hold the last good campaign list
    st.campError = shortError(e);
    return;
  }

  list = list.slice().sort((a, b) => sortKey(b) - sortKey(a));
  const head = list.slice(0, MAX_DETAIL);

  const prev = {};
  for (const c of st.campaigns) prev[cidOf(c.summary)] = c;

  st.campaigns = await Promise.all(head.map(async summary => {
    const cid  = cidOf(summary);
    const old  = prev[cid] || {};
    const item = {summary: summary, detail: old.detail, results: old.results};

    try {
      item.detail = await api.fetch(`campaign/${SID}/${cid}`, {quiet: true});
    } catch (e) { /* keep the last good detail */ }

    // results are only interesting once a stage has finished
    if (hasFinishedStage(item.detail) || hasFinishedStage(summary)) {
      try {
        item.results = await api.fetch(`results/${SID}/${cid}`, {quiet: true});
      } catch (e) { /* keep the last good results */ }
    }
    return item;
  }));

  if (st.storeRoot === null) {
    try {
      const s = await api.fetch(`store/${SID}`, {quiet: true});
      st.storeRoot = firstOf(s, ['root', 'path', 'store', 'base', 'store_root'])
                  || false;
    } catch (e) {
      st.storeRoot = false;   // asked once, do not ask again
    }
  }
}

function shortError(e) {
  const m = (e && e.message) ? String(e.message) : String(e);
  return m.length > 160 ? m.slice(0, 157) + '…' : m;
}

function cidOf(c) {
  return String(firstOf(c, ['campaign_id', 'cid', 'id']) || '');
}

function sortKey(c) {
  return toEpoch(firstOf(c, ['created_at', 'submitted_at', 'started_at'])) || 0;
}

function campaignState(item) {
  const s = firstOf(item.detail, ['state']) || firstOf(item.summary, ['state']);
  return String(s || 'UNKNOWN').toUpperCase();
}

function workflowsOf(item) {
  const wfs = firstOf(item.detail, ['workflows'])
           || firstOf(item.summary, ['workflows']);
  return isArr(wfs) ? wfs : [];
}

function hasFinishedStage(obj) {
  const wfs = obj && obj.workflows;
  if (!isArr(wfs)) return false;
  return wfs.some(w => isArr(w && w.stages)
                    && w.stages.some(s => stateClass(s && s.state) === 'st-done'));
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

function render(page) {
  const st = stateOf(page);
  renderResources(page, st);
  renderCampaigns(page, st);

  const sum = page.querySelector('.ac-summary');
  if (sum) {
    const nres = st.resources.length;
    const ncmp = st.campaigns.length;
    sum.textContent = (st.fedError && !nres)
      ? 'no federation'
      : `${nres} resource${nres === 1 ? '' : 's'} · `
      + `${ncmp} campaign${ncmp === 1 ? '' : 's'}`;
  }
}

// ---- panel 1: resources ---------------------------------------------------

function renderResources(page, st) {
  const body  = page.querySelector('.ac-resources-body');
  const count = page.querySelector('.ac-res-count');
  if (!body) return;

  // Raw server text can name Orbit internals ("dispatcher", "Namespace not
  // found for ..."); it lives in a tooltip, never in visible text.
  if (st.fedError && !st.resources.length) {
    const msg = st.fedEver ? 'Resources unavailable.'
                           : 'No federation available — no resources have '
                           + 'been joined yet.';
    body.innerHTML = `<div class="ac-note" title="${esc(st.fedError)}">`
                   + `${esc(msg)}</div>`;
    if (count) count.textContent = '';
    return;
  }

  if (!st.resources.length) {
    body.innerHTML = '<div class="ac-note">No resources joined yet.</div>';
    if (count) count.textContent = '';
    return;
  }

  const stale = st.fedError
    ? `<div class="ac-stale" title="${esc(st.fedError)}">`
    + `⚠ showing the last known resources — refresh failed</div>` : '';

  if (count) {
    const nh = st.resources.reduce(
      (a, r) => a + num(r && r.usage && r.usage.node_hours_used, 0), 0);
    count.textContent = `· ${fmtNum(nh, 2)} node-hours used`;
  }

  const rows = st.resources.map(r => renderResourceRow(r)
                                   + membersOf(r).map(m => renderMemberRow(r, m))
                                                 .join('')).join('');
  body.innerHTML = `${stale}
    <div style="overflow-x:auto">
      <table class="ac-table">
        <thead><tr>
          <th>Resource</th><th>Site</th><th>Type</th>
          <th>Cores</th><th>GPUs</th><th>Memory</th>
          <th>Software</th><th>Node-hours</th><th>Active work</th>
          <th>Status</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// A resource declares one MEMBER per shape of pilot it is willing to run,
// and each member sits in the pool for its capability class (fed-cpu,
// fed-gpu).  A federation that predates class pools reports none, and then
// the resource row is the whole story -- exactly as before.
function membersOf(r) {
  const members = (r && r.members) || [];
  return isArr(members) ? members.filter(m => m && typeof m === 'object') : [];
}

// The node-hour cell: `used / total h` plus a bar, or just what was used
// when nothing declared a budget.  Shared by resource and member rows.
function nodeHourCell(usage, bud) {
  const used  = num(usage.node_hours_used, 0);
  const left  = num(usage.node_hours_remaining, NaN);
  let   total = num(bud.node_hours, NaN);
  if (!Number.isFinite(total)) {
    total = Number.isFinite(left) ? used + left : NaN;
  }
  const frac = (Number.isFinite(total) && total > 0)
             ? Math.max(0, Math.min(1, used / total)) : 0;

  return Number.isFinite(total)
    ? `${fmtNum(used, 2)} / ${fmtNum(total, 1)} h
       <div class="ac-bar${frac > 0.85 ? ' ac-hot' : ''}">
         <i style="width:${(frac * 100).toFixed(1)}%"></i></div>`
    : `${fmtNum(used, 2)} h <span style="color:var(--muted)">used</span>`;
}

function softwareCell(list) {
  return isArr(list) && list.length
    ? list.map(s => `<span class="ac-soft">${esc(s)}</span>`).join('')
    : '<span style="color:var(--muted)">–</span>';
}

function renderMemberRow(r, m) {
  const attrs = m.attributes || {};
  const nodes = num(m.nodes, 1);
  const cores = nodes * num(m.cpus_per_node, 0);
  const gpus  = nodes * num(m.gpus_per_node, 0);
  const cls   = m['class'] || m.cls || '';
  const pool  = m.pool_name || '';
  const mem   = num(attrs.mem_gb_per_node, NaN);
  const usage = m.usage || {};

  // GPUs here are DECLARED, not reserved -- nothing pins one to a task.
  const tip = [m.member_id ? `member ${m.member_id}` : '',
               m.queue ? `queue ${m.queue}` : '',
               gpus ? `${gpus} declared GPU(s), not reserved` : '']
              .filter(Boolean).join(' · ');

  return `<tr class="ac-member">
    <td><span title="${esc(tip)}">└ ${esc(m.member || '?')}</span></td>
    <td>${esc(attrs.site || r.site || '–')}</td>
    <td>${esc([cls, pool].filter(Boolean).join(' / ') || '–')}</td>
    <td class="ac-mono">${cores}</td>
    <td class="ac-mono">${gpus}</td>
    <td class="ac-mono">${Number.isFinite(mem) ? esc(mem) + ' GB' : '–'}</td>
    <td>${softwareCell(m.software)}</td>
    <td class="ac-mono" style="min-width:150px">${
      nodeHourCell(usage, m.budget || {})}</td>
    <td class="ac-mono">${num(usage.tasks_running, 0)} running
        · ${num(usage.tasks_done, 0)} done</td>
    <td>${esc(m.liveness || r.liveness || '')}</td>
  </tr>`;
}

function renderResourceRow(r) {
  r = r || {};
  const caps  = r.capabilities || {};
  const usage = r.usage || {};
  const bud   = r.budget || {};

  const nh   = nodeHourCell(usage, bud);
  const soft = softwareCell(caps.software);

  const live = String(r.liveness || '').toLowerCase();
  const dot  = live === 'ok' ? 'ok'
             : live === 'suspect' ? 'suspect'
             : live === 'lost' ? 'lost' : '';
  const liveLabel = live === 'ok' ? 'online'
                  : live === 'suspect' ? 'unsteady'
                  : live === 'lost' ? 'offline' : 'unknown';

  const kind = [r.kind, r.mode].filter(Boolean).join(' / ') || '–';
  // the only place an Orbit-internal name is allowed: a tooltip
  const tip = r.endpoint ? `serving endpoint: ${r.endpoint}` : '';

  return `<tr>
    <td><span class="ac-res-name" title="${esc(tip)}">${esc(r.name)}</span></td>
    <td>${esc(r.site || '–')}</td>
    <td>${esc(kind)}</td>
    <td class="ac-mono">${esc(caps.cores !== undefined ? caps.cores : '–')}</td>
    <td class="ac-mono">${esc(caps.gpus !== undefined ? caps.gpus : 0)}</td>
    <td class="ac-mono">${caps.mem_gb !== undefined
                         ? esc(caps.mem_gb) + ' GB' : '–'}</td>
    <td>${soft}</td>
    <td class="ac-mono" style="min-width:150px">${nh}</td>
    <td class="ac-mono">${num(usage.tasks_running, 0)} running
        · ${num(usage.tasks_done, 0)} done</td>
    <td><span class="ac-dot ${dot}"></span>${esc(liveLabel)}</td>
  </tr>`;
}

// ---- panel 2: submit ------------------------------------------------------

function applyExample(page, ex) {
  const ta = page.querySelector('.ac-spec');
  if (ta) ta.value = JSON.stringify(ex.spec, null, 2);

  const p = page.querySelector('.ac-sweep-param');
  const v = page.querySelector('.ac-sweep-values');
  if (p && ex.sweep) p.value = ex.sweep.param;
  if (v && ex.sweep) v.value = ex.sweep.values;
}

// "300, 600, 900" -> [300, 600, 900]; non-numeric entries stay strings.
function parseSweepValues(text) {
  return String(text || '').split(',')
    .map(s => s.trim())
    .filter(s => s !== '')
    .map(s => {
      const n = Number(s);
      return (s !== '' && Number.isFinite(n)) ? n : s;
    });
}

// `tip` carries the server's own wording, which may name Orbit internals --
// it belongs in a tooltip, never in the visible line.
function setSubmitStatus(page, msg, kind, tip) {
  const el = page.querySelector('.ac-submit-status');
  if (!el) return;
  el.className = 'ac-submit-status' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  el.title = tip || '';
}

async function submitCampaign(page, api) {
  const ta    = page.querySelector('.ac-spec');
  const pEl   = page.querySelector('.ac-sweep-param');
  const vEl   = page.querySelector('.ac-sweep-values');
  const btn   = page.querySelector('.ac-submit-btn');

  let spec;
  try {
    spec = JSON.parse(ta ? ta.value : '');
  } catch (e) {
    setSubmitStatus(page, 'Workflow specification is not valid JSON: '
                        + e.message, 'ac-bad');
    return;
  }
  if (!spec || typeof spec !== 'object' || isArr(spec)) {
    setSubmitStatus(page, 'Workflow specification must be a JSON object.',
                    'ac-bad');
    return;
  }
  if (!isArr(spec.stages) || !spec.stages.length) {
    setSubmitStatus(page, 'Workflow specification has no stages.', 'ac-bad');
    return;
  }

  const body = {workflow: spec};
  const key  = (pEl ? pEl.value : '').trim();
  const vals = parseSweepValues(vEl ? vEl.value : '');
  if (key && vals.length) {
    body.sweep = {[key]: vals};
  } else if (key || vals.length) {
    setSubmitStatus(page, 'Give both a sweep parameter and its values '
                        + '(or neither, to run a single workflow).', 'ac-bad');
    return;
  }

  if (btn) btn.disabled = true;
  setSubmitStatus(page, 'Submitting…', '');
  try {
    const r = await api.fetch(`campaigns/${SID}`, {
      method: 'POST',
      body  : JSON.stringify(body)
    });
    const cid = cidOf(r);
    const n   = isArr(r && r.workflows) ? r.workflows.length : vals.length || 1;
    setSubmitStatus(page,
      `Campaign ${shortId(cid) || '?'} started — `
      + `${n} workflow${n === 1 ? '' : 's'}.`, 'ac-good');
    if (api.flash) api.flash('Campaign submitted');
    await refresh(page, api);
  } catch (e) {
    setSubmitStatus(page, 'Submit failed — the campaign service rejected '
                        + 'the request.', 'ac-bad', shortError(e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---- panel 3: campaigns ---------------------------------------------------

function onCampaignClick(ev, page, api) {
  const t = ev.target.closest ? ev.target.closest('[data-ac-action]') : null;
  if (!t) return;
  const action = t.getAttribute('data-ac-action');
  const cid    = t.getAttribute('data-cid');
  const st     = stateOf(page);

  if (action === 'toggle-results') {
    st.openResults[cid] = !st.openResults[cid];
    render(page);
  } else if (action === 'cancel') {
    t.disabled = true;
    api.fetch(`cancel/${SID}/${cid}`, {method: 'POST'})
       .then(() => refresh(page, api))
       .catch(() => {
         // again: a fixed phrase, not the server's own words
         if (api.flash) api.flash('Could not stop the campaign', false);
         t.disabled = false;
       });
  }
}

function renderCampaigns(page, st) {
  const body  = page.querySelector('.ac-campaigns-body');
  const count = page.querySelector('.ac-camp-count');
  if (!body) return;

  // as in the resources panel: the server's own words stay in a tooltip
  if (st.campError && !st.campaigns.length) {
    body.innerHTML = `<div class="ac-note ac-bad" `
                   + `title="${esc(st.campError)}">Campaign service `
                   + `unavailable.</div>`;
    if (count) count.textContent = '';
    return;
  }
  if (!st.campaigns.length) {
    body.innerHTML = '<div class="ac-note">No campaigns yet — submit '
                   + 'one above.</div>';
    if (count) count.textContent = '';
    return;
  }
  if (count) {
    const running = st.campaigns.filter(
      c => campaignState(c) === 'RUNNING').length;
    count.textContent = running ? `· ${running} running` : '';
  }

  const stale = st.campError
    ? `<div class="ac-stale" title="${esc(st.campError)}">`
    + `⚠ showing the last known campaigns — refresh failed</div>` : '';

  body.innerHTML = stale
                 + st.campaigns.map(c => renderCampaign(c, st)).join('');
}

function renderCampaign(item, st) {
  const cid   = cidOf(item.summary) || cidOf(item.detail);
  const state = campaignState(item);
  const wfs   = workflowsOf(item);
  const name  = firstOf(item.detail, ['name', 'workflow_name'])
             || firstOf(item.summary, ['name', 'workflow_name'])
             || workflowName(wfs)
             || 'campaign';

  const t0 = toEpoch(firstOf(item.detail, ['started_at', 'created_at',
                                           'submitted_at']))
          || toEpoch(firstOf(item.summary, ['started_at', 'created_at',
                                            'submitted_at']));
  const t1 = toEpoch(firstOf(item.detail, ['finished_at', 'ended_at',
                                           'completed_at']))
          || toEpoch(firstOf(item.summary, ['finished_at', 'ended_at',
                                            'completed_at']));
  const elapsed = t0 ? fmtDuration((t1 || (Date.now() / 1000)) - t0) : null;

  const colours = seriesColours(wfs);

  const rows = wfs.length
    ? wfs.map((w, i) => renderWorkflowRow(w, colours[i])).join('')
    : '<div class="ac-note">No workflows in this campaign.</div>';

  const plots   = renderPlots(item, colours, isTerminal(state));
  const results = renderResults(item, st, cid);

  const cancel = (state === 'RUNNING')
    ? `<button class="btn btn-secondary btn-sm" data-ac-action="cancel"
               data-cid="${esc(cid)}">⊘ Stop</button>` : '';

  // the detail fetch may have failed while the summary is still good
  const nwf = wfs.length
           || num(firstOf(item.summary, ['n_workflows', 'workflow_count']), 0);

  // `reason` is the campaign's own explanation (P4's Campaign.reason) --
  // written by the campaign plugin for a human, so it may be shown
  const reason = (state === 'FAILED' || state === 'INTERRUPTED'
                  || state === 'CANCELED')
    ? firstOf(item.detail, ['reason']) || firstOf(item.summary, ['reason'])
    : null;

  return `<div class="ac-camp">
    <div class="ac-camp-head">
      <span class="ac-camp-name">${esc(name)}</span>
      <span class="badge ${stateBadge(state)}">${esc(stateLabel(state))}</span>
      <span class="ac-camp-id" title="${esc(cid)}">${esc(shortId(cid))}</span>
      ${elapsed ? `<span class="ac-camp-elapsed">${esc(elapsed)} elapsed
        · ${nwf} workflow${nwf === 1 ? '' : 's'}</span>` : ''}
      <span class="ac-camp-actions">${cancel}</span>
    </div>
    ${reason ? `<div class="ac-reason">${esc(reason)}</div>` : ''}
    ${rows}
    ${plots}
    ${results}
  </div>`;
}

function isTerminal(state) {
  const c = stateClass(state);
  return c === 'st-done' || c === 'st-fail' || c === 'st-cancel';
}

function workflowName(wfs) {
  for (const w of wfs) {
    const n = firstOf(w, ['name', 'workflow']);
    if (n) return n;
  }
  return null;
}

// Where a stage ran: `resource/member` once the pool picked a member,
// `resource` before that, '' when it is not placed (the federation
// answers `resource: null` for a task waiting in a class pool, and for
// one whose resource left while it was queued).
function placementOf(s) {
  const res = (s && s.resource) || '';
  const mem = (s && s.member)   || '';
  if (!res) return '';
  return mem ? `${res}/${mem}` : res;
}

function renderWorkflowRow(w, colour) {
  w = w || {};
  const stages = isArr(w.stages) ? w.stages : [];
  const label  = paramsLabel(w.params) || shortId(firstOf(w, ['id', 'wf_id']));

  const chips = stages.map((s, i) => {
    const state = String((s && s.state) || '').toUpperCase();
    const cls   = stateClass(state);
    // Placement comes from the POLL, never from the submit answer: a
    // capability class pool picks the member when it dispatches, so
    // `resource` starts out advisory and may be absent altogether.
    const res   = placementOf(s);
    const bits  = [];
    if (s && s.task_id) bits.push('task ' + s.task_id);
    if (state) bits.push('state ' + state);
    // the class is tooltip material: the chip is already tight at 720p
    if (s && (s.cls || s.class)) bits.push('class ' + (s.cls || s.class));
    if (s && s.exit_code !== undefined && s.exit_code !== null) {
      bits.push('exit ' + s.exit_code);
    }
    // P4 names the human-readable failure explanation `reason` (StageRun,
    // WorkflowInstance and Campaign all carry it); `error` is tolerated
    const why = firstOf(s, ['reason', 'error']);
    if (why) bits.push(String(why));
    // The chip's job in the demo is to name the resource the stage ran on;
    // but a failed / skipped / staging chip must still say so in words, not
    // only in colour.
    const word = stateWord(state);
    const dim  = t => `<span style="opacity:.7">${esc(t)}</span>`;
    let sub;
    if (!res)                                        sub = dim(word);
    else if (cls === 'st-fail' || cls === 'st-cancel'
             || state === 'STAGING')                 sub = esc(res) + ' · '
                                                         + dim(word);
    else if (cls === 'st-wait')                      sub = dim(word);
    else                                             sub = esc(res);
    return (i ? '<span class="ac-arrow">▸</span>' : '')
         + `<span class="ac-chip ${cls}" title="${esc(bits.join(' · '))}">`
         + `${esc((s && s.name) || 'stage')}<small>${sub}</small></span>`;
  }).join('');

  return `<div class="ac-wf">
    <span class="ac-wf-params">
      <i class="ac-wf-swatch" style="background:${colour}"></i>${esc(label)}
    </span>
    <span class="ac-chips">${chips
      || '<span style="color:var(--muted)">no stages</span>'}</span>
  </div>`;
}

// The states P4's state.py declares for a stage / workflow / campaign,
// mapped onto chip styling.  Anything unknown renders grey rather than
// breaking, so a new state added later degrades quietly.
const STATE_STYLE = {
  NEW        : 'st-wait',   PENDING  : 'st-wait',   QUEUED   : 'st-wait',
  SUBMITTED  : 'st-wait',   WAITING  : 'st-wait',
  STAGING    : 'st-run',    RUNNING  : 'st-run',    ACTIVE   : 'st-run',
  EXECUTING  : 'st-run',
  DONE       : 'st-done',   COMPLETED: 'st-done',   SUCCESS  : 'st-done',
  FAILED     : 'st-fail',   ERROR    : 'st-fail',   INTERRUPTED: 'st-fail',
  CANCELED   : 'st-cancel', CANCELLED: 'st-cancel', SKIPPED  : 'st-cancel'
};

// what a state is called on screen (never the raw enum for the odd ones)
const STATE_WORD = {
  STAGING    : 'staging',    RUNNING : 'running',   ACTIVE   : 'running',
  EXECUTING  : 'running',    NEW     : 'queued',    PENDING  : 'queued',
  QUEUED     : 'queued',     SUBMITTED: 'queued',   WAITING  : 'queued',
  DONE       : 'done',       COMPLETED: 'done',     SUCCESS  : 'done',
  FAILED     : 'failed',     ERROR   : 'failed',
  INTERRUPTED: 'interrupted',
  CANCELED   : 'stopped',    CANCELLED: 'stopped',  SKIPPED  : 'skipped'
};

function stateClass(state) {
  return STATE_STYLE[String(state || '').toUpperCase()] || 'st-unknown';
}

function stateWord(state) {
  const s = String(state || '').toUpperCase();
  return STATE_WORD[s] || (s ? s.toLowerCase() : '–');
}

// the badge on a campaign header: the state spelled the way the page does
function stateLabel(state) {
  const s = String(state || '').toUpperCase();
  return (STATE_WORD[s] || s || 'unknown').toUpperCase();
}

function stateBadge(state) {
  const c = stateClass(state);
  return c === 'st-done'   ? 'badge-green'
       : c === 'st-fail'   ? 'badge-red'
       : c === 'st-run'    ? 'badge-blue'
       : c === 'st-wait'   ? 'badge-orange'
       : 'badge-gray';
}

function seriesColours(wfs) {
  return wfs.map((w, i) => SERIES_COLOURS[i % SERIES_COLOURS.length]);
}

// ---------------------------------------------------------------------------
// plots
// ---------------------------------------------------------------------------

// The two charts the demo tells its story with.  `stage` is matched against
// the stage name first and against the metric document's `type` second, so a
// renamed stage still plots.
const CHARTS = [
  {stage: 'md',    type: 'simulation',
   title: 'Energy vs step — md',      x: 'step',  y: 'energy',
   xlabel: 'step',  ylabel: 'energy'},
  {stage: 'train', type: 'ml_training',
   title: 'Accuracy vs epoch — train', x: 'epoch', y: 'accuracy',
   xlabel: 'epoch', ylabel: 'accuracy'}
];

function renderPlots(item, colours, terminal) {
  const res = item.results;
  const wfs = isArr(res && res.workflows) ? res.workflows : [];
  const declared = workflowsOf(item);

  // colour a results workflow the same as its row above
  const order = {};
  declared.forEach((w, i) => { order[String(firstOf(w, ['id', 'wf_id']))] = i; });

  const varying = varyingKeys(wfs.length ? wfs : declared);

  const panels = CHARTS.map(cfg => {
    const series = [];
    wfs.forEach((w, i) => {
      const doc = pickMetrics(w, cfg);
      const s   = doc && doc.series;
      if (!s) return;
      const xs = s[cfg.x];
      const ys = s[cfg.y];
      if (!isArr(xs) || !isArr(ys) || !xs.length) return;
      const n  = Math.min(xs.length, ys.length);
      const pts = [];
      for (let k = 0; k < n; k++) {
        const x = Number(xs[k]);
        const y = Number(ys[k]);
        if (Number.isFinite(x) && Number.isFinite(y)) pts.push([x, y]);
      }
      if (!pts.length) return;
      const idx = order[String(firstOf(w, ['id', 'wf_id']))];
      series.push({
        label : legendLabel(w, varying, i),
        colour: colours[Number.isFinite(idx) ? idx : i]
             || SERIES_COLOURS[i % SERIES_COLOURS.length],
        points: pts
      });
    });
    return renderPlot(cfg, series, terminal);
  }).filter(Boolean);

  if (!panels.length) return '';
  return `<div class="ac-plots">${panels.join('')}</div>`;
}

// A workflow's metrics for one chart: by stage name, else by document type.
function pickMetrics(w, cfg) {
  const m = w && w.metrics;
  if (!m || typeof m !== 'object') return null;
  if (m[cfg.stage] && typeof m[cfg.stage] === 'object') return m[cfg.stage];
  for (const k of Object.keys(m)) {
    const d = m[k];
    if (d && typeof d === 'object' && d.type === cfg.type) return d;
  }
  return null;
}

// Sweep keys are the params that actually differ between workflows -- that is
// what the legend must show ("300 K", not the full param dict).
function varyingKeys(wfs) {
  const seen = {};
  for (const w of wfs) {
    const p = (w && w.params) || {};
    for (const k of Object.keys(p)) {
      const v = JSON.stringify(p[k]);
      if (!seen[k]) seen[k] = new Set();
      seen[k].add(v);
    }
  }
  const keys = Object.keys(seen).filter(k => seen[k].size > 1);
  return keys.length ? keys : Object.keys(seen);
}

function legendLabel(w, keys, i) {
  const p = (w && w.params) || {};
  const parts = keys.filter(k => p[k] !== undefined)
                    .map(k => `${k}=${scalar(p[k])}`);
  if (parts.length) return parts.join(' · ');
  const id = firstOf(w, ['id', 'wf_id']);
  return id ? shortId(id) : `workflow ${i + 1}`;
}

// --- the SVG line chart ----------------------------------------------------

// Geometry is in viewBox units.  Two plots side by side at 1280x720 render
// this 660-unit box at roughly 410 px (scale ~0.62), so the 16-unit tick
// text lands near 10 px on screen; the padding is sized for that text, not
// for the 11 px it used to be.
const PW = 660, PH = 320;
const PAD = {l: 88, r: 18, t: 16, b: 56};

function renderPlot(cfg, series, terminal) {
  if (!series.length) {
    // a campaign that will never produce this metric should say so, rather
    // than leave a "waiting…" that waits forever
    const msg = terminal
      ? `no ${cfg.stage} data was collected`
      : `waiting for the first ${cfg.stage} stage to finish…`;
    return `<div class="ac-plot">
      <div class="ac-plot-title">${esc(cfg.title)}</div>
      <div class="ac-plot-empty">${esc(msg)}</div>
    </div>`;
  }

  let xlo = Infinity, xhi = -Infinity, ylo = Infinity, yhi = -Infinity;
  for (const s of series) {
    for (const [x, y] of s.points) {
      if (x < xlo) xlo = x;
      if (x > xhi) xhi = x;
      if (y < ylo) ylo = y;
      if (y > yhi) yhi = y;
    }
  }
  if (!Number.isFinite(xlo)) { xlo = 0; xhi = 1; }
  if (!Number.isFinite(ylo)) { ylo = 0; yhi = 1; }
  if (xhi === xlo) { xhi = xlo + 1; }
  if (yhi === ylo) { const d = Math.abs(ylo) * 0.05 || 0.5; ylo -= d; yhi += d; }
  else { const pad = (yhi - ylo) * 0.08; ylo -= pad; yhi += pad; }

  const iw = PW - PAD.l - PAD.r;
  const ih = PH - PAD.t - PAD.b;
  const sx = x => PAD.l + (x - xlo) / (xhi - xlo) * iw;
  const sy = y => PAD.t + ih - (y - ylo) / (yhi - ylo) * ih;

  const xt = niceTicks(xlo, xhi, 5);
  const yt = niceTicks(ylo, yhi, 5);

  let g = '';
  for (const t of yt) {
    const y = sy(t).toFixed(1);
    g += `<line class="ac-grid" x1="${PAD.l}" y1="${y}"
                x2="${PAD.l + iw}" y2="${y}"/>`
       + `<text class="ac-tick-text" x="${PAD.l - 10}" y="${y}"`
       + ` text-anchor="end" dominant-baseline="middle">`
       + `${esc(fmtNum(t, 3))}</text>`;
  }
  for (const t of xt) {
    const x = sx(t).toFixed(1);
    g += `<line class="ac-grid" x1="${x}" y1="${PAD.t}"
                x2="${x}" y2="${PAD.t + ih}"/>`
       + `<text class="ac-tick-text" x="${x}" y="${PAD.t + ih + 22}"`
       + ` text-anchor="middle">${esc(fmtNum(t, 3))}</text>`;
  }

  const axes = `<line class="ac-axis" x1="${PAD.l}" y1="${PAD.t}"
                      x2="${PAD.l}" y2="${PAD.t + ih}"/>
                <line class="ac-axis" x1="${PAD.l}" y1="${PAD.t + ih}"
                      x2="${PAD.l + iw}" y2="${PAD.t + ih}"/>`;

  const lines = series.map(s => {
    const pts = downsample(s.points, 400);
    const d   = pts.map(p => `${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`)
                   .join(' ');
    const dots = pts.length <= 24
      ? pts.map(p => `<circle cx="${sx(p[0]).toFixed(1)}"
                              cy="${sy(p[1]).toFixed(1)}" r="3.4"
                              fill="${s.colour}"/>`).join('')
      : '';
    return `<polyline fill="none" stroke="${s.colour}" stroke-width="2.6"
                      stroke-linejoin="round" stroke-linecap="round"
                      points="${d}"/>${dots}`;
  }).join('');

  const ymid   = PAD.t + ih / 2;
  const labels = `<text class="ac-axis-text" x="${PAD.l + iw / 2}"`
               + ` y="${PH - 8}" text-anchor="middle">`
               + `${esc(cfg.xlabel)}</text>`
               + `<text class="ac-axis-text" x="18" y="${ymid}"`
               + ` text-anchor="middle" transform="rotate(-90 18 ${ymid})">`
               + `${esc(cfg.ylabel)}</text>`;

  const legend = series.map(s =>
    `<span><i style="background:${s.colour}"></i>${esc(s.label)}</span>`
  ).join('');

  return `<div class="ac-plot">
    <div class="ac-plot-title">${esc(cfg.title)}</div>
    <svg viewBox="0 0 ${PW} ${PH}" role="img"
         aria-label="${esc(cfg.title)}">${g}${axes}${lines}${labels}</svg>
    <div class="ac-legend">${legend}</div>
  </div>`;
}

function downsample(points, maxPts) {
  if (points.length <= maxPts) return points;
  const stride = Math.ceil(points.length / maxPts);
  const out = [];
  for (let i = 0; i < points.length; i += stride) out.push(points[i]);
  const last = points[points.length - 1];
  if (out[out.length - 1] !== last) out.push(last);
  return out;
}

// "nice" tick positions (1/2/5 x 10^n) inside [lo, hi]
function niceTicks(lo, hi, count) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return [lo];
  const raw  = (hi - lo) / Math.max(1, count);
  const mag  = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const out  = [];
  const start = Math.ceil(lo / step) * step;
  for (let v = start; v <= hi + step * 1e-9 && out.length < 20; v += step) {
    out.push(Number(v.toPrecision(12)));
  }
  return out.length ? out : [lo, hi];
}

// ---------------------------------------------------------------------------
// results drawer
// ---------------------------------------------------------------------------

function renderResults(item, st, cid) {
  const res  = item.results;
  const wfs  = isArr(res && res.workflows) ? res.workflows : [];
  const open = !!st.openResults[cid];

  if (!wfs.length) return '';

  const head = `<button class="ac-results-toggle" data-ac-action="toggle-results"
                        data-cid="${esc(cid)}">${open ? '▾' : '▸'}
                  collected results (${wfs.length} workflow${
                    wfs.length === 1 ? '' : 's'})</button>`;
  if (!open) return `<div class="ac-results">${head}</div>`;

  const root = st.storeRoot;
  const rows = [];
  for (const w of wfs) {
    const wid   = String(firstOf(w, ['id', 'wf_id']) || '');
    const m     = (w && w.metrics) || {};
    const files = (w && w.files)   || {};   // P4's store.py: {stage: [file]}
    // a stage may have collected files but no parsable metrics (or the
    // other way round) -- list the union, so nothing silently vanishes
    const stages = Array.from(new Set(Object.keys(m)
                                      .concat(Object.keys(files))));
    for (const stage of stages) {
      const doc  = m[stage] || {};
      const sums = (doc && doc.summary) || {};
      const txt  = Object.keys(sums).slice(0, 4)
                     .map(k => `${k} = ${fmtNum(sums[k], 3)}`).join(', ');
      const path = root ? `${root}/${cid}/${wid}/${stage}/` : '';
      rows.push(`<tr>
        <td>${esc(paramsLabel(w.params) || shortId(wid))}</td>
        <td>${esc(stage)}</td>
        <td>${renderFiles(files[stage])}</td>
        <td>${esc(txt || '–')}</td>
        <td class="ac-path">${esc(path)}</td>
      </tr>`);
    }
  }

  const body = rows.length
    ? `<table class="ac-table"><thead><tr>
         <th>Workflow</th><th>Stage</th><th>Files</th>
         <th>Summary</th><th>Stored at</th>
       </tr></thead><tbody>${rows.join('')}</tbody></table>`
    : '<div class="ac-note">Nothing collected yet.</div>';

  return `<div class="ac-results">${head}
            <div class="ac-results-body">${body}</div></div>`;
}

// One stage's collected files: P4's store.py records each as
// {name, size, json, error?} -- `json` says the file was parsed into the
// metrics above, `error` says why collection or parsing did not work.
function renderFiles(files) {
  if (!isArr(files) || !files.length) {
    return '<span style="color:var(--muted)">–</span>';
  }
  return files.map(f => {
    f = f || {};
    const size = fmtBytes(f.size);
    const bits = [];
    if (size) bits.push(size);
    if (f.json) bits.push('plotted');
    const tail = bits.length ? ` <span style="color:var(--muted)">(`
                             + esc(bits.join(', ')) + ')</span>' : '';
    const bad  = f.error
      ? ` <span style="color:#fb7185" title="${esc(f.error)}">⚠</span>` : '';
    return `<div class="ac-path" style="color:var(--text)">`
         + `${esc(f.name || '?')}${tail}${bad}</div>`;
  }).join('');
}

// ---------------------------------------------------------------------------
// exported for tests (pure helpers, no DOM)
// ---------------------------------------------------------------------------

export const _internals = {
  EXAMPLES, esc, scalar, fmtNum, fmtBytes, fmtDuration, toEpoch,
  parseSweepValues, stateClass, stateBadge, stateWord, stateLabel,
  varyingKeys, legendLabel, paramsLabel, niceTicks, downsample,
  renderPlot, renderFiles, renderResourceRow, renderMemberRow, membersOf,
  placementOf, pickMetrics,
  PLOT_GEOMETRY: {PW, PH, PAD}
};

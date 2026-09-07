
"""Tests for the Explorer UI module (`atomic_wm/ui/atomic_campaign.js`).

The module is plain JavaScript, so the tests drive it with `node` (v22 is
present on the demo machine; `quickjs` -- which radical.orbit's own
explorer tests use -- is not in `ve3`).  Everything node-based skips
cleanly where node is missing, so the suite still passes on a login node.

Three levels:

* `node --check`  -- the file parses as an ES module;
* an import smoke -- the documented hooks are exported and `template()` /
  `css()` return non-empty strings, and the three panel containers exist;
* a fake-Explorer drive -- a minimal `page` / `api` pair (canned JSON, no
  network, no DOM library) is handed to `init()`, and the HTML the module
  writes into the page is asserted on: resources table, node-hour bar,
  stage chips labelled with their resource, the two SVG charts, the
  submit round-trip, the poll cadence (5 s hidden / 2 s while running),
  the notification nudge, the results drawer, stale-data retention, and
  the on-screen vocabulary rule (no Orbit internals in visible text).

The fake-Explorer harness lives in this file as `HARNESS_JS` rather than
as a checked-in `.js` file: it is test scaffolding, it must stay next to
the assertions it makes, and it keeps `atomic_wm/ui/` holding exactly the
one file the broker serves.
"""

import json
import os
import shutil
import subprocess

import pytest

from atomic_wm.ui import ui_module_path


NODE = shutil.which('node')

needs_node = pytest.mark.skipif(NODE is None,
                                reason='node is not installed')


# ---------------------------------------------------------------------------
# the harness: a minimal stand-in for the Explorer page + plugin API
# ---------------------------------------------------------------------------

HARNESS_JS = r"""
const MODULE = process.argv[2];

// Freeze the module's polling before it is imported: the module resolves
// setTimeout off the global object at call time, so a stub installed here
// captures every scheduled poll instead of keeping node alive.
let timers = [];
globalThis.setTimeout   = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
globalThis.clearTimeout = () => {};
const delays = () => timers.map(t => t[1]);

const fail = [];
function check(cond, msg) { if (!cond) fail.push(msg); }

// --- tiny DOM shim: only what the module actually touches -----------------
class El {
  constructor(sel) {
    this.sel = sel;
    this.innerHTML = ''; this.textContent = ''; this.className = '';
    this.value = ''; this.title = ''; this.disabled = false;
    this.listeners = {};
  }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
}

class Page {
  constructor(active = true) {
    this.els = new Map();
    this.isConnected = true;
    this.active = active;
    this.classList = { contains: c => c === 'active' && this.active };
    this.listeners = {};
  }
  querySelector(sel) {
    if (!this.els.has(sel)) this.els.set(sel, new El(sel));
    return this.els.get(sel);
  }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  html(sel) { return this.querySelector(sel).innerHTML; }
}

// fire a delegated click carrying the data-ac-action attributes the module
// reads off the clicked node
function fireClick(el, attrs) {
  const node = { getAttribute: k => (k in attrs ? attrs[k] : null),
                 disabled: false };
  const ev = { target: { closest: () => node } };
  for (const fn of (el.listeners.click || [])) fn(ev);
}

// --- canned backend (the frozen contract's shapes) ------------------------
const NOW = Date.now() / 1000;

const RESOURCES = { resources: [
  { name: 'local_a', endpoint: 'ep_local_a', mode: 'allocation',
    site: 'localhost', kind: 'workstation',
    capabilities: { cores: 8, gpus: 0, mem_gb: 32,
                    software: ['lammps', 'pytorch'] },
    budget: { node_hours: 4.0 },
    usage: { node_hours_used: 0.5, node_hours_remaining: 3.5,
             pilots_active: 1, tasks_running: 2, tasks_done: 4 },
    liveness: 'ok' },
  { name: 'local_b', endpoint: 'ep_local_b', mode: 'login',
    site: 'localhost', kind: 'cluster',
    capabilities: { cores: 4, gpus: 1, mem_gb: 16, software: ['pytorch'] },
    budget: { node_hours: 2.0 },
    usage: { node_hours_used: 1.9, node_hours_remaining: 0.1,
             tasks_running: 0, tasks_done: 3 },
    liveness: 'suspect',
    // two members in two capability class pools
    members: [
      { member: 'cpu', member_id: 'local_b.cpu', class: 'cpu',
        pool_name: 'fed-cpu', queue: 'local', nodes: 1, cpus_per_node: 2,
        gpus_per_node: 0, software: ['pytorch'],
        attributes: { site: 'NERSC', mem_gb_per_node: 16 },
        budget: { node_hours: 1.0 },
        usage: { node_hours_used: 0.5, node_hours_remaining: 0.5,
                 tasks_running: 0, tasks_done: 2 },
        liveness: 'ok' },
      { member: 'gpu', member_id: 'local_b.gpu', class: 'gpu',
        pool_name: 'fed-gpu', queue: 'local', nodes: 1, cpus_per_node: 1,
        gpus_per_node: 1, software: ['pytorch'],
        attributes: { site: 'PSC' },
        budget: { node_hours: 1.0 },
        usage: { node_hours_used: 1.4, node_hours_remaining: 0,
                 tasks_running: 0, tasks_done: 1 },
        liveness: 'ok' },
      // a member whose name and site carry markup, and whose usage
      // carries no task counts at all
      { member: '<b>x"y', member_id: 'local_b.<b>x"y', class: 'cpu',
        pool_name: 'fed-cpu', nodes: 1, cpus_per_node: 1,
        software: [], attributes: { site: '"><script>' },
        budget: {}, usage: {}, liveness: 'ok' } ] },
] };

// `m1`/`m2` are the MEMBERS the poll reported for the two stages; a null
// resource is what the federation answers for a task the class pool has
// not placed (or whose resource has left)
function wf(id, temp, s1, r1, s2, r2, reason, m1, m2) {
  return { id, params: { temperature: temp },
           stages: [ { name: 'md', state: s1, resource: r1,
                       member: m1 || null, cls: 'cpu', pool: 'fed-cpu',
                       task_id: 't.' + id + '.md',
                       exit_code: s1 === 'DONE' ? 0 : null,
                       reason: reason || null },
                     { name: 'train', state: s2, resource: r2,
                       member: m2 || null, cls: 'gpu', pool: 'fed-gpu',
                       task_id: 't.' + id + '.train' } ] };
}

const DETAIL = {
  campaign_id: 'camp.001', name: 'vacancy-classifier', state: 'RUNNING',
  created_at: NOW - 95,
  workflows: [ wf('wf.0', 300, 'DONE', 'local_a', 'DONE', 'local_a',
                  null, 'default', 'gpu'),
               wf('wf.1', 600, 'DONE', 'local_b', 'RUNNING', 'local_b',
                  null, 'cpu', 'gpu'),
               wf('wf.2', 900, 'RUNNING', 'local_a', 'NEW', null),
               wf('wf.3', 1200, 'STAGING', 'local_b', 'SKIPPED', null) ],
};

// a second, terminal campaign: exercises INTERRUPTED, the campaign-level
// `reason`, and the "no data" placeholder on a campaign that will never
// produce metrics
const DEAD = {
  campaign_id: 'camp.000', name: 'vacancy-md-only', state: 'INTERRUPTED',
  created_at: NOW - 900, finished_at: NOW - 800,
  reason: 'stopped while the service restarted',
  workflows: [ wf('wf.x', 300, 'INTERRUPTED', 'local_a', 'SKIPPED', null,
                  'stage did not finish') ],
};

function md(temp) {
  const step = [], energy = [];
  for (let i = 0; i < 40; i++) {
    step.push(i * 5);
    energy.push(-100 + temp / 100 + Math.sin(i / 4));
  }
  return { type: 'simulation', series: { step, energy },
           summary: { mean_energy: -99.2, std_energy: 0.7 } };
}

function train(temp) {
  const epoch = [], accuracy = [];
  for (let i = 1; i <= 20; i++) {
    epoch.push(i);
    accuracy.push(0.99 - (temp - 300) / 6000 - 0.3 / i);
  }
  return { type: 'ml_training', series: { epoch, accuracy },
           summary: { final_accuracy: 0.99 - (temp - 300) / 6000 } };
}

const RESULTS = { workflows: [
  { id: 'wf.0', params: { temperature: 300 },
    metrics: { md: md(300), train: train(300) },
    files  : { md   : [ { name: 'md.json', size: 5120, json: true } ],
               train: [ { name: 'model.json', size: 2048, json: true },
                        { name: 'stderr.txt', size: 12, json: false,
                          error: 'could not be parsed' } ] } },
  { id: 'wf.1', params: { temperature: 600 }, metrics: { md: md(600) },
    files: { md: [ { name: 'md.json', size: 5120, json: true } ] } },
  { id: 'wf.2', params: { temperature: 900 }, metrics: {} },
] };

const SUMMARY = { campaign_id: 'camp.001', name: 'vacancy-classifier',
                  state: 'RUNNING', created_at: NOW - 95,
                  n_workflows: 4, workflows: DETAIL.workflows };
const SUMMARY0 = { campaign_id: 'camp.000', name: 'vacancy-md-only',
                   state: 'INTERRUPTED', created_at: NOW - 900,
                   n_workflows: 1 };

const calls = [];
let submitted = null;

const api = {
  async fetch(path, opts = {}) {
    calls.push(path);
    if (path === 'campaigns/default' && opts.method === 'POST') {
      submitted = JSON.parse(opts.body);
      return { campaign_id: 'camp.002', state: 'RUNNING',
               workflows: [{}, {}, {}] };
    }
    if (path === 'campaigns/default')
      return { campaigns: [SUMMARY, SUMMARY0] };
    if (path === 'campaign/default/camp.001') return DETAIL;
    if (path === 'campaign/default/camp.000') return DEAD;
    if (path === 'results/default/camp.001')  return RESULTS;
    if (path === 'results/default/camp.000')  return { workflows: [] };
    if (path === 'store/default')
      return { root: '/home/u/.radical/orbit/atomic_store' };
    throw new Error('HTTP 404: no route ' + path);
  },
  async fetchRaw(path) {
    calls.push(path);
    if (path === '/broker/federation/resources/default') return RESOURCES;
    throw new Error('HTTP 404: no route ' + path);
  },
  flash() {},
  escHtml: s => String(s || ''),
};

// --- exports and static template ------------------------------------------
const m = await import(MODULE);

check(m.name === 'atomic_campaign', 'export name is not "atomic_campaign"');
for (const fn of ['template', 'css', 'init', 'onShow', 'onNotification']) {
  check(typeof m[fn] === 'function', `export ${fn} is not a function`);
}
const tmpl  = m.template();
const style = m.css();
check(typeof tmpl === 'string'  && tmpl.length  > 0, 'template() empty');
check(typeof style === 'string' && style.length > 0, 'css() empty');
for (const id of ['ac-panel-resources', 'ac-panel-submit',
                  'ac-panel-campaigns']) {
  check(tmpl.includes('id="' + id + '"'), `template() lacks panel id ${id}`);
}

// the spec editor must not push the campaigns panel off a 720 px screen
check(/min-height:\s*140px/.test(style), 'spec editor is not short (140px)');
check(/rows="8"/.test(tmpl), 'spec editor is not 8 rows');

// chart text is sized in viewBox units: ~16 units -> ~10 px on screen once
// two 660-unit plots share a 1280 px row
check(/\.ac-tick-text[^}]*font-size:\s*16px/.test(style),
      'chart tick text is smaller than 16 viewBox units');
check(m._internals.PLOT_GEOMETRY.PAD.l >= 80,
      'left padding is too small for 16-unit tick labels');

// --- the page starts hidden: poll idles at 5 s ----------------------------
const page = new Page(false);
await m.init(page, api);
check(calls.length === 0, 'a hidden page must not fetch: ' + calls);
check(JSON.stringify(delays()) === '[5000]',
      'a hidden page must re-check in 5000 ms, got ' + JSON.stringify(delays()));

// --- becomes visible: onShow does a full load and drops to 2 s ------------
timers = [];
page.active = true;
await m.onShow(page, api);
check(delays()[delays().length - 1] === 2000,
      'poll is not 2000 ms while a campaign RUNs: ' + JSON.stringify(delays()));

for (const p of ['/broker/federation/resources/default', 'campaigns/default',
                 'campaign/default/camp.001', 'results/default/camp.001',
                 'store/default']) {
  check(calls.includes(p), `route never called: ${p}`);
}

const res = page.html('.ac-resources-body');
check(res.includes('local_a') && res.includes('local_b'), 'resources missing');
check(res.includes('lammps'), 'software list missing');
check(/width:\s*12\.5%/.test(res), 'node-hour bar is not 0.5/4.0 for local_a');
check(res.includes('ac-dot ok') && res.includes('ac-dot suspect'),
      'status dots missing');
check(res.includes('serving endpoint: ep_local_a'),
      'endpoint name missing from the tooltip');

// --- member sub-rows ------------------------------------------------------
// local_b declares two members in two class pools; local_a declares none
// and must render exactly as before (one row, no sub-rows)
check((res.match(/class="ac-member"/g) || []).length === 3,
      'expected three member sub-rows, got '
      + (res.match(/class="ac-member"/g) || []).length);
// a member name / site out of a join spec is data, not markup
check(!/<b>x/.test(res) && !/<script>/.test(res),
      'a member name or site was rendered as markup');
check(res.includes('&lt;b&gt;') || res.includes('&lt;'),
      'a hostile member name was not escaped at all');
// no task counts reported for that member: '–', not a zero we invented
check(m._internals.countCell(undefined) === '–'
      && m._internals.countCell(0) === '0'
      && m._internals.countCell(3) === '3',
      'an absent task count must render as a dash, not 0');
check(res.includes('└ cpu') && res.includes('└ gpu'),
      'member sub-rows are not labelled with the member name');
check(res.includes('cpu / fed-cpu') && res.includes('gpu / fed-gpu'),
      'member sub-rows do not name the class and its pool');
check(res.includes('PSC'), "a member's own site is not rendered");
check(/member local_b\.gpu/.test(res),
      'the member id is not in the sub-row tooltip');
check(/declared GPU\(s\), not reserved/.test(res),
      'the GPU tooltip must say declared, not reserved');
check((res.match(/width:100\.0%/) || []).length >= 1,
      "the gpu member's node-hour bar is not full (1.4 of 1.4 h)");
check((res.match(/allocation/g) || []).length === 1,
      'the join mode is rendered twice (badge + Type column)');
check(!/NaN|undefined/.test(res), 'resources HTML contains NaN/undefined');

const camp = page.html('.ac-campaigns-body');
check(camp.includes('vacancy-classifier'), 'campaign name missing');
check(camp.includes('temperature=300') && camp.includes('temperature=900'),
      'workflow params missing');
for (const cls of ['st-done', 'st-run', 'st-wait', 'st-cancel', 'st-fail']) {
  check(camp.includes(cls), `stage chip class ${cls} never rendered`);
}
check(camp.includes('staging') && camp.includes('skipped')
      && camp.includes('interrupted'),
      'STAGING / SKIPPED / INTERRUPTED are not spelled out');
check((camp.match(/local_a/g) || []).length >= 2,
      'stage chips are not labelled with their resource');

// --- placement chips read resource/member off the POLLED stage -----------
check(camp.includes('local_a/default') && camp.includes('local_b/gpu'),
      'stage chips do not show resource/member');
// the class is tooltip material only -- the chip is tight at 720p
check(/title="[^"]*class gpu/.test(camp),
      'the capability class is not in the chip tooltip');
check(!/>\s*class gpu/.test(camp.replace(/<[^>]*>/g, m => m)),
      'the class leaked onto the chip itself');
// a stage the class pool has not placed carries no resource at all
check(m._internals.placementOf({resource: null, member: null}) === '',
      'a null resource must render as "not placed", not as text');
check(m._internals.placementOf({resource: 'r', member: null}) === 'r',
      'a stage with a resource and no member must show the resource');
check(m._internals.placementOf({resource: 'r', member: 'gpu'}) === 'r/gpu',
      'placement must read resource/member');
check(m._internals.membersOf({}).length === 0
      && m._internals.membersOf({members: [{member: 'x'}]}).length === 1,
      'membersOf does not tolerate a record without members');
check(camp.includes('stage did not finish'),
      'the stage `reason` is not in the chip tooltip');
check(camp.includes('stopped while the service restarted'),
      'the campaign-level `reason` is not shown for INTERRUPTED');
check(camp.includes('<svg'), 'no svg chart rendered');
check((camp.match(/<polyline/g) || []).length >= 3,
      'expected at least 3 polylines (2 md series + 1 train series)');
check(camp.includes('Energy vs step') && camp.includes('Accuracy vs epoch'),
      'chart titles missing');
check(camp.includes('no md data was collected'),
      'a terminal campaign should say "no data", not "waiting"');
check(camp.includes('elapsed'), 'campaign elapsed time missing');
check(!/NaN|undefined|\[object Object\]/.test(camp),
      'campaign HTML contains NaN/undefined');

// on-screen vocabulary: Orbit internals only ever inside attributes.  (The
// campaign's own `reason` is server-authored prose and is exempt -- it is
// stripped here with the elements the module wraps it in.)
const visible = (res + camp + tmpl).replace(/<div class="ac-reason">[^<]*<\/div>/g, ' ')
                                   .replace(/<[^>]*>/g, ' ');
for (const w of ['pilot', 'broker', 'endpoint', 'dispatcher', 'namespace']) {
  check(!new RegExp(w, 'i').test(visible),
        `forbidden word "${w}" in visible text`);
}

// --- a notification nudges exactly one extra refresh ----------------------
timers = [];
m.onNotification({endpoint: 'broker', plugin: 'atomic_campaign',
                  topic: 'stage', data: {}}, page, api);
m.onNotification({endpoint: 'broker', plugin: 'atomic_campaign',
                  topic: 'stage', data: {}}, page, api);
check(JSON.stringify(delays()) === '[250]',
      'a burst of notifications must coalesce into one 250 ms nudge, got '
      + JSON.stringify(delays()));

// --- results drawer -------------------------------------------------------
const body = page.querySelector('.ac-campaigns-body');
fireClick(body, {'data-ac-action': 'toggle-results', 'data-cid': 'camp.001'});
const drawer = page.html('.ac-campaigns-body');
check(drawer.includes('/home/u/.radical/orbit/atomic_store/camp.001/wf.0/md/'),
      'the store path is not rendered in the results drawer');
check(drawer.includes('md.json') && drawer.includes('model.json'),
      'collected file names are not rendered');
check(drawer.includes('kB'), 'collected file sizes are not rendered');
check(drawer.includes('could not be parsed'),
      'a per-file error is not surfaced');
check(drawer.includes('mean_energy'), 'summary scalars are not rendered');
fireClick(body, {'data-ac-action': 'toggle-results', 'data-cid': 'camp.001'});
check(!page.html('.ac-campaigns-body').includes('mean_energy'),
      'the results drawer does not close again');

// --- submit round-trip -----------------------------------------------------
const spec = page.querySelector('.ac-spec').value;
check(JSON.parse(spec).name === 'vacancy-classifier',
      'bundled example spec not loaded into the textarea');
const btn = page.querySelector('[data-action="ac-submit"]');
await btn.listeners.click[0]();
check(submitted && submitted.workflow
      && submitted.workflow.name === 'vacancy-classifier',
      'submit body carries no workflow');
check(submitted
      && JSON.stringify(submitted.sweep) === '{"temperature":[300,600,900]}',
      'submit sweep wrong: ' + JSON.stringify(submitted && submitted.sweep));

page.querySelector('.ac-spec').value = '{not json';
submitted = null;
await btn.listeners.click[0]();
check(submitted === null, 'invalid JSON was submitted anyway');
check(/not valid JSON/.test(page.querySelector('.ac-submit-status').textContent),
      'no JSON error shown to the user');

// a rejected submit shows a fixed phrase; the server's words go in a tooltip
const page9 = new Page();
const api9  = { ...api,
                fetch: async (p, o = {}) => {
                  if (o.method === 'POST') {
                    throw new Error('HTTP 503: dispatcher plugin not hosted');
                  }
                  return api.fetch(p, o);
                } };
await m.init(page9, api9);
await page9.querySelector('[data-action="ac-submit"]').listeners.click[0]();
const status = page9.querySelector('.ac-submit-status');
check(!/dispatcher/i.test(status.textContent),
      'a raw server error leaked into visible text: ' + status.textContent);
check(/dispatcher/i.test(status.title),
      'the server error is not preserved in the tooltip');

// --- degrade gracefully ----------------------------------------------------
const page2 = new Page();
const api2  = { ...api,
                fetchRaw: async () => { throw new Error('HTTP 404: nope'); } };
await m.init(page2, api2);
check(/no federation/i.test(page2.html('.ac-resources-body')),
      'a missing federation is not reported as "no federation"');
check(page2.html('.ac-campaigns-body').includes('vacancy-classifier'),
      'campaigns must still render without a federation');

const page3 = new Page();
const api3  = { ...api,
                fetch   : async () => { throw new Error('HTTP 503: nope'); },
                fetchRaw: async () => ({ resources: [] }) };
await m.init(page3, api3);
check(page3.html('.ac-resources-body').includes('No resources joined'),
      'empty resource list note missing');
check(page3.html('.ac-campaigns-body').includes('unavailable'),
      'campaign service error note missing');

// --- a failed poll keeps the last good data --------------------------------
const page4 = new Page();
let broken = false;
const api4 = {
  ...api,
  fetch   : async (p, o) => { if (broken) throw new Error('HTTP 500: dispatcher exploded'); return api.fetch(p, o); },
  fetchRaw: async (p)    => { if (broken) throw new Error('HTTP 500: dispatcher exploded'); return api.fetchRaw(p); },
};
await m.init(page4, api4);
check(page4.html('.ac-resources-body').includes('local_a'), 'first load failed');
broken = true;
await m.onShow(page4, api4);
const r4 = page4.html('.ac-resources-body');
const c4 = page4.html('.ac-campaigns-body');
check(r4.includes('local_a'), 'a failed poll blanked the resources table');
check(c4.includes('vacancy-classifier'), 'a failed poll blanked the campaigns');
check(r4.includes('last known resources') && c4.includes('last known campaigns'),
      'a failed poll shows no stale-data warning');
check(!/dispatcher/i.test((r4 + c4).replace(/<[^>]*>/g, ' ')),
      'the raw poll error leaked into visible text');

// --- pure helpers ----------------------------------------------------------
const I = m._internals;
check(JSON.stringify(I.parseSweepValues('300, 600,900')) === '[300,600,900]',
      'parseSweepValues does not parse numbers');
check(JSON.stringify(I.parseSweepValues('a, b ,')) === '["a","b"]',
      'parseSweepValues does not keep strings / drop blanks');
check(I.stateClass('STAGING') === 'st-run'
      && I.stateClass('SKIPPED') === 'st-cancel'
      && I.stateClass('INTERRUPTED') === 'st-fail'
      && I.stateClass('DONE') === 'st-done'
      && I.stateClass('zzz') === 'st-unknown', 'stateClass mapping');
check(I.stateWord('STAGING') === 'staging'
      && I.stateWord('SKIPPED') === 'skipped'
      && I.stateWord('INTERRUPTED') === 'interrupted', 'stateWord mapping');
check(I.stateBadge('INTERRUPTED') === 'badge-red', 'stateBadge(INTERRUPTED)');
check(I.paramsLabel({a: [1, 2]}) === 'a=[1,2]',
      'a non-scalar param is not JSON-stringified: ' + I.paramsLabel({a: [1, 2]}));
check(I.niceTicks(0, 100, 5).length >= 4, 'niceTicks');
check(I.downsample(Array.from({length: 5000}, (_, i) => [i, i]), 400).length
      <= 402, 'downsample');
check(I.renderPlot({stage: 'md', title: 't', xlabel: 'x', ylabel: 'y'}, [], false)
       .includes('waiting for'), 'empty plot has no placeholder');
check(I.renderPlot({stage: 'md', title: 't', xlabel: 'x', ylabel: 'y'}, [], true)
       .includes('no md data'), 'terminal empty plot still says "waiting"');
check(I.renderFiles([{name: 'a.json', size: 2048, json: true}])
       .includes('2 kB'), 'renderFiles size');
check(I.EXAMPLES.length >= 1
      && I.EXAMPLES[0].spec.name === 'vacancy-classifier',
      'bundled examples missing');
check(I.esc('<a>&"') === '&lt;a&gt;&amp;&quot;', 'esc does not escape');

if (fail.length) {
  console.error('FAIL:\n  ' + fail.join('\n  '));
  process.exit(1);
}
console.log('OK (' + calls.length + ' api calls)');
"""


# dumps the module's first bundled example spec as JSON on stdout
DUMP_SPEC_JS = r"""
const m = await import(process.argv[2]);
process.stdout.write(JSON.stringify(m._internals.EXAMPLES[0].spec));
"""


# ---------------------------------------------------------------------------
def _run_node(script, tmp_path, name):

    path = tmp_path / name
    path.write_text(script, encoding='utf-8')

    return subprocess.run([NODE, str(path), ui_module_path()],
                          capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------
def test_ui_module_is_present():

    path = ui_module_path()

    assert os.path.isabs(path)
    assert os.path.basename(path) == 'atomic_campaign.js'
    assert os.path.isfile(path)

    src = open(path, encoding='utf-8').read()

    assert "export const name = 'atomic_campaign'" in src
    assert len(src) > 1000


# ---------------------------------------------------------------------------
@needs_node
def test_ui_module_parses():

    out = subprocess.run([NODE, '--check', ui_module_path()],
                         capture_output=True, text=True)

    assert out.returncode == 0, out.stderr


# ---------------------------------------------------------------------------
@needs_node
def test_bundled_example_equals_the_shipped_spec(tmp_path):

    # the dropdown's `vacancy-classifier` entry and examples/
    # workflow_vacancy.json are the same spec written twice (the module
    # cannot read a file at load time) -- they must stay byte-equivalent
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ref  = os.path.join(root, 'examples', 'workflow_vacancy.json')

    if not os.path.exists(ref):
        pytest.skip('examples/workflow_vacancy.json not present')

    out = _run_node(DUMP_SPEC_JS, tmp_path, 'dump.mjs')

    assert out.returncode == 0, out.stderr

    with open(ref, encoding='utf-8') as fin:
        assert json.loads(out.stdout) == json.load(fin)


# ---------------------------------------------------------------------------
@needs_node
def test_ui_module_drives_a_fake_explorer(tmp_path):

    out = _run_node(HARNESS_JS, tmp_path, 'harness.mjs')

    assert out.returncode == 0, (out.stdout + '\n' + out.stderr)
    assert 'OK' in out.stdout

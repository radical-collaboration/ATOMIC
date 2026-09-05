
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
  submit round-trip, the 2 s poll while a campaign runs, graceful
  degradation when the federation or the campaign service is absent, and
  the on-screen vocabulary rule (no Orbit internals in visible text).

The fake-Explorer harness lives in this file as `HARNESS_JS` rather than
as a checked-in `.js` file: it is test scaffolding, it must stay next to
the assertions it makes, and it keeps `atomic_wm/ui/` holding exactly the
one file the broker serves.
"""

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
const timers = [];
globalThis.setTimeout   = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
globalThis.clearTimeout = () => {};

const fail = [];
function check(cond, msg) { if (!cond) fail.push(msg); }

// --- tiny DOM shim: only what the module actually touches -----------------
class El {
  constructor(sel) {
    this.sel = sel;
    this.innerHTML = ''; this.textContent = ''; this.className = '';
    this.value = ''; this.disabled = false; this.listeners = {};
  }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
}

class Page {
  constructor() {
    this.els = new Map();
    this.isConnected = true;
    this.classList = { contains: c => c === 'active' };
    this.listeners = {};
  }
  querySelector(sel) {
    if (!this.els.has(sel)) this.els.set(sel, new El(sel));
    return this.els.get(sel);
  }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  html(sel) { return this.querySelector(sel).innerHTML; }
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
    liveness: 'suspect' },
] };

function wf(id, temp, s1, r1, s2, r2) {
  return { id, params: { temperature: temp },
           stages: [ { name: 'md', state: s1, resource: r1,
                       task_id: 't.' + id + '.md',
                       exit_code: s1 === 'DONE' ? 0 : null },
                     { name: 'train', state: s2, resource: r2,
                       task_id: 't.' + id + '.train' } ] };
}

const DETAIL = {
  campaign_id: 'camp.001', name: 'vacancy-classifier', state: 'RUNNING',
  created_at: NOW - 95,
  workflows: [ wf('wf.0', 300, 'DONE', 'local_a', 'DONE', 'local_a'),
               wf('wf.1', 600, 'DONE', 'local_b', 'RUNNING', 'local_b'),
               wf('wf.2', 900, 'RUNNING', 'local_a', 'NEW', null) ],
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
    metrics: { md: md(300), train: train(300) } },
  { id: 'wf.1', params: { temperature: 600 }, metrics: { md: md(600) } },
  { id: 'wf.2', params: { temperature: 900 }, metrics: {} },
] };

const SUMMARY = { campaign_id: 'camp.001', name: 'vacancy-classifier',
                  state: 'RUNNING', created_at: NOW - 95,
                  workflows: DETAIL.workflows };

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
    if (path === 'campaigns/default')         return { campaigns: [SUMMARY] };
    if (path === 'campaign/default/camp.001') return DETAIL;
    if (path === 'results/default/camp.001')  return RESULTS;
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

// --- drive a full refresh --------------------------------------------------
const page = new Page();
await m.init(page, api);

for (const p of ['/broker/federation/resources/default', 'campaigns/default',
                 'campaign/default/camp.001', 'results/default/camp.001']) {
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
check(!/NaN|undefined/.test(res), 'resources HTML contains NaN/undefined');

const camp = page.html('.ac-campaigns-body');
check(camp.includes('vacancy-classifier'), 'campaign name missing');
check(camp.includes('temperature=300') && camp.includes('temperature=900'),
      'workflow params missing');
check(camp.includes('st-done') && camp.includes('st-run')
      && camp.includes('st-wait'), 'stage chip state classes missing');
check((camp.match(/local_a/g) || []).length >= 2,
      'stage chips are not labelled with their resource');
check(camp.includes('<svg'), 'no svg chart rendered');
check((camp.match(/<polyline/g) || []).length >= 3,
      'expected at least 3 polylines (2 md series + 1 train series)');
check(camp.includes('Energy vs step') && camp.includes('Accuracy vs epoch'),
      'chart titles missing');
check(camp.includes('elapsed'), 'campaign elapsed time missing');
check(!/NaN|undefined|\[object Object\]/.test(camp),
      'campaign HTML contains NaN/undefined');

check(timers.length > 0 && timers[timers.length - 1][1] === 2000,
      'poll interval is not 2000 ms while a campaign RUNs: '
      + JSON.stringify(timers));

// on-screen vocabulary: Orbit internals only ever inside attributes
const visible = (res + camp + tmpl).replace(/<[^>]*>/g, ' ');
for (const w of ['pilot', 'broker', 'endpoint', 'dispatcher']) {
  check(!new RegExp(w, 'i').test(visible),
        `forbidden word "${w}" in visible text`);
}

// --- submit round-trip -----------------------------------------------------
await m.init(page, api);          // reloads the example spec into the textarea
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

// --- pure helpers ----------------------------------------------------------
const I = m._internals;
check(JSON.stringify(I.parseSweepValues('300, 600,900')) === '[300,600,900]',
      'parseSweepValues does not parse numbers');
check(JSON.stringify(I.parseSweepValues('a, b ,')) === '["a","b"]',
      'parseSweepValues does not keep strings / drop blanks');
check(I.stateClass('DONE') === 'st-done'
      && I.stateClass('zzz') === 'st-unknown', 'stateClass');
check(I.niceTicks(0, 100, 5).length >= 4, 'niceTicks');
check(I.downsample(Array.from({length: 5000}, (_, i) => [i, i]), 400).length
      <= 402, 'downsample');
check(I.renderPlot({stage: 'md', title: 't', xlabel: 'x', ylabel: 'y'}, [])
       .includes('waiting for'), 'empty plot has no placeholder');
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
def test_ui_module_matches_the_bundled_example_spec():

    # the dropdown's `vacancy-classifier` entry and examples/
    # workflow_vacancy.json are the same spec written twice (the module
    # cannot read a file at load time); keep them in step
    import json

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ex   = os.path.join(root, 'examples', 'workflow_vacancy.json')

    if not os.path.exists(ex):
        pytest.skip('examples/workflow_vacancy.json not present')

    with open(ex, encoding='utf-8') as fin:
        want = json.load(fin)

    src = open(ui_module_path(), encoding='utf-8').read()

    # a cheap structural comparison: every stage name, every command token
    # and the workflow name must appear in the module source
    assert "name  : '%s'" % want['name'] in src
    for stage in want['stages']:
        assert "name: '%s'" % stage['name'] in src
        for token in stage['cmd']:
            assert "'%s'" % token in src


# ---------------------------------------------------------------------------
@needs_node
def test_ui_module_parses():

    out = subprocess.run([NODE, '--check', ui_module_path()],
                         capture_output=True, text=True)

    assert out.returncode == 0, out.stderr


# ---------------------------------------------------------------------------
@needs_node
def test_ui_module_drives_a_fake_explorer(tmp_path):

    harness = tmp_path / 'harness.mjs'
    harness.write_text(HARNESS_JS, encoding='utf-8')

    out = subprocess.run([NODE, str(harness), ui_module_path()],
                         capture_output=True, text=True, timeout=60)

    assert out.returncode == 0, (out.stdout + '\n' + out.stderr)
    assert 'OK' in out.stdout

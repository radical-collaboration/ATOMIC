"""Unit tests for the pure helpers in ``demo/local/smoke.py``.

No broker, no network, no ``requests`` -- the helpers are fed canned
payloads shaped like the contract in ``plans/00-overview.md``.  This is
what lets the smoke test be written (and its assertions trusted) before
the plugins it talks to exist.

    python -m pytest demo/local/test_smoke_helpers.py -q

The repo's own ``pytest`` run (``testpaths = ["tests"]``) does not pick
this file up: it belongs to the demo harness, not to the package.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import smoke                                                     # noqa: E402


# ---------------------------------------------------------------------------
# canned payloads
# ---------------------------------------------------------------------------

SPEC = {
    'name': 'vacancy-classifier',
    'params': {'temperature': 300},
    'stages': [
        {'name': 'md', 'type': 'simulation',
         'cmd': ['atomic-fake-md', '--temperature', '{temperature}'],
         'requirements': {'cores': 2, 'software': ['lammps']},
         'inputs': [], 'outputs': ['md.json']},
        {'name': 'train', 'type': 'ml_training',
         'cmd': ['atomic-fake-train', '--in', 'md.json'],
         'requirements': {'cores': 2, 'gpus': 0, 'software': ['pytorch']},
         'inputs': ['md.json'], 'outputs': ['model.json']},
    ],
}

RESOURCES = [
    {'name': 'local_a', 'mode': 'allocation',
     'capabilities': {'cores': 4, 'gpus': 0, 'software': ['lammps']},
     'usage': {'pilots_active': 1}},
    {'name': 'local_b', 'mode': 'allocation',
     'capabilities': {'cores': 4, 'gpus': 1,
                      'software': ['lammps', 'pytorch']},
     'usage': {'pilots_active': 1}},
    {'name': 'local_c', 'mode': 'login',
     'capabilities': {'cores': 2, 'gpus': 0, 'software': ['pytorch']},
     'usage': {'pilots_active': 0}},
]

TEMPS = [300, 600, 900]
ACC = {300: 0.9901, 600: 0.9302, 900: 0.8503}


def _campaign(state='DONE', wf_state='DONE', stage_state='DONE',
              md_on='local_a', train_on='local_c'):
    """A campaign record with three workflows of two stages each."""

    workflows = []

    for idx, temp in enumerate(TEMPS):
        workflows.append({
            'id': 'wf-%d' % idx,
            'params': {'temperature': temp},
            'state': wf_state,
            'stages': [
                {'name': 'md', 'state': stage_state, 'resource': md_on,
                 'task_id': 'c1-wf-%d-md' % idx, 'exit_code': 0},
                {'name': 'train', 'state': stage_state, 'resource': train_on,
                 'task_id': 'c1-wf-%d-train' % idx, 'exit_code': 0},
            ],
        })

    return {'campaign_id': 'c1', 'state': state, 'workflows': workflows}


def _results(accuracies=None):

    accuracies = accuracies or ACC
    workflows = []

    for idx, temp in enumerate(TEMPS):
        workflows.append({
            'id': 'wf-%d' % idx,
            'params': {'temperature': temp},
            'state': 'DONE',
            'metrics': {
                'md': {'type': 'simulation', 'summary': {'steps': 200}},
                'train': {'type': 'ml_training',
                          'summary': {'final_accuracy': accuracies[temp]}},
            },
        })

    return {'workflows': workflows}


# ---------------------------------------------------------------------------
# sweep parsing
# ---------------------------------------------------------------------------

def test_parse_sweep_default():

    assert smoke.parse_sweep(smoke.DEFAULT_SWEEP) \
        == {'temperature': [300, 600, 900]}


def test_parse_sweep_floats_and_several_keys():

    got = smoke.parse_sweep('temperature=300,600 ; pressure=0.5,1')

    assert got == {'temperature': [300, 600], 'pressure': [0.5, 1]}


def test_parse_sweep_keeps_non_numeric_values():

    assert smoke.parse_sweep('model=a,b') == {'model': ['a', 'b']}


@pytest.mark.parametrize('text', ['', 'temperature', 'temperature=', '=300'])
def test_parse_sweep_rejects_junk(text):

    with pytest.raises(smoke.SmokeError):
        smoke.parse_sweep(text)


# ---------------------------------------------------------------------------
# spec reading
# ---------------------------------------------------------------------------

def test_stage_requirements_and_outputs():

    assert smoke.stage_requirements(SPEC) == {'md': ['lammps'],
                                              'train': ['pytorch']}
    assert smoke.stage_outputs(SPEC) == {'md': ['md.json'],
                                         'train': ['model.json']}


def test_load_spec_reads_the_repo_example():
    """The default spec must exist and parse -- smoke.py submits it."""

    spec = smoke.load_spec(smoke.DEFAULT_SPEC)

    assert [s['name'] for s in spec['stages']] == ['md', 'train']
    assert smoke.stage_requirements(spec)['md'] == ['lammps']
    assert smoke.stage_requirements(spec)['train'] == ['pytorch']


def test_load_spec_errors(tmp_path):

    with pytest.raises(smoke.SmokeError):
        smoke.load_spec(str(tmp_path / 'nope.json'))

    bad = tmp_path / 'bad.json'
    bad.write_text('{not json', encoding='utf-8')
    with pytest.raises(smoke.SmokeError):
        smoke.load_spec(str(bad))

    empty = tmp_path / 'empty.json'
    empty.write_text('{}', encoding='utf-8')
    with pytest.raises(smoke.SmokeError):
        smoke.load_spec(str(empty))


# ---------------------------------------------------------------------------
# accessors
# ---------------------------------------------------------------------------

def test_software_by_resource():

    assert smoke.software_by_resource(RESOURCES) == {
        'local_a': ['lammps'],
        'local_b': ['lammps', 'pytorch'],
        'local_c': ['pytorch'],
    }


def test_software_by_resource_survives_junk():

    assert smoke.software_by_resource([None, {}, {'name': 'x'}]) == {'x': []}


def test_placements_flattens_the_campaign():

    places = smoke.placements_of(_campaign())

    assert len(places) == 6
    assert {p.stage for p in places} == {'md', 'train'}
    assert {p.resource for p in places} == {'local_a', 'local_c'}
    assert places[0].params['temperature'] == 300


def test_state_of_is_case_insensitive_and_safe():

    assert smoke.state_of({'state': 'done'}) == 'DONE'
    assert smoke.state_of({'status': 'Failed'}) == 'FAILED'
    assert smoke.state_of(None) == ''


def test_progress_line():

    line = smoke.progress_of(_campaign(stage_state='RUNNING'))

    assert line == 'RUNNING 6/6'
    assert smoke.progress_of({}) == 'no stages yet'


# ---------------------------------------------------------------------------
# the checks -- happy path
# ---------------------------------------------------------------------------

def test_all_checks_pass_on_a_healthy_run(tmp_path):

    campaign = _campaign()
    results = _results()
    software = smoke.software_by_resource(RESOURCES)
    reqs = smoke.stage_requirements(SPEC)
    outs = smoke.stage_outputs(SPEC)
    root = _make_store(tmp_path, campaign, outs)

    assert smoke.check_campaign_done(campaign) == []
    assert smoke.check_workflows(campaign, 3, ['md', 'train']) == []
    assert smoke.check_placement(smoke.placements_of(campaign),
                                 software, reqs) == []
    assert smoke.check_accuracy(results, 'train', 3) == []
    assert smoke.check_store(root, 'c1', campaign, outs) == []


# ---------------------------------------------------------------------------
# the checks -- each failure mode
# ---------------------------------------------------------------------------

def test_campaign_not_done_is_reported():

    fails = smoke.check_campaign_done(_campaign(state='FAILED'))

    assert len(fails) == 1
    assert 'FAILED' in fails[0]

    assert smoke.check_campaign_done({}) == \
        ['campaign state is \'<missing>\', expected \'DONE\'']


def test_wrong_workflow_count_and_state():

    campaign = _campaign(wf_state='FAILED')
    campaign['workflows'][0]['error'] = 'stage md failed'

    fails = smoke.check_workflows(campaign, 4, ['md', 'train'])

    assert any('4' in f and 'workflows' in f for f in fails)
    assert any('stage md failed' in f for f in fails)


def test_missing_stage_is_reported():

    campaign = _campaign()
    campaign['workflows'][1]['stages'] = campaign['workflows'][1]['stages'][:1]

    fails = smoke.check_workflows(campaign, 3, ['md', 'train'])

    assert any("no stage 'train'" in f for f in fails)


def test_stage_on_a_resource_without_the_software():
    """`train` on local_a (lammps only) must be caught."""

    campaign = _campaign(md_on='local_b', train_on='local_a')
    fails = smoke.check_placement(smoke.placements_of(campaign),
                                  smoke.software_by_resource(RESOURCES),
                                  smoke.stage_requirements(SPEC))

    # one per workflow, and nothing else (two resources were used)
    assert len(fails) == 3
    assert all('pytorch' in f and 'local_a' in f for f in fails)


def test_single_resource_run_is_caught():

    campaign = _campaign(md_on='local_b', train_on='local_b')
    fails = smoke.check_placement(smoke.placements_of(campaign),
                                  smoke.software_by_resource(RESOURCES),
                                  smoke.stage_requirements(SPEC))

    assert len(fails) == 1
    assert 'at least 2' in fails[0]


def test_unplaced_stage_is_caught():

    campaign = _campaign()
    campaign['workflows'][0]['stages'][0].pop('resource')

    fails = smoke.check_placement(smoke.placements_of(campaign),
                                  smoke.software_by_resource(RESOURCES),
                                  smoke.stage_requirements(SPEC))

    assert any('no resource recorded' in f for f in fails)


def test_unknown_resource_is_caught():

    campaign = _campaign(md_on='surprise')
    fails = smoke.check_placement(smoke.placements_of(campaign),
                                  smoke.software_by_resource(RESOURCES),
                                  smoke.stage_requirements(SPEC))

    assert any('does not list' in f for f in fails)


def test_accuracy_must_strictly_decrease():

    fails = smoke.check_accuracy(_results({300: 0.90, 600: 0.93, 900: 0.85}),
                                 'train', 3)

    assert len(fails) == 1
    assert 'not strictly decreasing' in fails[0]


def test_equal_accuracies_are_not_strictly_decreasing():

    fails = smoke.check_accuracy(_results({300: 0.9, 600: 0.9, 900: 0.85}),
                                 'train', 3)

    assert len(fails) == 1


def test_accuracy_sorted_by_temperature_not_by_listing_order():
    """A results list in a shuffled order still compares 300 > 600 > 900."""

    results = _results()
    results['workflows'].reverse()

    assert smoke.check_accuracy(results, 'train', 3) == []


def test_missing_accuracy_is_reported():

    results = _results()
    results['workflows'][2]['metrics']['train'] = {'summary': {}}

    fails = smoke.check_accuracy(results, 'train', 3)

    assert len(fails) == 1
    assert 'final_accuracy' in fails[0]


def test_final_accuracy_top_level_also_accepted():

    assert smoke.final_accuracy_of({'final_accuracy': 0.5}) == 0.5
    assert smoke.final_accuracy_of({'summary': {'final_accuracy': 1}}) == 1.0
    assert smoke.final_accuracy_of({'summary': {}}) is None
    assert smoke.final_accuracy_of(None) is None


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

def _make_store(tmp_path, campaign, outputs, skip=()):
    """Build the store layout the campaign plugin is supposed to write."""

    root = str(tmp_path / 'store')

    for wf in campaign['workflows']:
        for stage in wf['stages']:
            sdir = os.path.join(root, 'c1', wf['id'], stage['name'])
            os.makedirs(sdir, exist_ok=True)
            files = list(outputs.get(stage['name'], [])) + ['manifest.json']
            for fname in files:
                if (wf['id'], stage['name'], fname) in skip:
                    continue
                with open(os.path.join(sdir, fname), 'w',
                          encoding='utf-8') as fout:
                    json.dump({'file': fname}, fout)

    return root


def test_store_counts_six_outputs_and_six_manifests(tmp_path):

    campaign = _campaign()
    outs = smoke.stage_outputs(SPEC)
    root = _make_store(tmp_path, campaign, outs)

    files = []
    for dirpath, _, names in os.walk(os.path.join(root, 'c1')):
        files += [os.path.join(dirpath, n) for n in names]

    assert len([f for f in files if not f.endswith('manifest.json')]) == 6
    assert len([f for f in files if f.endswith('manifest.json')]) == 6
    assert smoke.check_store(root, 'c1', campaign, outs) == []


def test_store_missing_output_is_reported(tmp_path):

    campaign = _campaign()
    outs = smoke.stage_outputs(SPEC)
    root = _make_store(tmp_path, campaign, outs,
                       skip=[('wf-1', 'train', 'model.json')])

    fails = smoke.check_store(root, 'c1', campaign, outs)

    assert len(fails) == 1
    assert 'model.json' in fails[0]


def test_store_missing_manifest_is_reported(tmp_path):

    campaign = _campaign()
    outs = smoke.stage_outputs(SPEC)
    root = _make_store(tmp_path, campaign, outs,
                       skip=[('wf-0', 'md', 'manifest.json')])

    fails = smoke.check_store(root, 'c1', campaign, outs)

    assert len(fails) == 1
    assert 'manifest.json' in fails[0]


def test_store_corrupt_json_is_reported(tmp_path):

    campaign = _campaign()
    outs = smoke.stage_outputs(SPEC)
    root = _make_store(tmp_path, campaign, outs)

    path = os.path.join(root, 'c1', 'wf-0', 'md', 'md.json')
    with open(path, 'w', encoding='utf-8') as fout:
        fout.write('{ truncated')

    fails = smoke.check_store(root, 'c1', campaign, outs)

    assert len(fails) == 1
    assert 'not readable JSON' in fails[0]


def test_store_without_campaign_dir(tmp_path):

    fails = smoke.check_store(str(tmp_path), 'c1', _campaign(),
                              smoke.stage_outputs(SPEC))

    assert len(fails) == 1
    assert 'no campaign directory' in fails[0]


# ---------------------------------------------------------------------------
# rendering + argument parsing
# ---------------------------------------------------------------------------

def test_placement_table_lines_up():

    text = smoke.render_placements(smoke.placements_of(_campaign()))
    lines = text.splitlines()

    assert lines[0].split() == ['workflow', 'temperature', 'stage', 'state',
                                'resource', 'task']
    assert len(lines) == 8                       # header + rule + 6 stages
    assert 'local_a' in lines[2]


def test_argument_defaults():

    args = smoke.build_parser().parse_args([])

    assert args.spec == smoke.DEFAULT_SPEC
    assert args.sweep == smoke.DEFAULT_SWEEP
    assert args.timeout == 600.0
    assert args.min_resources == 2
    assert args.broker is None


def test_arguments_can_be_overridden():

    args = smoke.build_parser().parse_args(
        ['--broker', 'https://h:1', '--sweep', 'temperature=1,2',
         '--timeout', '30', '--no-store-check', '--min-resources', '3'])

    assert args.broker == 'https://h:1'
    assert args.timeout == 30.0
    assert args.min_resources == 3
    assert args.no_store_check is True


# ---------------------------------------------------------------------------
# env.sh fallback
# ---------------------------------------------------------------------------

def test_demo_env_is_read_back_from_env_sh(monkeypatch):
    """`up.sh` exports into its own subshell, so smoke.py reads env.sh."""

    for key in smoke.DEMO_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    added = smoke.load_demo_env()

    assert 'RADICAL_ORBIT_BROKER_URL' in added
    assert os.environ['RADICAL_ORBIT_BROKER_URL'].startswith('https://')
    assert os.environ['ATOMIC_STORE_ROOT']


def test_demo_env_does_not_override_the_caller(monkeypatch):

    monkeypatch.setenv('RADICAL_ORBIT_BROKER_URL', 'https://elsewhere:9999')

    assert smoke.load_demo_env() == []
    assert os.environ['RADICAL_ORBIT_BROKER_URL'] == 'https://elsewhere:9999'


def test_demo_env_survives_a_missing_env_sh(monkeypatch, tmp_path):

    monkeypatch.delenv('RADICAL_ORBIT_BROKER_URL', raising=False)

    assert smoke.load_demo_env(str(tmp_path / 'nope.sh')) == []


# ---------------------------------------------------------------------------
# the wait loop (with a scripted fake client)
# ---------------------------------------------------------------------------

class _FakeClient:

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def campaign(self, cid):
        self.calls += 1
        state = self.states[min(self.calls - 1, len(self.states) - 1)]
        return _campaign(state=state,
                         stage_state='DONE' if state == 'DONE' else 'RUNNING')


def test_wait_returns_on_a_terminal_state():

    client = _FakeClient(['RUNNING', 'RUNNING', 'DONE'])
    campaign = smoke.wait_for_campaign(client, 'c1', timeout=10, poll=0)

    assert smoke.state_of(campaign) == 'DONE'
    assert client.calls == 3


def test_wait_returns_on_failure_too():
    """A FAILED campaign must end the wait -- the checks report it."""

    client = _FakeClient(['RUNNING', 'FAILED'])

    assert smoke.state_of(
        smoke.wait_for_campaign(client, 'c1', timeout=10, poll=0)) == 'FAILED'


def test_wait_times_out_with_a_useful_message():

    client = _FakeClient(['RUNNING'])

    with pytest.raises(smoke.SmokeError) as excinfo:
        smoke.wait_for_campaign(client, 'c1', timeout=0, poll=0)

    assert 'RUNNING' in str(excinfo.value)
    assert 'c1' in str(excinfo.value)

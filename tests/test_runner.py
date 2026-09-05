"""Unit tests for the campaign runner, store and state persistence.

The federation is faked: :class:`FakeFederation` implements the whole
``FederationAPI`` surface with scripted task-state transitions and lets a
test decide *where* an output can be fetched from (pilot staging /
dispatcher stage_out / the broker's local filesystem), so the collection
fallback order is exercised for real.
"""

import asyncio
import json

from pathlib import Path

import pytest

from atomic_wm.campaign.planner import SweepPlanner
from atomic_wm.campaign.runner  import (CampaignRunner, FederationAPI,
                                        FederationUnavailable, StageRunner)
from atomic_wm.campaign.state   import (Campaign, load_campaigns,
                                        save_campaigns)
from atomic_wm.campaign.store   import ResultStore


# ---------------------------------------------------------------------------
def _spec(stages=None):
    return {
        'name'  : 'demo',
        'params': {},
        'stages': stages or [
            {'name': 'md', 'type': 'simulation',
             'cmd': ['atomic-fake-md', '--temperature', '{temperature}',
                     '--out', 'md.json'],
             'requirements': {'cores': 2}, 'inputs': [],
             'outputs': ['md.json']},
            {'name': 'train', 'type': 'ml_training',
             'cmd': ['atomic-fake-train', '--in', 'md.json',
                     '--out', 'model.json'],
             'requirements': {'cores': 2}, 'inputs': ['md.json'],
             'outputs': ['model.json']},
        ],
    }


def _campaign(tmp_path, sweep=None, stages=None, cid='cmp-test'):
    wfs = SweepPlanner().expand(_spec(stages), sweep or {'temperature': [300]})
    return Campaign(campaign_id=cid, name='demo', sweep=sweep or {},
                    workflows=wfs)


def _envelope(kind, value):
    return json.dumps({'type': kind, 'series': {'x': [1, 2]},
                       'summary': {'v': value}}).encode()


# ---------------------------------------------------------------------------
class FakeFederation(FederationAPI):
    """A scripted federation: submits, polls, staging, dispatcher staging."""

    def __init__(self, tmp_path, *, plans=None, unavailable=False):
        self.root        = Path(tmp_path) / 'scratch'
        self.plans       = plans or {}        # stage name -> plan dict
        self.unavailable = unavailable

        self.submits    = []                  # [(task, requirements)]
        self.stage_ins  = []                  # [(sid, pool, task, name, data)]
        self.puts       = []                  # [(endpoint, path, len)]
        self.cancels    = []                  # [(sid, task_id)]
        self.polls      = {}                  # task_id -> n polls

        self._pilot     = {}                  # (endpoint, path) -> bytes
        self._scratch   = {}                  # (task_id, name)   -> bytes
        self._stage      = {}                 # task_id -> stage name
        self._cwd        = {}                 # task_id -> cwd
        self.in_flight   = 0
        self.max_in_flight = 0

    # -- plan helpers -----------------------------------------------------
    def plan(self, stage):
        return self.plans.get(stage, {})

    def _stage_of(self, task_id):
        return self._stage.get(task_id, '')

    # -- FederationAPI ----------------------------------------------------
    async def submit(self, task, requirements):
        if self.unavailable:
            raise FederationUnavailable(FederationUnavailable.DEFAULT)
        task_id = task['task_id']
        stage   = task_id.rsplit('-', 1)[-1]
        plan    = self.plan(stage)
        if plan.get('submit_error'):
            raise RuntimeError(plan['submit_error'])

        self._stage[task_id] = stage
        cwd = self.root / task_id
        cwd.mkdir(parents=True, exist_ok=True)
        self._cwd[task_id] = str(cwd)

        self.submits.append((task, requirements))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)

        # publish this stage's outputs through the configured channel
        for name, data in (plan.get('outputs') or {}).items():
            channel = plan.get('channel', 'stage_out')
            if channel == 'pilot':
                endpoint = plan.get('child_endpoint', 'fed-a_p1')
                self._pilot[(endpoint, str(cwd / name))] = data
            elif channel == 'stage_out':
                self._scratch[(task_id, name)] = data
            elif channel == 'local':
                (cwd / name).write_bytes(data)

        return {'task': dict(task, cwd=str(cwd), state='QUEUED'),
                'resource': plan.get('resource', 'res-a'),
                'pool': plan.get('pool', 'fed-a'),
                'dispatcher_sid': plan.get('dispatcher_sid', 'sid-a')}

    async def task(self, task_id):
        if self.unavailable:
            raise FederationUnavailable(FederationUnavailable.DEFAULT)
        n     = self.polls.get(task_id, 0)
        self.polls[task_id] = n + 1
        plan  = self.plan(self._stage_of(task_id))
        polls = plan.get('polls') or [{'state': 'DONE', 'exit_code': 0}]
        entry = dict(polls[min(n, len(polls) - 1)])
        entry.setdefault('cwd', self._cwd.get(task_id))
        entry.setdefault('resource', plan.get('resource', 'res-a'))
        if entry.get('state') in ('DONE', 'FAILED', 'CANCELED'):
            self.in_flight = max(0, self.in_flight - 1)
        return entry

    async def resources(self):
        return [{'name': 'res-a'}, {'name': 'res-b'}]

    async def stage_in(self, dispatcher_sid, pool, task_id, filename, data):
        self.stage_ins.append((dispatcher_sid, pool, task_id, filename, data))
        cwd = Path(self._cwd.get(task_id, self.root / task_id))
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / filename).write_bytes(data)
        return {'cwd': str(cwd), 'size': len(data)}

    async def stage_out(self, dispatcher_sid, task_id, filename):
        return self._scratch.get((task_id, filename))

    async def staging_get(self, endpoint, path):
        return self._pilot.get((endpoint, path))

    async def staging_put(self, endpoint, path, data):
        self.puts.append((endpoint, path, len(data)))
        self._pilot[(endpoint, path)] = data
        return True

    async def cancel_task(self, dispatcher_sid, task_id):
        self.cancels.append((dispatcher_sid, task_id))
        return True


# ---------------------------------------------------------------------------
def _run(fed, campaign, store, **kwargs):
    """Run a campaign to its end with instant polls."""

    kwargs.setdefault('poll_interval', 0)
    kwargs.setdefault('poll_max_interval', 0)
    runner = CampaignRunner(fed, store, **kwargs)
    asyncio.run(runner.run(campaign))
    return runner


def _store(tmp_path):
    return ResultStore(Path(tmp_path) / 'store')


# ---------------------------------------------------------------------------
class TestHappyPath:

    def test_two_stages_run_in_order_and_land_in_the_store(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md'   : {'outputs': {'md.json': _envelope('simulation', 1)},
                      'channel': 'stage_out', 'resource': 'res-a'},
            'train': {'outputs': {'model.json': _envelope('ml_training', 2)},
                      'channel': 'stage_out', 'resource': 'res-b'}})
        camp  = _campaign(tmp_path)
        store = _store(tmp_path)
        _run(fed, camp, store)

        assert camp.state == 'DONE'
        wf = camp.workflows[0]
        assert wf.state == 'DONE'
        assert [s.state for s in wf.stages] == ['DONE', 'DONE']
        # sequential: md submitted before train
        assert [t['task_id'] for t, _ in fed.submits] == \
               ['cmp-test-wf-000-md', 'cmp-test-wf-000-train']
        # per-stage resources are recorded
        assert [s.resource for s in wf.stages] == ['res-a', 'res-b']
        # store layout: <cid>/<wf>/<stage>/<file>
        base = store.root / 'cmp-test' / 'wf-000'
        assert (base / 'md' / 'md.json').is_file()
        assert (base / 'train' / 'model.json').is_file()
        assert (base / 'md' / 'manifest.json').is_file()

    def test_outputs_of_stage_k_are_staged_into_stage_k_plus_1(self,
                                                               tmp_path):
        payload = _envelope('simulation', 7)
        fed = FakeFederation(tmp_path, plans={
            'md'   : {'outputs': {'md.json': payload}},
            'train': {'outputs': {'model.json': _envelope('ml_training', 8)}}})
        camp = _campaign(tmp_path)
        _run(fed, camp, _store(tmp_path))

        assert len(fed.stage_ins) == 1
        sid, pool, task_id, name, data = fed.stage_ins[0]
        assert (sid, pool, name) == ('sid-a', 'fed-a', 'md.json')
        assert task_id == 'cmp-test-wf-000-train'
        assert data    == payload

    def test_manifest_records_provenance(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': _envelope('simulation', 1)},
                   'resource': 'res-a', 'channel': 'stage_out'}})
        camp  = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        store = _store(tmp_path)
        _run(fed, camp, store)

        path = store.root / 'cmp-test' / 'wf-000' / 'md' / 'manifest.json'
        man  = json.loads(path.read_text())
        assert man['stage']       == 'md'
        assert man['resource']    == 'res-a'
        assert man['task_id']     == 'cmp-test-wf-000-md'
        assert man['state']       == 'DONE'
        assert man['exit_code']   == 0
        assert man['params']      == {'temperature': 300}
        assert man['outputs'][0]['name'] == 'md.json'
        assert man['outputs'][0]['via']  == 'dispatcher_stage_out'
        assert man['cmd'][0] == 'atomic-fake-md'

    def test_results_parse_every_collected_json(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md'   : {'outputs': {'md.json': _envelope('simulation', 1)}},
            'train': {'outputs': {'model.json': _envelope('ml_training', 2)}}})
        camp  = _campaign(tmp_path, sweep={'temperature': [300, 600]})
        store = _store(tmp_path)
        _run(fed, camp, store)

        res = store.results(camp)
        assert len(res['workflows']) == 2
        first = res['workflows'][0]
        assert first['metrics']['md']['type']       == 'simulation'
        assert first['metrics']['train']['type']    == 'ml_training'
        assert first['files']['md'][0]['name']      == 'md.json'
        assert first['files']['md'][0]['json'] is True

    def test_non_json_outputs_are_listed_by_name_and_size(self, tmp_path):
        stages = [{'name': 'md', 'cmd': ['atomic-fake-md'],
                   'inputs': [], 'outputs': ['md.log']}]
        fed = FakeFederation(tmp_path,
                             plans={'md': {'outputs': {'md.log': b'hello'}}})
        camp  = _campaign(tmp_path, stages=stages)
        store = _store(tmp_path)
        _run(fed, camp, store)

        res = store.results(camp)['workflows'][0]
        assert res['metrics'] == {}
        assert res['files']['md'] == [{'name': 'md.log', 'size': 5,
                                       'json': False}]


# ---------------------------------------------------------------------------
class TestCollectionFallback:

    def _one_stage(self, tmp_path, channel, extra=None):
        plan = {'outputs': {'md.json': _envelope('simulation', 1)},
                'channel': channel,
                'polls'  : [{'state': 'RUNNING',
                             'child_endpoint': 'fed-a_p1'},
                            {'state': 'DONE', 'exit_code': 0,
                             'child_endpoint': 'fed-a_p1'}]}
        plan.update(extra or {})
        fed   = FakeFederation(tmp_path, plans={'md': plan})
        camp  = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        store = _store(tmp_path)
        _run(fed, camp, store)
        return camp.workflows[0].stages[0], fed, store

    def test_pilot_staging_is_tried_first(self, tmp_path):
        stage, _, _ = self._one_stage(tmp_path, 'pilot')
        assert stage.state == 'DONE'
        assert stage.outputs[0]['via'] == 'pilot_staging'
        assert stage.child_endpoint    == 'fed-a_p1'

    def test_dispatcher_stage_out_is_the_second_choice(self, tmp_path):
        stage, _, _ = self._one_stage(tmp_path, 'stage_out')
        assert stage.outputs[0]['via'] == 'dispatcher_stage_out'
        assert 'pilot_staging' in stage.outputs[0]['errors']

    def test_local_read_is_the_last_resort(self, tmp_path):
        stage, _, _ = self._one_stage(tmp_path, 'local')
        assert stage.outputs[0]['via'] == 'broker_local'

    def test_uncollectable_output_fails_the_stage(self, tmp_path):
        stage, _, _ = self._one_stage(tmp_path, 'none')
        assert stage.state == 'FAILED'
        assert 'not collected' in (stage.reason or '')

    def test_child_endpoint_is_derived_from_pool_and_pilot_id(self, tmp_path):
        plan = {'outputs': {'md.json': _envelope('simulation', 1)},
                'channel': 'pilot', 'child_endpoint': 'fed-a_p7',
                'polls'  : [{'state': 'DONE', 'exit_code': 0,
                             'pilot_id': 'p7'}]}
        fed   = FakeFederation(tmp_path, plans={'md': plan})
        camp  = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))
        stage = camp.workflows[0].stages[0]
        assert stage.child_endpoint    == 'fed-a_p7'
        assert stage.outputs[0]['via'] == 'pilot_staging'


# ---------------------------------------------------------------------------
class TestInputPush:

    def test_inputs_are_pushed_to_the_pilot_best_effort(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md'   : {'outputs': {'md.json': _envelope('simulation', 1)}},
            'train': {'outputs': {'model.json': _envelope('ml_training', 2)},
                      'polls': [{'state': 'RUNNING',
                                 'child_endpoint': 'fed-a_p1'},
                                {'state': 'DONE', 'exit_code': 0,
                                 'child_endpoint': 'fed-a_p1'}]}})
        camp = _campaign(tmp_path)
        _run(fed, camp, _store(tmp_path))

        assert len(fed.puts) == 1
        endpoint, path, size = fed.puts[0]
        assert endpoint == 'fed-a_p1'
        assert path.endswith('cmp-test-wf-000-train/md.json')
        assert size > 0

    def test_a_failing_push_does_not_fail_the_stage(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md'   : {'outputs': {'md.json': _envelope('simulation', 1)}},
            'train': {'outputs': {'model.json': _envelope('ml_training', 2)},
                      'polls': [{'state': 'DONE', 'exit_code': 0,
                                 'child_endpoint': 'fed-a_p1'}]}})

        async def _boom(endpoint, path, data):
            raise RuntimeError('no route to pilot')
        fed.staging_put = _boom                        # type: ignore[method-assign]

        camp = _campaign(tmp_path)
        _run(fed, camp, _store(tmp_path))
        assert camp.state == 'DONE'


# ---------------------------------------------------------------------------
class TestFailures:

    def test_a_failed_task_fails_only_its_workflow(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md'   : {'outputs': {'md.json': _envelope('simulation', 1)}},
            'train': {'outputs': {'model.json': _envelope('ml_training', 2)}}})

        # wf-001's md task fails
        real_task = fed.task

        async def _task(task_id):
            out = await real_task(task_id)
            if task_id.startswith('cmp-test-wf-001-md') \
                    and out.get('state') == 'DONE':
                return dict(out, state='FAILED', exit_code=3,
                            error='boom')
            return out
        fed.task = _task                               # type: ignore[method-assign]

        camp = _campaign(tmp_path, sweep={'temperature': [300, 600, 900]})
        _run(fed, camp, _store(tmp_path))

        states = [wf.state for wf in camp.workflows]
        assert states == ['DONE', 'FAILED', 'DONE']
        bad = camp.workflows[1]
        assert bad.stages[0].state == 'FAILED'
        assert bad.stages[0].exit_code == 3
        assert bad.stages[1].state == 'SKIPPED'
        assert 'boom' in (bad.reason or '')
        assert camp.state == 'FAILED'

    def test_nonzero_exit_code_is_a_failure(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': _envelope('simulation', 1)},
                   'polls': [{'state': 'DONE', 'exit_code': 1}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))
        assert camp.workflows[0].stages[0].state == 'FAILED'
        assert camp.state == 'FAILED'

    def test_submit_error_fails_the_workflow_only(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'submit_error': 'no resource satisfies requirements'}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]],
                         sweep={'temperature': [300, 600]})
        _run(fed, camp, _store(tmp_path))
        assert all(wf.state == 'FAILED' for wf in camp.workflows)
        assert 'no resource' in (camp.workflows[0].reason or '')

    def test_missing_federation_fails_the_campaign_with_a_clear_reason(
            self, tmp_path):
        fed  = FakeFederation(tmp_path, unavailable=True)
        camp = _campaign(tmp_path, sweep={'temperature': [300, 600]})
        _run(fed, camp, _store(tmp_path))
        assert camp.state  == 'FAILED'
        assert camp.reason == 'the resource federation is not available'
        assert all(wf.state == 'FAILED' for wf in camp.workflows)

    def test_stage_times_out_on_task_state(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': _envelope('simulation', 1)},
                   'polls': [{'state': 'RUNNING'}]}})   # never terminal
        camp  = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        store = _store(tmp_path)

        ticks = iter(range(0, 10000, 7))

        _run(fed, camp, store, stage_timeout=20,
             clock=lambda: float(next(ticks)))

        stage = camp.workflows[0].stages[0]
        assert stage.state == 'FAILED'
        assert 'timed out' in (stage.reason or '')
        assert fed.cancels == [('sid-a', 'cmp-test-wf-000-md')]

    def test_a_missing_input_fails_the_stage(self, tmp_path):
        # md declares no output, so train's input can never be produced
        stages = [{'name': 'md', 'cmd': ['x'], 'inputs': [], 'outputs': []},
                  {'name': 'train', 'cmd': ['y'], 'inputs': ['md.json'],
                   'outputs': ['model.json']}]
        fed  = FakeFederation(tmp_path, plans={
            'train': {'outputs': {'model.json': b'{}'}}})
        camp = _campaign(tmp_path, stages=stages)
        _run(fed, camp, _store(tmp_path))
        train = camp.workflows[0].stages[1]
        assert train.state == 'FAILED'
        assert 'not produced' in (train.reason or '')


# ---------------------------------------------------------------------------
class TestCancel:

    def test_cancel_before_the_run_cancels_every_workflow(self, tmp_path):
        fed    = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'}}})
        camp   = _campaign(tmp_path, stages=[_spec()['stages'][0]],
                           sweep={'temperature': [300, 600]})
        runner = CampaignRunner(fed, _store(tmp_path), poll_interval=0,
                                poll_max_interval=0)
        runner.cancel()
        asyncio.run(runner.run(camp))

        assert camp.state == 'CANCELED'
        assert all(wf.state == 'CANCELED' for wf in camp.workflows)
        assert not fed.submits

    def test_cancel_while_polling_cancels_the_task(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'polls': [{'state': 'RUNNING'}]}})
        camp   = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        runner = CampaignRunner(fed, _store(tmp_path), poll_interval=0,
                                poll_max_interval=0)

        async def _scenario():
            task = asyncio.ensure_future(runner.run(camp))
            for _ in range(50):            # let the stage reach its poll loop
                if fed.submits:
                    break
                await asyncio.sleep(0)
            runner.cancel()
            await task

        asyncio.run(_scenario())
        stage = camp.workflows[0].stages[0]
        assert stage.state == 'CANCELED'
        assert camp.state  == 'CANCELED'
        assert fed.cancels == [('sid-a', 'cmp-test-wf-000-md')]


# ---------------------------------------------------------------------------
class TestConcurrency:

    def test_workflows_run_concurrently_up_to_the_semaphore(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'polls': [{'state': 'RUNNING'},
                             {'state': 'DONE', 'exit_code': 0}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]],
                         sweep={'temperature': [1, 2, 3, 4, 5]})
        _run(fed, camp, _store(tmp_path), max_concurrent_workflows=2)

        assert camp.state == 'DONE'
        assert fed.max_in_flight == 2

    def test_a_single_slot_serialises_the_workflows(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'polls': [{'state': 'RUNNING'},
                             {'state': 'DONE', 'exit_code': 0}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]],
                         sweep={'temperature': [1, 2, 3]})
        _run(fed, camp, _store(tmp_path), max_concurrent_workflows=1)
        assert fed.max_in_flight == 1


# ---------------------------------------------------------------------------
class TestToolPrefix:

    def test_prefix_is_applied_to_bare_atomic_fake_tools(self, tmp_path):
        runner = StageRunner(FakeFederation(tmp_path), _store(tmp_path),
                             tool_prefix='/opt/ve/bin')
        assert runner.resolve_cmd(['atomic-fake-md', '--x']) == \
               ['/opt/ve/bin/atomic-fake-md', '--x']

    def test_absolute_and_foreign_commands_are_left_alone(self, tmp_path):
        runner = StageRunner(FakeFederation(tmp_path), _store(tmp_path),
                             tool_prefix='/opt/ve/bin')
        assert runner.resolve_cmd(['/usr/bin/atomic-fake-md'])[0] == \
               '/usr/bin/atomic-fake-md'
        assert runner.resolve_cmd(['lmp', '-in', 'x'])[0] == 'lmp'

    def test_no_prefix_keeps_the_command(self, tmp_path):
        runner = StageRunner(FakeFederation(tmp_path), _store(tmp_path),
                             tool_prefix=None)
        assert runner.resolve_cmd(['atomic-fake-md'])[0] == 'atomic-fake-md'

    def test_prefix_comes_from_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv('ATOMIC_TOOL_PREFIX', '/env/bin')
        runner = StageRunner(FakeFederation(tmp_path), _store(tmp_path))
        assert runner.resolve_cmd(['atomic-fake-train'])[0] == \
               '/env/bin/atomic-fake-train'

    def test_the_submitted_command_carries_the_prefix(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setenv('ATOMIC_TOOL_PREFIX', '/env/bin')
        fed  = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'}}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))
        assert fed.submits[0][0]['cmd'][0] == '/env/bin/atomic-fake-md'


# ---------------------------------------------------------------------------
class TestPersistence:

    def test_round_trip(self, tmp_path):
        camp = _campaign(tmp_path)
        camp.workflows[0].stages[0].state = 'DONE'
        path = Path(tmp_path) / 'state.json'
        save_campaigns(path, [camp])

        back = load_campaigns(path)
        assert len(back) == 1
        assert back[0].campaign_id == 'cmp-test'
        assert back[0].workflows[0].stages[0].state == 'DONE'
        assert back[0].workflows[0].params == {'temperature': 300}

    def test_running_campaigns_come_back_interrupted(self, tmp_path):
        camp = _campaign(tmp_path)
        camp.state = 'RUNNING'
        path = Path(tmp_path) / 'state.json'
        save_campaigns(path, [camp])

        back = load_campaigns(path)[0]
        assert back.state == 'INTERRUPTED'
        assert back.workflows[0].state  == 'INTERRUPTED'
        assert back.workflows[0].stages[0].state == 'INTERRUPTED'

    def test_terminal_campaigns_survive_unchanged(self, tmp_path):
        camp = _campaign(tmp_path)
        camp.state = 'DONE'
        for wf in camp.workflows:
            wf.state = 'DONE'
        path = Path(tmp_path) / 'state.json'
        save_campaigns(path, [camp])
        assert load_campaigns(path)[0].state == 'DONE'

    def test_a_broken_state_file_is_ignored(self, tmp_path):
        path = Path(tmp_path) / 'state.json'
        path.write_text('{not json')
        assert load_campaigns(path) == []

    def test_missing_state_file_is_empty(self, tmp_path):
        assert load_campaigns(Path(tmp_path) / 'nope.json') == []

    def test_on_change_is_called_while_running(self, tmp_path):
        calls = []
        fed   = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'}}})
        camp  = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path),
             on_change=lambda: calls.append(1))
        assert len(calls) > 3


# ---------------------------------------------------------------------------
class TestStoreLayout:

    def test_layout_lists_campaigns_workflows_stages_and_files(self,
                                                               tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md'   : {'outputs': {'md.json': _envelope('simulation', 1)}},
            'train': {'outputs': {'model.json': _envelope('ml_training', 2)}}})
        camp  = _campaign(tmp_path)
        store = _store(tmp_path)
        _run(fed, camp, store)

        layout = store.layout()
        assert layout['root'] == str(store.root)
        assert len(layout['campaigns']) == 1
        entry = layout['campaigns'][0]
        assert entry['campaign_id'] == 'cmp-test'
        stages = entry['workflows'][0]['stages']
        assert [s['name'] for s in stages] == ['md', 'train']
        names = {f['name'] for f in stages[0]['files']}
        assert names == {'md.json', 'manifest.json'}

    def test_layout_can_be_filtered_to_one_campaign(self, tmp_path):
        store = _store(tmp_path)
        store.write_output('cmp-a', 'wf-000', 'md', 'md.json', b'{}')
        store.write_output('cmp-b', 'wf-000', 'md', 'md.json', b'{}')
        assert len(store.layout()['campaigns']) == 2
        assert len(store.layout('cmp-a')['campaigns']) == 1

    def test_empty_store_is_empty(self, tmp_path):
        assert _store(tmp_path).layout()['campaigns'] == []

    def test_unsafe_path_components_are_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            _store(tmp_path).stage_dir('..', 'wf-000', 'md')

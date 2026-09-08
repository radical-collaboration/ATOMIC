"""Unit tests for the campaign runner, store and state persistence.

The federation is faked: :class:`FakeFederation` implements the whole
``FederationAPI`` surface with scripted task-state transitions and lets a
test decide *where* an output can be fetched from (pilot staging /
dispatcher stage_out / the broker's local filesystem), so the collection
fallback order is exercised for real.
"""

import asyncio
import base64
import json

from pathlib import Path

import pytest

from atomic_wm.campaign.planner import SweepPlanner
from atomic_wm.campaign.runner  import (CampaignRunner, FederationAPI,
                                        FederationCallError,
                                        FederationUnavailable, StageRunner,
                                        TaskNotFound, MAX_POLL_FAILURES,
                                        REASON_NO_RESOURCE, REASON_NO_STATUS,
                                        REASON_STAGE_IN, REASON_STOPPED)
from atomic_wm.campaign.state   import (CANCELED, Campaign, DONE, FAILED,
                                        PENDING, RUNNING, WorkflowInstance,
                                        load_campaigns, save_campaigns)
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
            raise FederationCallError(REASON_NO_RESOURCE,
                                      plan['submit_error'])

        self._stage[task_id] = stage
        cwd = self.root / task_id
        cwd.mkdir(parents=True, exist_ok=True)
        self._cwd[task_id] = str(cwd)

        self.submits.append((task, requirements))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)

        # the dispatcher spools `inputs_b64` and places it with the task;
        # here that means writing it into the task's working directory
        for name, blob in (task.get('inputs_b64') or {}).items():
            (cwd / name).write_bytes(base64.b64decode(blob))

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

        # the submit answer names an ADVISORY resource and no member:
        # the class pool binds one only when it dispatches
        return {'task': dict(task, cwd=str(cwd), state='QUEUED'),
                'resource': plan.get('resource', 'res-a'),
                'member': plan.get('submit_member'),
                'class': plan.get('cls', 'cpu'),
                'pool': plan.get('pool', 'fed-cpu'),
                'dispatcher_sid': plan.get('dispatcher_sid', 'sid-a'),
                'members_eligible': plan.get('members_eligible', [])}

    async def task(self, task_id):
        if self.unavailable:
            raise FederationUnavailable(FederationUnavailable.DEFAULT)
        n     = self.polls.get(task_id, 0)
        self.polls[task_id] = n + 1
        plan  = self.plan(self._stage_of(task_id))
        if plan.get('task_404'):
            raise TaskNotFound('unknown task: %s' % task_id)
        if plan.get('task_error'):
            raise RuntimeError(plan['task_error'])
        polls = plan.get('polls') or [{'state': 'DONE', 'exit_code': 0}]
        entry = dict(polls[min(n, len(polls) - 1)])
        entry.setdefault('cwd', self._cwd.get(task_id))
        if 'resource' not in entry and 'member_id' not in entry:
            entry['resource'] = plan.get('resource', 'res-a')
        if entry.get('state') in ('DONE', 'FAILED', 'CANCELED'):
            self.in_flight = max(0, self.in_flight - 1)
        return entry

    async def resources(self):
        return [{'name': 'res-a'}, {'name': 'res-b'}]

    async def stage_out(self, dispatcher_sid, task_id, filename):
        return self._scratch.get((task_id, filename))

    async def staging_get(self, endpoint, path):
        return self._pilot.get((endpoint, path))

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

    def test_outputs_of_stage_k_ride_in_the_submit_of_stage_k_plus_1(
            self, tmp_path):
        # inputs travel WITH the submit: with a class pool nobody knows
        # where the task will run until the dispatcher places it, so
        # there is no directory to stage into beforehand
        payload = _envelope('simulation', 7)
        fed = FakeFederation(tmp_path, plans={
            'md'   : {'outputs': {'md.json': payload}},
            'train': {'outputs': {'model.json': _envelope('ml_training', 8)}}})
        camp = _campaign(tmp_path)
        _run(fed, camp, _store(tmp_path))

        md_task, train_task = [t for t, _ in fed.submits]
        assert 'inputs_b64' not in md_task            # md declares no input
        assert set(train_task['inputs_b64']) == {'md.json'}
        assert base64.b64decode(train_task['inputs_b64']['md.json']) == payload
        assert train_task['inputs'] == ['md.json']
        # and the runner never names a working directory itself
        assert 'cwd' not in md_task and 'cwd' not in train_task
        assert not hasattr(fed, 'stage_ins')

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
                'pool'   : 'fed-a',
                'polls'  : [{'state': 'DONE', 'exit_code': 0,
                             'pilot_id': 'p7'}]}
        fed   = FakeFederation(tmp_path, plans={'md': plan})
        camp  = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))
        stage = camp.workflows[0].stages[0]
        assert stage.child_endpoint    == 'fed-a_p7'
        assert stage.outputs[0]['via'] == 'pilot_staging'


# ---------------------------------------------------------------------------
class TestClassPoolPlacement:
    """Placement is what the POLL says, never what the submit said."""

    def test_the_poll_overwrites_the_advisory_resource(self, tmp_path):
        # submit answers an advisory 'res-a' and no member; the dispatcher
        # then places the task on res-b's gpu member
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'resource': 'res-a', 'pool': 'fed-gpu', 'cls': 'gpu',
                   'polls': [{'state': 'RUNNING', 'member_id': 'res-b.gpu'},
                             {'state': 'DONE', 'exit_code': 0,
                              'member_id': 'res-b.gpu'}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))

        stage = camp.workflows[0].stages[0]
        assert stage.state     == 'DONE'
        assert stage.resource  == 'res-b'
        assert stage.member    == 'gpu'
        assert stage.member_id == 'res-b.gpu'
        assert stage.cls       == 'gpu'
        assert stage.pool      == 'fed-gpu'

    def test_a_member_id_splits_on_the_last_dot(self, tmp_path):
        # a resource name may carry dots, a member short name may not
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'polls': [{'state': 'DONE', 'exit_code': 0,
                              'member_id': 'site.a.res.gpu'}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))

        stage = camp.workflows[0].stages[0]
        assert (stage.resource, stage.member) == ('site.a.res', 'gpu')

    def test_a_null_resource_does_not_fail_or_wipe_the_placement(self,
                                                                 tmp_path):
        # a task queued in a class pool -- or one whose resource left the
        # federation -- legitimately reports resource: null
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'polls': [{'state': 'QUEUED', 'resource': None},
                             {'state': 'RUNNING', 'member_id': 'res-b.cpu'},
                             {'state': 'QUEUED', 'resource': None,
                              'member_id': None},
                             {'state': 'DONE', 'exit_code': 0,
                              'resource': None}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))

        stage = camp.workflows[0].stages[0]
        assert stage.state    == 'DONE'
        assert stage.resource == 'res-b'          # the last poll that knew
        assert stage.member   == 'cpu'

    def test_the_child_endpoint_fallback_carries_the_member(self, tmp_path):
        # the dispatcher names a class pool's pilot
        # '<pool>_<member_id>_<pid>'; this only matters when the task dict
        # reports no child_endpoint (the pilot finished between two polls)
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'}, 'pool': 'fed-gpu',
                   'polls': [{'state': 'DONE', 'exit_code': 0,
                              'member_id': 'res-b.gpu',
                              'pilot_id': 'p.abc123'}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))

        stage = camp.workflows[0].stages[0]
        assert stage.child_endpoint == 'fed-gpu_res-b.gpu_p.abc123'

    def test_without_a_member_the_fallback_stays_two_part(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'}, 'pool': 'fed-a',
                   'polls': [{'state': 'DONE', 'exit_code': 0,
                              'pilot_id': 'p.abc123'}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))

        assert camp.workflows[0].stages[0].child_endpoint == 'fed-a_p.abc123'

    def test_the_manifest_records_the_member_and_class(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': _envelope('simulation', 1)},
                   'pool': 'fed-gpu', 'cls': 'gpu',
                   'polls': [{'state': 'DONE', 'exit_code': 0,
                              'member_id': 'res-b.gpu'}]}})
        camp  = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        store = _store(tmp_path)
        _run(fed, camp, store)

        man = json.loads((store.root / 'cmp-test' / 'wf-000' / 'md'
                          / 'manifest.json').read_text())
        assert man['resource']  == 'res-b'
        assert man['member']    == 'gpu'
        assert man['member_id'] == 'res-b.gpu'
        assert man['class']     == 'gpu'

    def test_the_input_manifest_says_the_inputs_came_with_the_submit(
            self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md'   : {'outputs': {'md.json': _envelope('simulation', 1)}},
            'train': {'outputs': {'model.json': _envelope('ml_training', 2)}}})
        camp  = _campaign(tmp_path)
        store = _store(tmp_path)
        _run(fed, camp, store)

        man = json.loads((store.root / 'cmp-test' / 'wf-000' / 'train'
                          / 'manifest.json').read_text())
        assert man['inputs_staged'] == [{'name': 'md.json', 'via': 'submit',
                                         'size': man['inputs_staged'][0]
                                                    ['size']}]
        assert man['inputs_staged'][0]['size'] > 0


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
        # the on-screen reason is a fixed phrase; ORBIT's text is in
        # `detail`.  No poll named a member here, so the resource on the
        # record is still the advisory one and is NOT blamed by name
        assert bad.stages[0].reason == 'the stage failed'
        assert 'boom' in (bad.stages[0].detail or '')
        assert 'boom' not in (bad.reason or '')
        assert camp.state == 'FAILED'

    def test_a_confirmed_placement_is_named_in_the_reason(self, tmp_path):
        # once a poll reported a member_id the placement is no longer
        # advisory, so the failure may name the resource it happened on
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'polls': [{'state': 'FAILED', 'exit_code': 3,
                              'member_id': 'res-b.gpu', 'error': 'boom'}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))

        stage = camp.workflows[0].stages[0]
        assert stage.reason == 'the stage failed on resource res-b'
        assert stage.detail == 'boom'

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
        stage = camp.workflows[0].stages[0]
        assert stage.reason == REASON_NO_RESOURCE
        assert 'no resource satisfies requirements' in (stage.detail or '')

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

    def test_an_unknown_task_fails_the_stage_immediately(self, tmp_path):
        # a 404 must not burn the whole stage timeout
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'}, 'task_404': True}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path), stage_timeout=10 ** 6)
        stage = camp.workflows[0].stages[0]
        assert stage.state  == 'FAILED'
        assert stage.reason == REASON_NO_STATUS
        assert 'unknown task' in (stage.detail or '')
        assert fed.polls[stage.task_id] == 1        # one look, then out

    def test_repeated_status_errors_give_up_after_the_cap(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'task_error': 'gateway said no'}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path), stage_timeout=10 ** 6)
        stage = camp.workflows[0].stages[0]
        assert stage.state  == 'FAILED'
        assert stage.reason == REASON_NO_STATUS
        assert fed.polls[stage.task_id] == MAX_POLL_FAILURES

    def test_a_canceled_task_is_not_a_failure_text(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'polls': [{'state': 'CANCELED', 'error': 'pilot lost'}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))
        stage = camp.workflows[0].stages[0]
        assert stage.state  == 'CANCELED'
        assert stage.reason == REASON_STOPPED
        assert stage.detail == 'pilot lost'
        assert camp.workflows[0].state == 'CANCELED'
        assert camp.state == 'CANCELED'

    def test_exit_code_none_still_counts_as_success(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'polls': [{'state': 'DONE'}]}})       # no exit_code key
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))
        stage = camp.workflows[0].stages[0]
        assert stage.state     == 'DONE'
        assert stage.exit_code is None
        assert camp.state == 'DONE'

    def test_no_reason_carries_orbit_vocabulary(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'polls': [{'state': 'FAILED', 'exit_code': 7,
                              'error': 'rhapsody submit error: child '
                                       'endpoint unavailable'}]}})
        camp = _campaign(tmp_path, stages=[_spec()['stages'][0]])
        _run(fed, camp, _store(tmp_path))
        texts = [camp.reason or '']
        for wf in camp.workflows:
            texts.append(wf.reason or '')
            texts += [s.reason or '' for s in wf.stages]
        for text in texts:
            for word in ('pilot', 'broker', 'endpoint', 'plugin',
                         'dispatcher', 'rhapsody', 'Error'):
                assert word not in text, text

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
        # the on-screen phrase is the fixed one; what actually happened
        # goes to `detail`
        assert train.reason == REASON_STAGE_IN
        assert 'not produced' in (train.detail or '')
        # the task was never submitted -- the input is read before that
        assert [t['task_id'] for t, _ in fed.submits] == \
               ['cmp-test-wf-000-md']


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


# ---------------------------------------------------------------------------
class TestCampaignRollup:
    """A rolled-up FAILED campaign must say *why* -- the card and the CLI
    show the campaign's own reason, not the workflows'."""

    def _camp(self, *wfs):
        return Campaign(campaign_id='cmp-r', name='demo',
                        workflows=list(wfs))

    def _wf(self, wid, state, reason=None, detail=None):
        return WorkflowInstance(id=wid, state=state, reason=reason,
                                detail=detail)

    def test_one_shared_reason_becomes_the_campaigns_own(self):
        why  = "stage 'md': no resources have joined the federation yet"
        camp = self._camp(self._wf('wf-000', FAILED, why, 'HTTP 409'),
                          self._wf('wf-001', FAILED, why, 'HTTP 409'))
        assert camp.refresh_state() == FAILED
        assert camp.reason == why
        assert camp.detail == 'HTTP 409'

    def test_different_reasons_are_counted_instead(self):
        camp = self._camp(self._wf('wf-000', FAILED, 'a', 'detail a'),
                          self._wf('wf-001', FAILED, 'b'),
                          self._wf('wf-002', DONE))
        assert camp.refresh_state() == FAILED
        assert camp.reason == '2 of 3 workflows failed'
        assert camp.detail == 'detail a'

    def test_a_failed_workflow_without_a_reason_is_counted(self):
        camp = self._camp(self._wf('wf-000', FAILED))
        assert camp.refresh_state() == FAILED
        assert camp.reason == '1 of 1 workflows failed'

    def test_an_explicit_reason_is_never_overwritten(self):
        camp = self._camp(self._wf('wf-000', CANCELED, 'stage stopped'))
        camp.reason = 'the campaign was stopped'
        assert camp.refresh_state() == CANCELED
        assert camp.reason == 'the campaign was stopped'

    def test_a_running_campaign_gets_no_reason(self):
        camp = self._camp(self._wf('wf-000', PENDING))
        assert camp.refresh_state() == RUNNING
        assert camp.reason is None
        assert camp.finished_at is None

    def test_done_clears_a_reason_that_arrived_too_late(self):
        camp = self._camp(self._wf('wf-000', DONE))
        camp.reason, camp.detail = 'the campaign was stopped', 'cancel'
        assert camp.refresh_state() == DONE
        assert camp.reason is None
        assert camp.detail is None

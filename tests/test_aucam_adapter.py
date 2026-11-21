"""AuCaM adapter: real AuCaM steering synthetic frames over a fake federation.

The federation is faked in time, not in polls: a task waits ``queue_s``,
runs ``duration_sec x TIME_FACTOR`` (the synthetic tools' own duration,
shrunk), then is DONE.  Its outputs are the real tools' outputs -- the fake
runs ``atomic-fake-*`` in-process at submit time (without the padding), so
scores are exactly what a rehearsal would see.

AuCaM (``src.aucam`` @ 6842888) must be importable -- put the clone on
PYTHONPATH -- or the module is skipped.

``TestTrackAContract`` exercises "a cancelled ``StageRunner.run_workflow``
cancels its ORBIT task", which the runner provides since the endpoint work.
"""

import asyncio
import base64
import contextlib
import io
import time

from pathlib import Path

import pytest

pytest.importorskip('src.aucam', reason='AuCaM (src.aucam) not importable')

from atomic_wm.campaign.aucam_adapter import (                 # noqa: E402
    REASON_LIMIT, AucamCampaignDriver, BigsimController, launch_id,
    make_backend, parse_aucam_request, planner_of)
from atomic_wm.campaign.planner import PlannerError           # noqa: E402
from atomic_wm.campaign.runner import (FederationAPI,         # noqa: E402
                                       FederationUnavailable)
from atomic_wm.campaign.state import Campaign                 # noqa: E402
from atomic_wm.campaign.store import ResultStore              # noqa: E402
from atomic_wm.workload import fake_descriptors, fake_md, fake_train  # noqa
from atomic_wm.workload.frames import (GOOD_SCORE, POOR_SCORE,  # noqa: E402
                                       frame_workflow, synthetic_frames,
                                       synthetic_submit_body)


# one synthetic second takes this long here (a 5 s stage runs 0.1 s)
TIME_FACTOR = 0.02

TOOLS = {'atomic-fake-md'         : fake_md.main,
         'atomic-fake-train'      : fake_train.main,
         'atomic-fake-descriptors': fake_descriptors.main}


# ---------------------------------------------------------------------------
class TimedFederation(FederationAPI):
    """Tasks queue, run and finish in (shrunk) wall time."""

    def __init__(self, root, *, queue_s=0.0, slow=None, hang=None,
                 unavailable=False):
        self.root    = Path(root) / 'scratch'
        self.queue_s = queue_s
        # slow(task_id, n) -> run-time factor for the n-th submit of the
        # frame's stage; hang(task_id) -> True: polls of it never return
        self.slow    = slow or (lambda task_id, n: 1.0)
        self.hang    = hang or (lambda task_id: False)
        self.unavailable = unavailable

        self.submits = []                 # task ids, in order
        self.cancels = []                 # task ids
        self._tasks  = {}                 # task id -> record
        self._seen   = {}                 # '<frame>-<stage>' -> n submits

    async def submit(self, task, requirements):
        if self.unavailable:
            raise FederationUnavailable(FederationUnavailable.DEFAULT)
        tid = task['task_id']
        assert tid not in self._tasks, 'task id reused: %s' % tid
        cwd = self.root / tid
        cwd.mkdir(parents=True, exist_ok=True)
        for name, blob in (task.get('inputs_b64') or {}).items():
            (cwd / name).write_bytes(base64.b64decode(blob))
        duration = _execute(task['cmd'], cwd)

        # '<cid>-f<frame>-<hex>-<stage>': the same frame stage, any launch
        cid_frame, _, stage = tid.rpartition('-')
        key = '%s-%s' % (cid_frame.rsplit('-', 1)[0], stage)
        self._seen[key] = self._seen.get(key, 0) + 1
        run_s = duration * TIME_FACTOR * self.slow(tid, self._seen[key])

        self._tasks[tid] = {'t0': time.monotonic(), 'run_s': run_s,
                            'cwd': cwd, 'canceled': False,
                            'outputs': task['outputs']}
        self.submits.append(tid)
        return {'task': dict(task, state='QUEUED'), 'resource': 'res-a',
                'member': None, 'class': 'cpu', 'pool': 'fed-cpu',
                'dispatcher_sid': 'sid-a', 'members_eligible': []}

    async def task(self, task_id):
        if self.unavailable:
            raise FederationUnavailable(FederationUnavailable.DEFAULT)
        if self.hang(task_id):
            await asyncio.Event().wait()
        rec = self._tasks[task_id]
        age = time.monotonic() - rec['t0']
        out = {'task_id': task_id, 'cwd': str(rec['cwd']),
               'resource': 'res-a', 'member_id': 'res-a.m0'}
        if rec['canceled']:
            out['state'] = 'CANCELED'
        elif age < self.queue_s:
            out['state'] = 'QUEUED'
        elif age < self.queue_s + rec['run_s']:
            out['state'] = 'RUNNING'
        else:
            out.update(state='DONE', exit_code=0)
        return out

    async def resources(self):
        return [{'name': 'res-a'}]

    async def stage_out(self, dispatcher_sid, task_id, filename):
        path = self._tasks[task_id]['cwd'] / filename
        return path.read_bytes() if path.is_file() else None

    async def staging_get(self, endpoint, path):
        return None

    async def cancel_task(self, dispatcher_sid, task_id):
        self.cancels.append(task_id)
        if task_id in self._tasks:
            self._tasks[task_id]['canceled'] = True
        return True


def _execute(cmd, cwd):
    """Run one synthetic tool in-process, unpadded; return its duration."""

    argv = list(cmd[1:])
    duration = 0.0
    for flag in ('--duration-sec',):
        if flag in argv:
            idx = argv.index(flag)
            duration = float(argv[idx + 1])
            argv[idx + 1] = '0'
    for flag in ('--in', '--out'):
        if flag in argv:
            idx = argv.index(flag)
            argv[idx + 1] = str(Path(cwd) / argv[idx + 1])
    with contextlib.redirect_stdout(io.StringIO()):
        assert TOOLS[cmd[0]](argv) == 0
    return duration


# ---------------------------------------------------------------------------
FAST = {'policy_interval_s': 0.05, 'report_interval_s': 0.05,
        'cancel_wait_s': 1.0, 'idle_exit_s': 0.5,
        # one node-hour per running second: numbers read as seconds
        'node_hour_scale': 3600.0}

STAGE_KW = {'poll_interval': 0.01, 'poll_max_interval': 0.02}


def _body(n_frames=10, seed=0, **aucam):
    params = dict(FAST)
    params.update(aucam)
    return synthetic_submit_body(n_frames, seed, **params)


def _campaign(cid='cmp-aucam'):
    return Campaign(campaign_id=cid, name='frames', workflows=[])


def _drive(fed, store, body, cid='cmp-aucam', backend=None):
    plan   = parse_aucam_request(body)
    camp   = _campaign(cid)
    driver = AucamCampaignDriver(fed, store, plan, backend=backend,
                                 **STAGE_KW)
    return driver, camp


def _run(driver, camp):
    asyncio.run(driver.run(camp))
    return driver.outcome


def _frames_of(camp):
    out = {}
    for wf in camp.workflows:
        out.setdefault(wf.params['frame'], []).append(wf)
    return out


@pytest.fixture
def store(tmp_path):
    return ResultStore(Path(tmp_path) / 'store')


# ---------------------------------------------------------------------------
class TestRequest:

    def test_planner_switch(self):
        assert planner_of({}) == 'sweep'
        assert planner_of({'planner': 'aucam'}) == 'aucam'
        with pytest.raises(PlannerError):
            planner_of({'planner': 'magic'})

    def test_defaults(self):
        body = {'workflow': frame_workflow(cores=2),
                'aucam': {'frames': [{'temperature': 300, 'steps': 10,
                                      'epochs': 5, 'seed': 1, 'md_sec': 0,
                                      'train_sec': 0, 'analysis_sec': 0}]}}
        plan = parse_aucam_request(body)
        assert plan.frames[0]['frame'] == 0
        assert plan.resources == {'cpus': 2.0}
        assert plan.capacity == {'cpus': 8.0}          # 4 frames at once
        assert (plan.score_stage, plan.score_file) == \
               ('analysis', 'descriptors.json')
        assert plan.max_wall_time_s is None and plan.stale_timeout_s is None

    def test_frames_from_the_sweep(self):
        wf = {'name': 'w', 'stages': [
            {'name': 'md', 'cmd': ['atomic-fake-md', '--temperature', '{t}'],
             'outputs': ['md.json']}]}
        plan = parse_aucam_request({'workflow': wf,
                                    'sweep': {'t': [300, 600]}})
        assert [f['frame'] for f in plan.frames] == [0, 1]
        assert [f['t'] for f in plan.frames] == [300, 600]

    @pytest.mark.parametrize('aucam, text', [
        ({}, 'needs frames'),
        ({'frames': [{'frame': 0}]}, 'unknown parameter'),
        ({'frames': [{}], 'score': {'stage': 'nope'}}, 'is not a stage'),
        ({'frames': [{}], 'goal_good_frames': 2}, 'needs aucam.good_score'),
        ({'frames': [{}], 'budget_node_hours': -1}, 'must be >'),
        ({'frames': [{}], 'resources': {'nodes': 1}}, 'unknown resource'),
    ])
    def test_rejects(self, aucam, text):
        wf = {'name': 'w', 'stages': [
            {'name': 'md', 'cmd': ['x'], 'outputs': ['md.json']}]}
        if aucam.get('frames') == [{'frame': 0}]:
            wf['stages'][0]['cmd'] = ['x', '{missing}']
        with pytest.raises(PlannerError, match=text):
            parse_aucam_request({'workflow': wf, 'aucam': aucam})

    def test_launch_ids_are_unique_and_task_id_safe(self):
        assert launch_id(3, 'cmp-1.frame.0a1b2c3d') == 'f003-0a1b2c3d'
        assert launch_id(3, 'cmp-1.frame.0a1b2c3d') != \
               launch_id(3, 'cmp-1.frame.ffff0000')


# ---------------------------------------------------------------------------
class TestCampaign:

    def test_poor_frames_cancelled_goal_ends_it(self, tmp_path, store):
        fed = TimedFederation(tmp_path)
        driver, camp = _drive(fed, store, _body(10, 0))
        out = _run(driver, camp)
        expected = {f['frame']: f['expected_score']
                    for f in synthetic_frames(10, 0)}

        assert out['exit_reason'] == 'goals_satisfied'
        assert out['goal_reached'] is True
        assert camp.state == 'DONE'
        assert camp.reason.startswith('the goal was reached: 3 good')

        good = {f for f, s in expected.items() if s >= GOOD_SCORE}
        poor = {f for f, s in expected.items() if s < POOR_SCORE}
        assert set(out['good']) == good
        states = {r['frame']: r['state'] for r in out['per_frame']}
        assert {f for f, s in states.items() if s == 'POOR'} <= poor
        assert any(s == 'POOR' for s in states.values())

        # a poor frame never ran its expensive stage to the end, and the
        # stage that was running was cancelled on the resource
        for frame, wfs in _frames_of(camp).items():
            if states[frame] != 'POOR':
                continue
            wf = wfs[-1]
            assert wf.state == 'CANCELED'
            assert 'score' in wf.reason and 'worse than' in wf.reason
            analysis = wf.stage('analysis')
            assert analysis.state != 'DONE'
            if analysis.task_id:
                assert analysis.task_id in fed.cancels

        # every launch is its own ATOMIC instance, and owns its task ids
        assert len(camp.workflows) == out['launches']
        assert len({wf.id for wf in camp.workflows}) == len(camp.workflows)
        assert len(set(fed.submits)) == len(fed.submits)
        for tid in fed.submits:
            assert tid.startswith('cmp-aucam-f')

        # the Explorer view: every launch is in the campaign, terminal
        assert all(wf.state in ('DONE', 'CANCELED') for wf in camp.workflows)
        assert out['aucam']['completed'].get('frame', 0) >= 3

    def test_budget_ends_it(self, tmp_path, store):
        fed = TimedFederation(tmp_path)
        body = _body(10, 0, goal_good_frames=None, budget_node_hours=1.5,
                     frame_node_hours=0.8, learn=False)
        driver, camp = _drive(fed, store, body)
        out = _run(driver, camp)

        assert out['exit_reason'] == 'spending_limit'
        assert camp.state == 'DONE'
        assert 'budget' in camp.reason
        assert out['goal_reached'] is False
        # not every frame got to run
        assert out['states'].get('PENDING', 0) + \
               out['states'].get('DROPPED', 0) > 0

    def test_relaunch_gets_a_new_task_id(self, tmp_path, store):
        # frame 1's first launch runs 20x slower and goes past its own
        # node-hour limit; AuCaM cancels it, the controller relaunches it
        first = []

        def slow(task_id, n):
            if '-f001-' not in task_id:
                return 1.0
            launch = task_id.rpartition('-')[0]
            first[:] = first or [launch]
            return 20.0 if launch == first[0] else 1.0

        fed    = TimedFederation(tmp_path, slow=slow)
        frames = [dict(f, md_sec=5, train_sec=5, analysis_sec=5,
                       temperature=300.0)
                  for f in synthetic_frames(3, 1)]
        body   = _body(goal_good_frames=None, poor_score=None,
                       frame_node_hours=1.0, learn=False,
                       max_concurrent=3)
        body['aucam']['frames'] = frames
        driver, camp = _drive(fed, store, body)
        out = _run(driver, camp)

        assert out['aucam']['budget_cancels'] == 1
        assert out['relaunches'] == 1
        first, second = _frames_of(camp)[1]
        assert first.id != second.id
        assert first.state == 'CANCELED' and first.reason == REASON_LIMIT
        assert second.state == 'DONE'
        assert second.params['attempt'] == 2
        assert first.stages[0].task_id != second.stages[0].task_id
        # the over-limit launch's running task was cancelled on the resource
        assert any(t.startswith('cmp-aucam-%s-' % first.id)
                   for t in fed.cancels)
        assert camp.state == 'DONE'
        assert out['exit_reason'] in ('controller_terminate',
                                      'campaign_idle')

    def test_node_hours_charged_from_reports(self, tmp_path, store):
        # every task waits 0.3 s before it runs: resources x wall time would
        # charge that wait, the reports do not
        fed    = TimedFederation(tmp_path, queue_s=0.3)
        frames = [dict(f, md_sec=5, train_sec=5, analysis_sec=5,
                       temperature=300.0)
                  for f in synthetic_frames(2, 2)]
        body   = _body(goal_good_frames=None, poor_score=None,
                       max_concurrent=2)
        body['aucam']['frames'] = frames
        driver, camp = _drive(fed, store, body)
        out = _run(driver, camp)

        charged = out['spent']['node_hours']
        measured = sum(driver.spent(wf)[0] for wf in camp.workflows)
        wall = sum(wf.finished_at - wf.created_at for wf in camp.workflows)
        assert charged == pytest.approx(measured, rel=0.05)
        # 3 stages x 0.1 s running per frame, against >= 3 x 0.4 s of wall
        assert charged < 0.5 * wall
        assert charged == pytest.approx(2 * 3 * 5 * TIME_FACTOR, rel=0.5)

    def test_concurrent_drivers_share_a_backend(self, tmp_path, store):
        fed = TimedFederation(tmp_path)

        async def main():
            backend = await make_backend()
            try:
                runs = []
                for cid, seed in (('cmp-a', 0), ('cmp-b', 1)):
                    driver, camp = _drive(fed, store, _body(6, seed),
                                          cid=cid, backend=backend)
                    runs.append((driver, camp))
                await asyncio.gather(*[d.run(c) for d, c in runs])
                return runs
            finally:
                await backend.shutdown()

        runs = asyncio.run(main())
        for driver, camp in runs:
            assert camp.state == 'DONE'
            assert driver.outcome['exit_reason'] in (
                'goals_satisfied', 'controller_terminate', 'campaign_idle')
            for wf in camp.workflows:
                for st in wf.stages:
                    if st.task_id:
                        assert st.task_id.startswith(camp.campaign_id + '-')
        assert len(set(fed.submits)) == len(fed.submits)

    def test_stop_cancels_running_frames(self, tmp_path, store):
        fed  = TimedFederation(tmp_path, slow=lambda t, n: 50.0)
        driver, camp = _drive(fed, store, _body(4, 0))

        async def main():
            task = asyncio.ensure_future(driver.run(camp))
            while not any(fed.submits):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            driver.cancel()
            await task

        asyncio.run(main())
        assert camp.state == 'CANCELED'
        assert driver.outcome['exit_reason'] == 'signal'
        assert camp.workflows
        for wf in camp.workflows:
            assert wf.state == 'CANCELED'
            assert wf.reason == 'the campaign was stopped'
        # the stages that were running were cancelled on the resource
        assert set(fed.cancels) == set(fed.submits)

    def test_federation_unavailable_fails_the_campaign(self, tmp_path,
                                                       store):
        fed = TimedFederation(tmp_path, unavailable=True)
        driver, camp = _drive(fed, store, _body(4, 0))
        _run(driver, camp)
        assert camp.state == 'FAILED'
        assert camp.reason == FederationUnavailable.DEFAULT

    def test_the_driver_returns_the_outcome(self, tmp_path, store):
        fed  = TimedFederation(tmp_path)
        driver, camp = _drive(fed, store, _body(4, 3, goal_good_frames=None))
        out  = _run(driver, camp)
        assert out['planner'] == 'aucam'
        assert out['frames'] == 4
        assert camp.state == 'DONE'


# ---------------------------------------------------------------------------
class TestTrackAContract:

    def test_cancel_reaches_orbit_when_the_runner_is_stuck(self, tmp_path,
                                                           store):
        # the poll of frame 0's analysis never returns: the runner never
        # sees its stop event, so the block falls back to cancelling the
        # runner task -- which must cancel the ORBIT task (track A)
        fed = TimedFederation(
            tmp_path, hang=lambda t: '-f000-' in t and t.endswith('-train'))
        body = _body(1, 0, goal_good_frames=None, cancel_wait_s=0.2)
        driver, camp = _drive(fed, store, body)

        async def main():
            task = asyncio.ensure_future(driver.run(camp))
            while not any(t.endswith('-train') for t in fed.submits):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            driver.cancel()
            await task

        asyncio.run(main())
        train = [t for t in fed.submits if t.endswith('-train')]
        assert train and train[0] in fed.cancels


# ---------------------------------------------------------------------------
def test_controller_requires_frames_in_obs_shape():
    # the controller's obs keys are what AuCaM goals read (top level)
    plan = parse_aucam_request(_body(3, 0))
    ctl  = BigsimController(plan)
    assert [g.metric for g in ctl.goals()] == ['good_frames']


# ---------------------------------------------------------------------------
@pytest.fixture
def plugin_clients():
    """Close the plugin TestClients this module opens (their fixture lives in
    test_plugin_campaign and does not run here)."""

    import test_plugin_campaign as tpc
    yield tpc
    while tpc._OPEN_CLIENTS:
        try:
            tpc._OPEN_CLIENTS.pop().__exit__(None, None, None)
        except Exception:                                     # noqa: BLE001
            pass


class TestPluginRoute:
    """``planner: aucam`` through the plugin's submit route."""

    def test_aucam_campaign_runs_through_the_plugin(self, tmp_path,
                                                    plugin_clients):
        NS, _make_plugin = plugin_clients.NS, plugin_clients._make_plugin
        _request = plugin_clients._request
        _wait_terminal = plugin_clients._wait_terminal

        fed = TimedFederation(tmp_path)
        _, plugin = _make_plugin(tmp_path, fed=fed)
        client = _request(plugin)

        resp = client.post('%s/campaigns/default' % NS, json=_body())
        assert resp.status_code == 200, resp.text
        cid = resp.json()['campaign_id']

        camp = _wait_terminal(client, cid, timeout=60.0)
        assert camp['planner'] == 'aucam'
        assert camp['state'] == 'DONE', camp.get('reason')
        assert camp['workflows'], 'AuCaM launched no frame'
        assert camp['outcome'], 'no AuCaM outcome stored'
        # every launch got its own task ids
        assert len(set(fed.submits)) == len(fed.submits)

    def test_bad_aucam_request_is_400(self, tmp_path, plugin_clients):
        NS, _make_plugin = plugin_clients.NS, plugin_clients._make_plugin
        _request = plugin_clients._request

        _, plugin = _make_plugin(tmp_path, fed=TimedFederation(tmp_path))
        body = _body(direction='sideways')
        resp = _request(plugin).post('%s/campaigns/default' % NS, json=body)
        assert resp.status_code == 400

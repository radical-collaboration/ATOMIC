"""Unit tests for the broker-hosted ``atomic_campaign`` plugin.

Mounted the way ``radical.orbit/tests/unittests/test_plugin_task_dispatcher
.py`` mounts the dispatcher: a bare ``FastAPI()`` carrying the broker state
attributes, the plugin constructed on it, and a ``starlette`` ``TestClient``
over ``plugin._app``.  The federation is faked -- either through the
``fed_api`` seam (the happy paths) or by giving the real ``_FederationAPI`` a
plugin host that hosts nothing (the "no federation" path the demo broker
shows).
"""

import asyncio
import json
import time

from pathlib import Path

import pytest

from fastapi import FastAPI
from starlette.testclient import TestClient

from atomic_wm.plugins.campaign import PluginAtomicCampaign, _FederationAPI

from test_runner import FakeFederation, _envelope, _spec       # noqa: I100

NS = '/atomic_campaign'


# ---------------------------------------------------------------------------
class FakeHost:
    """Stand-in for ``BrokerPluginHost``: a plugin registry + handle_request."""

    def __init__(self, plugins=None, responses=None):
        self.plugins   = plugins or {}
        self.responses = responses or {}
        self.calls     = []
        self.notified  = []

    async def handle_request(self, method, path, headers, body_bytes,
                             query_string=''):
        self.calls.append((method, path, body_bytes))
        from starlette.responses import JSONResponse
        return JSONResponse(self.responses.get((method, path), {}))

    async def send_notification(self, plugin, topic, data):
        self.notified.append((plugin, topic, data))


# ---------------------------------------------------------------------------
def _make_plugin(tmp_path, fed=None, host=None, **kwargs):
    """Build the plugin on a broker-shaped app; return (app, plugin)."""

    app = FastAPI()
    app.state.endpoint_name    = 'broker'
    app.state.is_broker        = True
    app.state.broker_url       = 'https://localhost:9999'
    app.state.broker_caller    = None
    app.state.broker_tap       = None
    app.state.endpoint_service = host if host is not None else FakeHost()

    kwargs.setdefault('poll_interval_sec', 0.01)
    kwargs.setdefault('poll_max_interval_sec', 0.01)
    plugin = PluginAtomicCampaign(app,
                                  state_root=Path(tmp_path) / 'state',
                                  store_root=Path(tmp_path) / 'store',
                                  fed_api=fed, **kwargs)
    return app, plugin


_OPEN_CLIENTS = []


def _request(plugin):
    """A TestClient whose event loop *persists across requests*.

    Without the context manager starlette builds (and tears down) one portal
    -- i.e. one event loop -- per request, which would kill the campaign
    driver task the POST handler spawned.  The autouse fixture below closes
    the clients again at the end of each test.
    """

    client = TestClient(plugin._app)
    client.__enter__()
    _OPEN_CLIENTS.append(client)
    return client


@pytest.fixture(autouse=True)
def _close_clients():
    yield
    while _OPEN_CLIENTS:
        try:
            _OPEN_CLIENTS.pop().__exit__(None, None, None)
        except Exception:                                     # noqa: BLE001
            pass


def _submit(client, sweep=None, spec=None):
    body = {'workflow': spec or _spec(), 'sweep': sweep or {'temperature':
                                                            [300, 600]}}
    return client.post('%s/campaigns/default' % NS, json=body)


def _wait_terminal(client, cid, timeout=10.0):
    """Poll the campaign route until the driver has finished."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        camp = client.get('%s/campaign/default/%s' % (NS, cid)).json()
        if camp['state'] in ('DONE', 'FAILED', 'CANCELED', 'INTERRUPTED'):
            return camp
        time.sleep(0.02)
    raise AssertionError('campaign %s did not finish' % cid)


def _fed(tmp_path):
    return FakeFederation(tmp_path, plans={
        'md'   : {'outputs': {'md.json': _envelope('simulation', 1)},
                  'resource': 'res-a'},
        'train': {'outputs': {'model.json': _envelope('ml_training', 2)},
                  'resource': 'res-b'}})


# ---------------------------------------------------------------------------
class TestPluginBasics:

    def test_is_enabled_on_broker_only(self):
        from unittest.mock import patch
        with patch('radical.orbit.utils.host_role') as m:
            m.return_value = {'role': 'broker'}
            assert PluginAtomicCampaign.is_enabled(FastAPI()) is True
            for role in ('login', 'compute', 'standalone'):
                m.return_value = {'role': role}
                assert PluginAtomicCampaign.is_enabled(FastAPI()) is False

    def test_routes_are_registered(self, tmp_path):
        app, plugin = _make_plugin(tmp_path)
        pats = [pat.pattern for _, pat, _, _ in app.state.direct_routes]
        for frag in ('atomic_campaign/campaigns/',
                     'atomic_campaign/campaign/',
                     'atomic_campaign/results/',
                     'atomic_campaign/cancel/',
                     'atomic_campaign/store/',
                     'atomic_campaign/health'):
            assert any(frag in p for p in pats), 'route %s missing' % frag

    def test_health_and_version(self, tmp_path):
        _, plugin = _make_plugin(tmp_path)
        client = _request(plugin)
        health = client.get('%s/health' % NS)
        assert health.status_code == 200
        assert health.json()['plugin'] == 'atomic_campaign'
        assert client.get('%s/version' % NS).json()['version'] \
            == PluginAtomicCampaign.version

    def test_ui_module_points_at_the_packaged_js(self, tmp_path):
        path = PluginAtomicCampaign.ui_module
        # P5 ships the file; if it is absent the attribute must be None,
        # never a broken path (BrokerPluginHost would silently skip it)
        if path is not None:
            assert Path(path).is_file()
            assert Path(path).name == 'atomic_campaign.js'

    def test_default_session_is_created_on_demand(self, tmp_path):
        _, plugin = _make_plugin(tmp_path)
        client = _request(plugin)
        assert client.get('%s/campaigns/default' % NS).status_code == 200
        assert 'default' in plugin._sessions

    def test_unknown_session_is_404(self, tmp_path):
        _, plugin = _make_plugin(tmp_path)
        r = _request(plugin).get('%s/campaigns/nope' % NS)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
class TestSubmit:

    def test_submit_returns_the_planned_campaign(self, tmp_path):
        _, plugin = _make_plugin(tmp_path, fed=_fed(tmp_path))
        client = _request(plugin)
        r = _submit(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['state'] == 'RUNNING'
        assert body['campaign_id'].startswith('cmp-')
        assert len(body['workflows']) == 2
        assert [s['name'] for s in body['workflows'][0]['stages']] \
            == ['md', 'train']
        _wait_terminal(client, body['campaign_id'])

    def test_campaign_runs_to_done_and_collects(self, tmp_path):
        _, plugin = _make_plugin(tmp_path, fed=_fed(tmp_path))
        client = _request(plugin)
        cid  = _submit(client).json()['campaign_id']
        camp = _wait_terminal(client, cid)

        assert camp['state'] == 'DONE'
        assert all(wf['state'] == 'DONE' for wf in camp['workflows'])
        stages = camp['workflows'][0]['stages']
        assert [s['resource'] for s in stages] == ['res-a', 'res-b']
        assert stages[0]['task_id'].startswith(cid)

        results = client.get('%s/results/default/%s' % (NS, cid)).json()
        assert len(results['workflows']) == 2
        assert results['workflows'][0]['metrics']['md']['type'] \
            == 'simulation'
        assert results['workflows'][0]['metrics']['train']['type'] \
            == 'ml_training'

    def test_list_shows_the_campaign_summary(self, tmp_path):
        _, plugin = _make_plugin(tmp_path, fed=_fed(tmp_path))
        client = _request(plugin)
        cid = _submit(client).json()['campaign_id']
        _wait_terminal(client, cid)

        body = client.get('%s/campaigns/default' % NS).json()
        assert isinstance(body['campaigns'], list)
        entry = body['campaigns'][0]
        assert entry['campaign_id'] == cid
        assert entry['n_workflows'] == 2
        assert entry['name'] == 'demo'
        assert isinstance(entry['started_at'], float)
        assert isinstance(entry['finished_at'], float)

    def test_store_route_lists_the_collected_files(self, tmp_path):
        _, plugin = _make_plugin(tmp_path, fed=_fed(tmp_path))
        client = _request(plugin)
        cid = _submit(client).json()['campaign_id']
        _wait_terminal(client, cid)

        layout = client.get('%s/store/default' % NS).json()
        assert layout['root'].endswith('store')
        entry = layout['campaigns'][0]
        assert entry['campaign_id'] == cid
        files = entry['workflows'][0]['stages'][0]['files']
        assert {f['name'] for f in files} == {'md.json', 'manifest.json'}

    def test_bad_spec_is_a_400(self, tmp_path):
        _, plugin = _make_plugin(tmp_path, fed=_fed(tmp_path))
        client = _request(plugin)
        r = client.post('%s/campaigns/default' % NS,
                        json={'workflow': {'name': 'x', 'stages': []}})
        assert r.status_code == 400
        assert 'stages' in r.json()['detail']

    def test_missing_workflow_is_a_400(self, tmp_path):
        _, plugin = _make_plugin(tmp_path, fed=_fed(tmp_path))
        r = _request(plugin).post('%s/campaigns/default' % NS, json={})
        assert r.status_code == 400

    def test_unknown_placeholder_is_a_400(self, tmp_path):
        spec = _spec()
        spec['stages'][0]['cmd'] = ['atomic-fake-md', '{pressure}']
        _, plugin = _make_plugin(tmp_path, fed=_fed(tmp_path))
        r = _request(plugin).post('%s/campaigns/default' % NS,
                                  json={'workflow': spec,
                                        'sweep': {'temperature': [300]}})
        assert r.status_code == 400
        assert 'pressure' in r.json()['detail']

    def test_unknown_campaign_is_a_404(self, tmp_path):
        _, plugin = _make_plugin(tmp_path)
        client = _request(plugin)
        assert client.get('%s/campaign/default/nope' % NS).status_code == 404
        assert client.get('%s/results/default/nope' % NS).status_code == 404
        assert client.post('%s/cancel/default/nope' % NS).status_code == 404


# ---------------------------------------------------------------------------
class TestNoFederation:
    """The manual check: a broker without the federation plugin."""

    def test_campaign_fails_with_a_clear_reason(self, tmp_path):
        host = FakeHost(plugins={'task_dispatcher': object()})
        _, plugin = _make_plugin(tmp_path, host=host)   # real _FederationAPI
        client = _request(plugin)
        r = _submit(client)
        assert r.status_code == 200               # accepted, then fails

        camp = _wait_terminal(client, r.json()['campaign_id'])
        assert camp['state']  == 'FAILED'
        assert camp['reason'] == 'the resource federation is not available'
        # the on-screen reason carries no ORBIT vocabulary
        for word in ('plugin', 'broker', 'pilot', 'endpoint'):
            assert word not in camp['reason']
        assert all(wf['state'] == 'FAILED' for wf in camp['workflows'])

    def test_federation_api_raises_when_the_plugin_is_absent(self, tmp_path):
        app, _ = _make_plugin(tmp_path, host=FakeHost())
        api = _FederationAPI(app)
        assert api.has_plugin('federation') is False
        with pytest.raises(Exception) as exc:
            asyncio.run(api.submit({'task_id': 't'}, {}))
        assert 'resource federation is not available' in str(exc.value)


# ---------------------------------------------------------------------------
class TestFederationAPICalls:
    """The in-process call shape (path, body, parsing)."""

    def _api(self, tmp_path, responses):
        host = FakeHost(plugins={'federation': object(),
                                 'task_dispatcher': object()},
                        responses=responses)
        app, _ = _make_plugin(tmp_path, host=host)
        return _FederationAPI(app), host

    def test_submit_posts_to_the_federation_route(self, tmp_path):
        api, host = self._api(tmp_path, {
            ('POST', '/federation/submit/default'):
                {'task': {'task_id': 't1'}, 'resource': 'res-a',
                 'pool': 'fed-a', 'dispatcher_sid': 'sid-a'}})
        out = asyncio.run(api.submit({'task_id': 't1'}, {'cores': 2}))
        assert out['resource'] == 'res-a'
        method, path, body = host.calls[0]
        assert (method, path) == ('POST', '/federation/submit/default')
        assert json.loads(body) == {'task': {'task_id': 't1'},
                                    'requirements': {'cores': 2}}

    def test_task_lookup_uses_the_default_sid(self, tmp_path):
        api, host = self._api(tmp_path, {
            ('GET', '/federation/task/default/t1'): {'state': 'DONE'}})
        assert asyncio.run(api.task('t1'))['state'] == 'DONE'
        assert host.calls[0][1] == '/federation/task/default/t1'

    def test_stage_in_sends_pool_filename_and_content(self, tmp_path):
        api, host = self._api(tmp_path, {
            ('POST', '/task_dispatcher/stage_in/sid-a/t1'): {'cwd': '/tmp/x'}})
        out = asyncio.run(api.stage_in('sid-a', 'fed-a', 't1', 'md.json',
                                       b'hello'))
        assert out['cwd'] == '/tmp/x'
        body = json.loads(host.calls[0][2])
        assert body['pool']        == 'fed-a'
        assert body['filename']    == 'md.json'
        assert body['overwrite']   is True
        import base64
        assert base64.b64decode(body['content_b64']) == b'hello'

    def test_stage_out_decodes_content_b64(self, tmp_path):
        import base64
        api, _ = self._api(tmp_path, {
            ('GET', '/task_dispatcher/stage_out/sid-a/t1/md.json'):
                {'content_b64': base64.b64encode(b'data').decode()}})
        assert asyncio.run(api.stage_out('sid-a', 't1', 'md.json')) == b'data'

    def test_stage_out_returns_none_when_absent(self, tmp_path):
        api, _ = self._api(tmp_path, {})
        assert asyncio.run(api.stage_out('sid-a', 't1', 'md.json')) is None

    def test_staging_needs_a_broker_caller(self, tmp_path):
        api, _ = self._api(tmp_path, {})
        assert asyncio.run(api.staging_get('child', '/tmp/x')) is None
        assert asyncio.run(api.staging_put('child', '/tmp/x', b'y')) is False

    def test_resources_unwraps_the_list(self, tmp_path):
        api, _ = self._api(tmp_path, {
            ('GET', '/federation/resources/default'):
                {'resources': [{'name': 'res-a'}]}})
        assert asyncio.run(api.resources()) == [{'name': 'res-a'}]


# ---------------------------------------------------------------------------
class TestCancelRoute:

    def test_cancel_marks_the_campaign(self, tmp_path):
        fed = FakeFederation(tmp_path, plans={
            'md': {'outputs': {'md.json': b'{}'},
                   'polls': [{'state': 'RUNNING'}]}})       # never finishes
        _, plugin = _make_plugin(tmp_path, fed=fed)
        client = _request(plugin)
        spec = _spec([_spec()['stages'][0]])
        cid  = client.post('%s/campaigns/default' % NS,
                           json={'workflow': spec,
                                 'sweep': {'temperature': [300]}}
                           ).json()['campaign_id']

        deadline = time.time() + 5
        while not fed.submits and time.time() < deadline:
            time.sleep(0.02)

        r = client.post('%s/cancel/default/%s' % (NS, cid))
        assert r.status_code == 200
        assert r.json()['canceled'] is True

        camp = _wait_terminal(client, cid)
        assert camp['state'] == 'CANCELED'

    def test_cancel_of_a_finished_campaign_is_a_no_op(self, tmp_path):
        _, plugin = _make_plugin(tmp_path, fed=_fed(tmp_path))
        client = _request(plugin)
        cid = _submit(client).json()['campaign_id']
        _wait_terminal(client, cid)
        r = client.post('%s/cancel/default/%s' % (NS, cid))
        assert r.json()['canceled'] is False


# ---------------------------------------------------------------------------
class TestPersistenceAcrossRestart:

    def test_state_survives_and_running_becomes_interrupted(self, tmp_path):
        _, plugin = _make_plugin(tmp_path, fed=_fed(tmp_path))
        client = _request(plugin)
        cid = _submit(client).json()['campaign_id']
        _wait_terminal(client, cid)

        # a "restart": a second plugin instance over the same state root
        _, plugin2 = _make_plugin(tmp_path, fed=_fed(tmp_path))
        body = _request(plugin2).get('%s/campaigns/default' % NS).json()
        assert [c['campaign_id'] for c in body['campaigns']] == [cid]
        assert body['campaigns'][0]['state'] == 'DONE'

    def test_state_root_env_override(self, tmp_path, monkeypatch):
        from atomic_wm.campaign.state import (default_state_root,
                                              default_store_root)
        monkeypatch.setenv('ATOMIC_CAMPAIGN_STATE', str(tmp_path / 'cs'))
        monkeypatch.setenv('ATOMIC_STORE_ROOT',     str(tmp_path / 'st'))
        assert default_state_root() == tmp_path / 'cs'
        assert default_store_root() == tmp_path / 'st'

    def test_plugin_honours_the_env_overrides(self, tmp_path, monkeypatch):
        monkeypatch.setenv('ATOMIC_CAMPAIGN_STATE', str(tmp_path / 'cs'))
        monkeypatch.setenv('ATOMIC_STORE_ROOT',     str(tmp_path / 'st'))
        app = FastAPI()
        app.state.endpoint_name    = 'broker'
        app.state.is_broker        = True
        app.state.broker_caller    = None
        app.state.endpoint_service = FakeHost()
        plugin = PluginAtomicCampaign(app, fed_api=_fed(tmp_path))
        assert plugin._state_file == tmp_path / 'cs' / 'state.json'
        assert plugin.store.root  == tmp_path / 'st'

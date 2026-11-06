"""Unit tests for ``atomic_wm.client`` -- the CLIs' HTTP client."""

import json

import pytest

from atomic_wm import client
from atomic_wm import client as _client
from atomic_wm.client import Client, ClientError

from fake_broker import FakeBroker, FakeResponse

# a throwaway self-signed certificate (CN=atomic-test, no SAN -- exactly
# like ORBIT's own broker certs).  It is never used to talk to anything;
# it only has to be loadable as a trust root.
TEST_CERT = """\
-----BEGIN CERTIFICATE-----
MIIDDzCCAfegAwIBAgIUDelg/ybNyJ8nNyv733kGwGxr7EUwDQYJKoZIhvcNAQEL
BQAwFjEUMBIGA1UEAwwLYXRvbWljLXRlc3QwIBcNMjYwOTA1MjIzMjU5WhgPMjEy
NjA4MTIyMjMyNTlaMBYxFDASBgNVBAMMC2F0b21pYy10ZXN0MIIBIjANBgkqhkiG
9w0BAQEFAAOCAQ8AMIIBCgKCAQEAm8kAY5162Xxj1KclrXp+aHBH8zxI3ea5savW
onpoLN0S5PoKzBMups4P7o11XXQgjFpRCwzHNQ4q52VFjykl/QxCuTfHrpqfngyB
3rTljUuDeNY3U7iCBuEVzoNC0q800nOyCfpnPpe28wWWjXdOpOeFmJfB94VXMh7g
6T0sMML786sLDFU0Ux7Lv5HmW8OYYgBUCpYfd/s1VMc8zrz4O+4iMAEn7Df795zM
qiknJFUPWEMUUeAzPsydQStO0kYoQleO/ljx/vctKf6GLyVnU7KZaLQgKdh/KdnX
1q5kYZb5ch4FjBjUuL6AiYXOaAsOL/E46j+7ejFo26jCgFjO/wIDAQABo1MwUTAd
BgNVHQ4EFgQUA3NaSVWMv67/YTtenu/EIYs0+z4wHwYDVR0jBBgwFoAUA3NaSVWM
v67/YTtenu/EIYs0+z4wDwYDVR0TAQH/BAUwAwEB/zANBgkqhkiG9w0BAQsFAAOC
AQEAAfHFrv69lX+7EMUFq/ohPGTZQsH62faeYunUnHyXGdqvlijfguh2zmSBsjEn
AOBlJl0L+HAs4z+TqUQDSR5YaiBGukMrufPbxiuVvhS6CQLs7c/ydhVwBN4MI+jH
xwrnd0B5Mekj+f1GTUTsusMEyyqcK+jQmkh8ZRzg9K6a67KnqxDbst8o15vmA64q
zY9j+wrFTzIzaKTd2xGzuTb5PqtmCnUoBoZMjpfQZMX7Lacbbpk7e9rG8HY3Zq1S
C+kaxQhgnghVs9N8BCGssZ5S4E52V0znINpOUu5OKiISPzvb4BrrjhGtt/AU2yJc
NqIp93rWFhh474E7N/lXoBR5Jw==
-----END CERTIFICATE-----
"""


# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No stray broker config from the developer's own environment.

    ``$HOME`` moves too: the client falls back to the operator-placed
    ``~/.radical/orbit/{broker_cert.pem,broker.token}``, which exist on a
    machine that runs ORBIT.
    """

    monkeypatch.setenv('HOME', str(tmp_path / 'home'))

    for var in ['RADICAL_ORBIT_BROKER_URL', 'RADICAL_ORBIT_TOKEN',
                'RADICAL_ORBIT_BROKER_TOKEN', 'RADICAL_ORBIT_BROKER_CERT']:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def broker(monkeypatch):

    return FakeBroker().install(monkeypatch)


@pytest.fixture
def orbit_home(tmp_path):
    """``~/.radical/orbit`` with an operator-placed cert and token."""

    path = tmp_path / 'home' / '.radical' / 'orbit'
    path.mkdir(parents=True)
    (path / 'broker_cert.pem').write_text(TEST_CERT)
    (path / 'broker.token').write_text('file-token\n')

    return path


@pytest.fixture
def cert_file(tmp_path):

    path = tmp_path / 'broker_cert.pem'
    path.write_text(TEST_CERT)

    return str(path)


def make_client(**kw):

    kw.setdefault('broker', 'https://127.0.0.1:8013')
    kw.setdefault('token', '')
    kw.setdefault('cert', None)

    return Client(**kw)


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------

def test_broker_url_required():

    with pytest.raises(ValueError):
        Client(broker=None, token='')


def test_env_defaults(monkeypatch, cert_file):

    monkeypatch.setenv('RADICAL_ORBIT_BROKER_URL', 'https://host:8003/')
    monkeypatch.setenv('RADICAL_ORBIT_TOKEN', 'tok')
    monkeypatch.setenv('RADICAL_ORBIT_BROKER_CERT', cert_file)

    c = Client()

    assert c.broker == 'https://host:8003'      # trailing slash stripped
    assert c.token  == 'tok'
    assert c.cert   == cert_file


def test_url_building():

    c = make_client()

    assert c.url('/endpoints')  == 'https://127.0.0.1:8013/endpoints'
    assert c.url('endpoints')   == 'https://127.0.0.1:8013/endpoints'
    assert c.url('/broker/federation/join/default') \
        == 'https://127.0.0.1:8013/broker/federation/join/default'


# ---------------------------------------------------------------------------
# headers / TLS
# ---------------------------------------------------------------------------

def test_no_auth_header_when_token_empty(broker):

    broker.connect('ep_a')
    c = make_client(token='')
    c.endpoints()

    assert c.headers() == {}
    assert 'Authorization' not in broker.calls[-1]['headers']


def test_auth_header_when_token_given(broker):

    broker.connect('ep_a')
    c = make_client(token='s3cret')
    c.endpoints()

    assert broker.calls[-1]['headers']['Authorization'] == 'Bearer s3cret'


def test_tls_verify_uses_cert(broker, cert_file):

    broker.connect('ep_a')

    client = make_client(cert=cert_file)
    client.endpoints()
    assert broker.calls[-1]['verify'] == cert_file

    # the broker cert is pinned as the only trust root, with the hostname
    # check off -- as radical.orbit's own endpoints do it
    adapter = client.session.get_adapter('https://x/')
    assert isinstance(adapter, _client.PinnedCertAdapter)

    make_client(cert=None).endpoints()
    assert broker.calls[-1]['verify'] is True


def test_tls_pin_survives_an_http_proxy(cert_file):
    # behind https_proxy (OLCF compute nodes) requests builds a separate
    # ProxyManager; the pin must reach it too, or the broker's self-signed
    # cert fails default verification while the endpoint joins fine
    adapter = _client.PinnedCertAdapter(cert_file)

    adapter.init_poolmanager(1, 1)
    proxied = adapter.proxy_manager_for('http://proxy.example:3128')

    for mgr in (adapter.poolmanager, proxied):
        kw = mgr.connection_pool_kw
        assert kw['assert_hostname'] is False
        assert kw['ssl_context'].check_hostname is False
        assert kw['ssl_context'].verify_mode == _client.ssl.CERT_REQUIRED


def test_orbit_files_are_the_last_default(orbit_home):

    # a machine set up for ORBIT needs neither --cert nor --token
    c = Client(broker='https://127.0.0.1:8013')

    assert c.cert  == str(orbit_home / 'broker_cert.pem')
    assert c.token == 'file-token'
    assert c.headers() == {'Authorization': 'Bearer file-token'}


def test_explicit_empty_token_beats_the_token_file(orbit_home):

    assert Client(broker='https://x', token='').token == ''


def test_env_token_beats_the_token_file(orbit_home, monkeypatch):

    monkeypatch.setenv('RADICAL_ORBIT_BROKER_TOKEN', 'env-token')

    assert Client(broker='https://x').token == 'env-token'


def test_missing_cert_is_reported(tmp_path):

    with pytest.raises(ValueError) as exc:
        make_client(cert=str(tmp_path / 'nope.pem'))

    assert 'broker cert not found' in str(exc.value)


def test_unusable_cert_is_reported(tmp_path):

    junk = tmp_path / 'junk.pem'
    junk.write_text('not a certificate\n')

    with pytest.raises(ValueError) as exc:
        make_client(cert=str(junk))

    assert 'not usable' in str(exc.value)


# ---------------------------------------------------------------------------
# gateway routes
# ---------------------------------------------------------------------------

def test_endpoints_and_connected(broker):

    broker.connect('ep_a')
    broker.endpoints['ep_b'] = {'connected': False, 'plugins': []}

    c = make_client()

    assert {e['name'] for e in c.endpoints()} == {'ep_a', 'ep_b'}
    assert c.endpoint_connected('ep_a') is True
    assert c.endpoint_connected('ep_b') is False
    assert c.endpoint_connected('ep_c') is False
    assert c.endpoint_plugins('ep_a') == ['sysinfo', 'queue_info', 'rhapsody']
    assert c.endpoint_plugins('ep_c') == []


# ---------------------------------------------------------------------------
# endpoint plugins
# ---------------------------------------------------------------------------

def test_sysinfo_metrics_session_round_trip(broker):

    broker.connect('ep_a')
    metrics = make_client().sysinfo_metrics('ep_a')

    assert metrics['cpu']['cores_logical'] == 16
    assert broker.paths() == ['/ep_a/sysinfo/register_session',
                              '/ep_a/sysinfo/metrics/sys-1',
                              '/ep_a/sysinfo/unregister_session/sys-1']
    # the session was closed again
    assert broker.sysinfo_sessions == []


def test_sysinfo_unregisters_even_when_metrics_fail(broker):

    broker.connect('ep_a')
    broker.errors['/sysinfo/metrics/'] = (500, 'boom')

    with pytest.raises(ClientError):
        make_client().sysinfo_metrics('ep_a')

    assert broker.paths()[-1] == '/ep_a/sysinfo/unregister_session/sys-1'


def test_job_allocation(broker):

    broker.connect('ep_a')
    c = make_client()

    # login node: the plugin answers, but with no allocation
    assert c.job_allocation('ep_a') is None

    broker.allocation = {'n_nodes': 4, 'runtime': 3600}
    assert c.job_allocation('ep_a') == {'n_nodes': 4, 'runtime': 3600}

    # plugin not loaded -> best effort, no exception
    broker.has_queue_info = False
    assert c.job_allocation('ep_a') is None


# ---------------------------------------------------------------------------
# federation routes
# ---------------------------------------------------------------------------

def test_federation_routes_use_the_default_sid(broker):

    broker.connect('ep_a')
    c = make_client()

    c.fed_join({'name': 'a', 'endpoint': 'ep_a'})
    assert broker.calls[-1]['path'] == '/broker/federation/join/default'
    assert broker.calls[-1]['method'] == 'POST'

    assert [r['name'] for r in c.fed_resources()] == ['a']
    assert broker.calls[-1]['path'] == '/broker/federation/resources/default'

    assert c.fed_resource('a')['pool_name'] == 'fed-a'
    assert broker.calls[-1]['path'] == '/broker/federation/resource/default/a'

    c.fed_leave('a')
    assert broker.calls[-1]['path'] == '/broker/federation/leave/default/a'
    assert broker.left == ['a']
    # leaving a class pool cancels nothing unless it is asked to: another
    # member can still run the tasks this resource queued
    assert broker.calls[-1]['json'] == {'cancel_tasks': False}


def test_leave_can_ask_for_the_tasks_to_be_cancelled(broker):

    broker.resources.append({'name': 'b', 'pool_name': 'fed-cpu'})
    make_client().fed_leave('b', cancel_tasks=True)

    assert broker.calls[-1]['json'] == {'cancel_tasks': True}


# ---------------------------------------------------------------------------
# pilots_of: the three payload shapes it has to render
# ---------------------------------------------------------------------------

# shape 1: the federation names the endpoint, the pilot kind and the time
# left per row, and derives `idle` itself
SHAPED = {
    'name': 'perlmutter', 'endpoint': 'ep_perlmutter', 'mode': 'login',
    'site': 'NERSC', 'state': 'ok',
    'members': [
        {'member': 'cpu', 'member_id': 'perlmutter.cpu',
         'endpoint': 'ep_perlmutter', 'pilot': 'submit', 'class': 'cpu',
         'pool_name': 'fed-cpu', 'nodes': 4, 'cpus_per_node': 128,
         'gpus_per_node': 0, 'walltime_sec': 3600, 'remaining_sec': 2520,
         'attributes': {'site': 'NERSC', 'mem_gb_per_node': 512},
         'usage': {'tasks_running': 4, 'tasks_done': 9, 'tasks_failed': 1,
                   'pilots_active': 2},
         'liveness': 'ok', 'state': 'ok'},
        {'member': 'gpu', 'member_id': 'perlmutter.gpu',
         'endpoint': 'ep_perlmutter', 'pilot': 'submit', 'class': 'gpu',
         'pool_name': 'fed-gpu', 'nodes': 1, 'cpus_per_node': 64,
         'gpus_per_node': 4, 'walltime_sec': 1800, 'remaining_sec': None,
         'attributes': {'site': 'NERSC', 'mem_gb_per_node': 256},
         'usage': {'tasks_running': 0, 'tasks_done': 2, 'tasks_failed': 0,
                   'pilots_active': 0},
         'liveness': 'ok', 'state': 'idle'}]}

ALLOCATED = {
    'name': 'odo', 'endpoint': 'ep_odo', 'mode': 'allocation',
    'site': 'OLCF', 'state': 'ok',
    'members': [
        {'member': 'default', 'member_id': 'odo.default',
         'endpoint': 'ep_odo', 'pilot': 'endpoint', 'class': 'gpu',
         'pool_name': 'fed-gpu', 'nodes': 2, 'cpus_per_node': 112,
         'gpus_per_node': 8, 'walltime_sec': 5400, 'remaining_sec': 4140,
         'attributes': {'site': 'OLCF', 'mem_gb_per_node': 256},
         'usage': {'tasks_running': 0, 'tasks_done': 3, 'tasks_failed': 0,
                   'pilots_active': 1},
         'liveness': 'ok', 'state': 'ok'}]}

# shape 2: the same two resources, from a federation that reports rows
# without `endpoint`, `pilot`, `remaining_sec` or a state word
UNSHAPED = {
    'name': 'perlmutter', 'endpoint': 'ep_perlmutter', 'mode': 'login',
    'site': 'NERSC', 'liveness': 'ok',
    'members': [
        {'member': 'cpu', 'member_id': 'perlmutter.cpu', 'class': 'cpu',
         'pool_name': 'fed-cpu', 'nodes': 4, 'cpus_per_node': 128,
         'gpus_per_node': 0, 'walltime_sec': 3600,
         'attributes': {'mem_gb_per_node': 512},
         'usage': {'tasks_running': 4, 'tasks_done': 9, 'tasks_failed': 1,
                   'pilots_active': 2},
         'liveness': 'ok'},
        {'member': 'gpu', 'member_id': 'perlmutter.gpu', 'class': 'gpu',
         'pool_name': 'fed-gpu', 'nodes': 1, 'cpus_per_node': 64,
         'gpus_per_node': 4, 'walltime_sec': 1800,
         'attributes': {'mem_gb_per_node': 256},
         'usage': {'tasks_running': 0, 'tasks_done': 2, 'tasks_failed': 0,
                   'pilots_active': 0},
         'liveness': 'ok'}]}

UNSHAPED_ALLOC = {
    'name': 'odo', 'endpoint': 'ep_odo', 'mode': 'allocation',
    'site': 'OLCF', 'liveness': 'ok',
    'members': [
        {'member': 'default', 'member_id': 'odo.default', 'class': 'gpu',
         'pool_name': 'fed-gpu', 'nodes': 2, 'cpus_per_node': 112,
         'gpus_per_node': 8, 'walltime_sec': 5400,
         'attributes': {'mem_gb_per_node': 256},
         'usage': {'tasks_running': 0, 'tasks_done': 3, 'tasks_failed': 0,
                   'pilots_active': 1},
         'liveness': 'ok'}]}

# shape 3: a record from a broker that predates class pools -- no rows
FLAT = {'name': 'local_a', 'mode': 'allocation', 'endpoint': 'ep_local_a',
        'site': 'Rutgers', 'kind': 'workstation',
        'pool_name': 'fed-local_a',
        'capabilities': {'cores': 4, 'gpus': 0, 'mem_gb': 8.0,
                         'software': ['lammps']},
        'budget': {'node_hours': 4.0},
        'usage': {'pilots_active': 1, 'tasks_failed': 2},
        'liveness': 'ok'}


def test_pilots_of_takes_shape_one_as_it_comes():

    cpu, gpu = client.pilots_of(SHAPED)

    assert cpu['pilot_name']    == 'ep_perlmutter/cpu'
    assert cpu['pilot']         == 'submit'
    assert cpu['mode']          == 'login'
    assert cpu['remaining_sec'] == 2520.0
    assert cpu['state']         == 'ok'
    assert cpu['mem_gb_per_node'] == 512

    # the federation says this shape holds no pilot: believed, not derived
    assert gpu['pilot_name']    == 'ep_perlmutter/gpu'
    assert gpu['state']         == 'idle'
    assert gpu['remaining_sec'] is None
    assert not any(p.get('derived') for p in (cpu, gpu))


def test_pilots_of_names_an_allocation_after_its_endpoint():

    pilot, = client.pilots_of(ALLOCATED)

    assert pilot['pilot_name']    == 'ep_odo'
    assert pilot['pilot']         == 'endpoint'
    assert pilot['mode']          == 'alloc'
    assert pilot['remaining_sec'] == 4140.0
    assert pilot['usage']['tasks_failed'] == 0


def test_pilots_of_fills_shape_two_in_from_the_record():

    cpu, gpu = client.pilots_of(UNSHAPED)

    # no `endpoint` on the row: the record's serves both
    assert cpu['pilot_name']    == 'ep_perlmutter/cpu'
    assert gpu['pilot_name']    == 'ep_perlmutter/gpu'
    # no `pilot` on the row: login mode means the pilots are submitted
    assert [p['pilot'] for p in (cpu, gpu)] == ['submit', 'submit']
    # nothing reports a time left, and none is invented
    assert cpu['remaining_sec'] is None
    # ... and the shape that holds no pilot and has not failed is idle
    assert cpu['state'] == 'ok'
    assert gpu['state'] == 'idle'


def test_pilots_of_derives_the_kind_from_an_allocation_record():

    pilot, = client.pilots_of(UNSHAPED_ALLOC)

    assert pilot['pilot_name'] == 'ep_odo'
    assert pilot['pilot']      == 'endpoint'
    assert pilot['mode']       == 'alloc'
    assert pilot['state']      == 'ok'


def test_a_failing_shape_is_not_called_idle():

    record = json.loads(json.dumps(UNSHAPED))
    record['members'][1]['usage'].update({'pilot_error'   : 'quota',
                                          'pilot_failures': 5})
    record['state'] = 'failing'

    assert client.pilots_of(record)[1]['state'] == 'failing'


def test_a_shape_on_a_dead_endpoint_is_not_called_idle():

    # liveness comes first: a lost site holds no pilot either, and `idle`
    # would say the resource is fine and merely quiet
    record = {'name': 'x', 'endpoint': 'ep_x', 'mode': 'login',
              'members': [{'member': 'cpu', 'liveness': 'lost',
                           'usage': {'pilots_active': 0}}]}

    assert client.pilots_of(record)[0]['state'] == 'lost'

    # ... and the same word off the record, where the row carries none
    record['members'][0].pop('liveness')
    record['liveness'] = 'suspect'

    assert client.pilots_of(record)[0]['state'] == 'suspect'


def test_only_a_real_failure_run_is_called_failing():

    def state(usage):
        record = {'name': 'x', 'endpoint': 'ep_x', 'mode': 'login',
                  'liveness': 'ok',
                  'members': [{'member': 'cpu', 'liveness': 'ok',
                               'usage': dict(usage, pilots_active=0)}]}
        return client.pilots_of(record)[0]['state']

    # one bad submit is not a failing shape -- the federation waits for
    # three, or for a backoff it has actually started
    assert state({})                        == 'idle'
    assert state({'pilot_failures': 1})     == 'idle'
    assert state({'pilot_failures': 3})     == 'failing'
    assert state({'paused_until': 1.0})     == 'idle'      # long past
    assert state({'paused_until': 4e9})     == 'failing'   # still backing off
    # a reason without a counter is the case the line was written for
    assert state({'pilot_error': 'quota'})  == 'failing'


def test_the_memory_per_node_may_come_off_the_row_itself():

    record = {'name': 'x', 'mode': 'login',
              'members': [{'member': 'cpu', 'mem_gb_per_node': 128,
                           'attributes': {'mem_gb_per_node': None}}]}

    assert client.pilots_of(record)[0]['mem_gb_per_node'] == 128


def test_pilots_of_derives_one_row_for_an_old_record():

    # a broker that predates class pools reports no rows at all; the
    # single derived one keeps every reader working
    pilot, = client.pilots_of(FLAT)

    assert pilot['member']        == 'default'
    assert pilot['member_id']     == 'local_a.default'
    assert pilot['pilot_name']    == 'ep_local_a'
    assert pilot['pilot']         == 'endpoint'
    assert pilot['mode']          == 'alloc'
    assert pilot['class']         == 'cpu'
    assert pilot['software']      == ['lammps']
    assert pilot['budget']        == {'node_hours': 4.0}
    assert pilot['shared_fs']     is True
    assert pilot['derived']       is True
    assert pilot['remaining_sec'] is None
    assert pilot['state']         == 'ok'
    assert pilot['mem_gb_per_node']                == 8.0
    assert pilot['attributes']['site']             == 'Rutgers'
    assert pilot['attributes']['mem_gb_per_node']  == 8.0
    # `tasks_failed` is on every shape, and travels untouched
    assert pilot['usage']['tasks_failed'] == 2


def test_pilots_of_tolerates_a_record_without_a_name():

    # no name, no endpoint: the row is left with the only word there is
    assert client.pilots_of({})[0]['pilot_name'] == 'default'
    assert client.pilots_of({})[0]['member_id']  == ''
    assert client.pilots_of(None)[0]['member']   == 'default'


def test_members_of_is_still_the_same_call():

    # kept as an alias for one release
    assert client.members_of is client.pilots_of
    assert [m['member_id'] for m in client.members_of(SHAPED)] \
        == ['perlmutter.cpu', 'perlmutter.gpu']


def test_member_id_joins_resource_and_member():

    # a resource name may contain dots, a member short name may not, so
    # the LAST dot is always the separator
    assert client.member_id('a.b', 'gpu') == 'a.b.gpu'
    assert client.member_id('local_b', 'gpu').rpartition('.')[0] == 'local_b'


def test_campaign_routes_use_the_default_sid(broker):

    c = make_client()

    # the fake broker hosts no campaign plugin -- we only check the URLs
    for call, path in [
            (lambda: c.campaign_submit({'name': 'w'}, {'t': [1]}),
             '/broker/atomic_campaign/campaigns/default'),
            (lambda: c.campaigns(),
             '/broker/atomic_campaign/campaigns/default'),
            (lambda: c.campaign('cid'),
             '/broker/atomic_campaign/campaign/default/cid'),
            (lambda: c.campaign_results('cid'),
             '/broker/atomic_campaign/results/default/cid'),
            (lambda: c.campaign_cancel('cid'),
             '/broker/atomic_campaign/cancel/default/cid')]:

        with pytest.raises(ClientError) as exc:
            call()

        assert broker.calls[-1]['path'] == path
        assert exc.value.status == 503


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------

def test_error_maps_status_and_detail(broker):

    broker.connect('ep_a')
    broker.errors['/federation/join/'] = (409, "name 'a' exists")

    with pytest.raises(ClientError) as exc:
        make_client().fed_join({'name': 'a'})

    assert exc.value.status == 409
    assert exc.value.detail == "name 'a' exists"
    assert '409' in str(exc.value)
    assert exc.value.url.endswith('/broker/federation/join/default')


def test_transport_error_maps_to_status_zero(monkeypatch):

    import requests

    def _boom(session, method, url, **kw):
        raise requests.ConnectionError('connection refused')

    monkeypatch.setattr(requests.Session, 'request', _boom)

    with pytest.raises(ClientError) as exc:
        make_client().endpoints()

    assert exc.value.status == 0
    assert 'cannot reach broker' in str(exc.value)


def test_tls_error_is_one_actionable_line(monkeypatch):

    import requests

    def _boom(session, method, url, **kw):
        raise requests.exceptions.SSLError(
            'HTTPSConnectionPool(host=…): Max retries exceeded … '
            'CERTIFICATE_VERIFY_FAILED … ' + 'x' * 300)

    monkeypatch.setattr(requests.Session, 'request', _boom)

    with pytest.raises(ClientError) as exc:
        make_client().endpoints()

    assert exc.value.status == 0
    assert 'pass --cert' in exc.value.detail
    assert len(exc.value.detail) < 200


def test_detail_falls_back_to_body_text():

    resp = FakeResponse(500, None, text='<html>gateway error</html>')
    assert 'gateway error' in _client._detail(resp)

    resp = FakeResponse(500, {'error': 'nope'})
    assert _client._detail(resp) == 'nope'

    resp = FakeResponse(500, None, text='')
    assert _client._detail(resp) == 'fake'      # the reason phrase


def test_empty_body_is_none():

    assert _client._body(FakeResponse(200, None, text='')) is None
    assert _client._body(FakeResponse(200, None, text='not json')) \
        == 'not json'
    assert _client._body(FakeResponse(200, {'a': 1})) == {'a': 1}

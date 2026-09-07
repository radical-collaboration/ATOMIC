"""Unit tests for ``atomic_wm.client`` -- the CLIs' HTTP client."""

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


def test_members_of_returns_the_reported_members(broker):

    record = {'name': 'local_b',
              'members': [{'member': 'cpu', 'member_id': 'local_b.cpu'},
                          {'member': 'gpu', 'member_id': 'local_b.gpu'}]}

    assert [m['member_id'] for m in client.members_of(record)] \
        == ['local_b.cpu', 'local_b.gpu']
    assert not any(m.get('derived') for m in client.members_of(record))


def test_members_of_derives_one_member_for_an_old_record():

    # a broker that predates class pools reports no members at all; the
    # single derived one keeps every reader working
    record = {'name': 'local_a', 'mode': 'allocation',
              'site': 'Rutgers', 'kind': 'workstation',
              'pool_name': 'fed-local_a',
              'capabilities': {'cores': 4, 'gpus': 0, 'mem_gb': 8.0,
                               'software': ['lammps']},
              'budget': {'node_hours': 4.0},
              'usage': {'pilots_active': 1},
              'liveness': 'ok'}

    member, = client.members_of(record)

    assert member['member']        == 'default'
    assert member['member_id']     == 'local_a.default'
    assert member['class']         == 'cpu'
    assert member['software']      == ['lammps']
    assert member['budget']        == {'node_hours': 4.0}
    assert member['shared_fs']     is True
    assert member['derived']       is True
    assert member['attributes']['site']            == 'Rutgers'
    assert member['attributes']['mem_gb_per_node'] == 8.0


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

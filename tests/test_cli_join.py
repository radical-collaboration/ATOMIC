"""Unit tests for the federation CLIs: join, leave and resources.

The broker is faked at the ``requests`` layer (``tests/fake_broker.py``)
and the endpoint child is faked at the :class:`EndpointProcess` layer, so
these tests exercise the real argument parsing, record assembly, join
flow, signal handling and teardown logic without a broker or a child
process.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from typing import Any

from atomic_wm import endpoint_proc
from atomic_wm.cli import join, leave, resources

from fake_broker import FakeBroker


# ---------------------------------------------------------------------------
# fakes and fixtures
# ---------------------------------------------------------------------------

class FakeProc:
    """Stand-in for :class:`atomic_wm.endpoint_proc.EndpointProcess`."""

    instances: list = []
    hook: Any = None            # called on start(), e.g. to connect the ep

    def __init__(self, name, url, endpoint=None, token=None, cert=None,
                 plugins='default', log_level='INFO', binary=None):

        self.name       = name
        self.url        = url
        self.endpoint   = endpoint or endpoint_proc.endpoint_name(name)
        self.token      = token
        self.cert       = cert
        self.plugins    = plugins
        self.log_level  = log_level
        self.binary     = binary

        self.log        = '/tmp/fake-endpoint.log'
        self.started    = False
        self.stopped    = False
        self.detached   = False
        self.alive      = True
        self.returncode = None

        FakeProc.instances.append(self)

    def start(self):

        self.started = True
        hook = type(self).hook
        if hook:
            hook(self)

        return 4242

    @property
    def pid(self):
        return 4242 if self.started else None

    def is_alive(self):
        return self.alive and not self.stopped

    def stop(self, timeout=10.0):
        self.stopped = True
        self.alive   = False

    def detach(self):
        self.detached = True

    def log_tail(self, lines=15):
        return 'fake endpoint log'


@pytest.fixture
def broker(monkeypatch):

    return FakeBroker().install(monkeypatch)


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """State under tmp_path, no /proc scans, short poll intervals."""

    monkeypatch.setenv(endpoint_proc.ENV_STATE_ROOT, str(tmp_path / 'state'))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))

    for var in ['RADICAL_ORBIT_BROKER_URL', 'RADICAL_ORBIT_TOKEN',
                'RADICAL_ORBIT_BROKER_TOKEN', 'RADICAL_ORBIT_BROKER_CERT']:
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setattr(endpoint_proc, 'iter_processes', lambda: [])
    monkeypatch.setattr(join, 'POLL_INTERVAL', 0.01)
    monkeypatch.setattr(join, 'LOOP_INTERVAL', 0.01)
    monkeypatch.setattr(join, 'LIVENESS_INTERVAL', 0.02)


@pytest.fixture
def proc(monkeypatch, broker):
    """Fake endpoint child; connects itself to the fake broker on start."""

    FakeProc.instances = []
    FakeProc.hook      = lambda p: broker.connect(p.endpoint)
    monkeypatch.setattr(join, 'EndpointProcess', FakeProc)

    return FakeProc


ARGS_LOCAL = ['--broker', 'https://127.0.0.1:8013', '--token', '',
              '--name', 'local_a', '--mode', 'allocation',
              '--declare', 'cores=4', '--software', 'lammps',
              '--connect-timeout', '2']


def pidfile(name):
    """The pidfile of `name` -- asserting there is one."""

    info = endpoint_proc.read_pidfile(name)
    assert info is not None

    return info


def parsed(argv):
    """Parse + validate a command line (as ``main`` does)."""

    return join.validate(join.build_parser().parse_args(argv))


# ---------------------------------------------------------------------------
# argument parsing and validation
# ---------------------------------------------------------------------------

def test_declare_parsing():

    assert join.parse_declare('cores=4,gpus=0,mem_gb=8.5') \
        == {'cores': 4, 'gpus': 0, 'mem_gb': 8.5}
    assert join.parse_declare(None) == {}
    assert join.parse_declare('') == {}


@pytest.mark.parametrize('text', ['cores', 'nodes=2', 'cores=many',
                                  'cores=-1'])
def test_declare_rejects_junk(text):

    with pytest.raises(join.UsageError):
        join.parse_declare(text)


def test_software_flattens_and_dedupes():

    assert join.parse_software(['lammps,pytorch', 'lammps', ' vasp ']) \
        == ['lammps', 'pytorch', 'vasp']
    assert join.parse_software([]) == []


@pytest.mark.parametrize('name', ['Local_A', 'a b', '_a', 'a/b', ''])
def test_name_is_validated(name):

    with pytest.raises(join.UsageError):
        parsed(['--broker', 'https://x', '--name', name,
                '--mode', 'allocation'])


def test_allocation_mode_rejects_login_flags():

    with pytest.raises(join.UsageError) as exc:
        parsed(['--broker', 'https://x', '--name', 'a', '--mode',
                'allocation', '--queue', 'RM'])

    assert '--queue' in str(exc.value)


def test_login_mode_requires_the_pilot_description():

    with pytest.raises(join.UsageError) as exc:
        parsed(['--broker', 'https://x', '--name', 'a', '--mode', 'login',
                '--queue', 'RM'])

    for flag in ['--nodes', '--cpus', '--walltime']:
        assert flag in str(exc.value)


def test_login_mode_requires_node_hours():

    with pytest.raises(join.UsageError) as exc:
        parsed(['--broker', 'https://x', '--name', 'a', '--mode', 'login',
                '--queue', 'RM', '--nodes', '1', '--cpus', '128',
                '--walltime', '3600'])

    assert '--node-hours' in str(exc.value)


def test_login_mode_rejects_the_dispatcher_sentinel_queue():

    with pytest.raises(join.UsageError):
        parsed(['--broker', 'https://x', '--name', 'a', '--mode', 'login',
                '--queue', 'default', '--nodes', '1', '--cpus', '8',
                '--walltime', '3600', '--node-hours', '4'])


def test_login_mode_accepts_a_complete_description():

    args = parsed(['--broker', 'https://x', '--name', 'a', '--mode', 'login',
                   '--site', 'PSC', '--queue', 'RM', '--account', 'abc123',
                   '--nodes', '1', '--cpus', '128', '--walltime', '3600',
                   '--node-hours', '20', '--software', 'lammps'])

    assert args.queue == 'RM'
    assert args.software == ['lammps']


def test_node_hours_must_be_positive():

    with pytest.raises(join.UsageError):
        parsed(['--broker', 'https://x', '--name', 'a', '--mode',
                'allocation', '--node-hours', '0'])


def test_scratch_must_be_under_home_or_tmp():

    args = parsed(['--broker', 'https://x', '--name', 'a', '--mode',
                   'allocation', '--scratch', '/tmp/atomic-demo/a'])
    assert args.scratch == '/tmp/atomic-demo/a'

    with pytest.raises(join.UsageError):
        parsed(['--broker', 'https://x', '--name', 'a', '--mode',
                'allocation', '--scratch', '/etc/atomic'])


def test_main_reports_usage_errors_with_exit_2(capsys):

    rc = join.main(['--broker', 'https://x', '--name', 'a', '--mode',
                    'allocation', '--declare', 'cores=many'])

    assert rc == 2
    assert 'not a number' in capsys.readouterr().err


# ---------------------------------------------------------------------------
# capability detection and record assembly
# ---------------------------------------------------------------------------

def test_capabilities_from_metrics():

    caps = join.capabilities_from_metrics(
        {'cpu'   : {'cores_logical': 16},
         'memory': {'total': 32 * 1024 ** 3},
         'gpus'  : [{'vendor': 'NVIDIA'}]})

    assert caps == {'cores': 16, 'mem_gb': 32.0, 'gpus': 1}
    assert join.capabilities_from_metrics({}) == {}


def test_declared_beats_detected():

    args   = parsed(ARGS_LOCAL)                     # declares cores=4
    record = join.assemble_record(args,
                                  detected={'cores': 64, 'gpus': 2,
                                            'mem_gb': 128.0})

    assert record['capabilities'] == {'cores': 4, 'gpus': 2, 'mem_gb': 128.0,
                                      'software': ['lammps']}


def test_allocation_mode_leaves_the_budget_to_the_federation():

    args = parsed(['--broker', 'https://x', '--name', 'a', '--mode',
                   'allocation'])

    # no --node-hours: the federation derives nodes x walltime from the
    # allocation itself, so we must not send a budget at all
    rec = join.assemble_record(args, {'cores': 8},
                               {'n_nodes': 2, 'runtime': 1800})
    assert 'budget' not in rec


def test_declared_budget_is_sent():

    args = parsed(['--broker', 'https://x', '--name', 'a', '--mode',
                   'allocation', '--node-hours', '12.5'])

    rec = join.assemble_record(args, {'cores': 8},
                               {'n_nodes': 2, 'runtime': 1800})
    assert rec['budget'] == {'node_hours': 12.5}


def test_allocation_record_shape():

    args = parsed(ARGS_LOCAL + ['--site', 'local', '--kind', 'workstation',
                                '--scratch', '/tmp/atomic-demo/local_a'])
    rec  = join.assemble_record(args, {'gpus': 0, 'mem_gb': 8.0})

    assert rec == {'name'        : 'local_a',
                   'endpoint'    : 'ep_local_a',
                   'mode'        : 'allocation',
                   'site'        : 'local',
                   'kind'        : 'workstation',
                   'capabilities': {'cores': 4, 'gpus': 0, 'mem_gb': 8.0,
                                    'software': ['lammps']},
                   'scratch_base': '/tmp/atomic-demo/local_a'}


def test_login_record_carries_the_pool():

    args = parsed(['--broker', 'https://x', '--name', 'b', '--mode', 'login',
                   '--queue', 'RM', '--account', 'abc123', '--nodes', '2',
                   '--cpus', '128', '--gpus-per-node', '4',
                   '--walltime', '3600', '--node-hours', '20'])
    rec  = join.assemble_record(args)

    assert rec['pool'] == {'queue'           : 'RM',
                           'account'         : 'abc123',
                           'nodes'           : 2,
                           'cpus_per_node'   : 128,
                           'gpus_per_node'   : 4,
                           'walltime_sec'    : 3600,
                           'max_pilots'      : 2,
                           'rhapsody_backend': 'concurrent'}
    assert rec['budget'] == {'node_hours': 20.0}

    # login-mode capabilities describe one pilot, not the login node
    assert rec['capabilities'] == {'cores': 256, 'gpus': 8,
                                   'software': []}


def test_login_declaration_beats_the_derived_pilot_size():

    args = parsed(['--broker', 'https://x', '--name', 'b', '--mode', 'login',
                   '--queue', 'RM', '--nodes', '2', '--cpus', '128',
                   '--walltime', '3600', '--node-hours', '20',
                   '--declare', 'cores=64,mem_gb=512'])
    rec  = join.assemble_record(args)

    assert rec['capabilities'] == {'cores': 64, 'gpus': 0, 'mem_gb': 512.0,
                                   'software': []}


def test_login_mode_does_not_probe_the_login_node(broker):

    broker.connect('ep_a')

    detected, alloc = join.detect(_client(broker), 'ep_a', 'login', {})

    assert detected == {}
    assert alloc is None
    assert broker.calls == []                   # no sysinfo, no queue_info


def test_detection_is_skipped_when_everything_is_declared(broker):

    broker.connect('ep_a')

    detected, alloc = join.detect(_client(broker), 'ep_a', 'allocation',
                                  {'cores': 1, 'gpus': 0, 'mem_gb': 2})

    assert detected == {}
    assert alloc is None
    # the allocation is still asked for (the federation sizes the pool
    # from it), but sysinfo is not
    assert broker.paths() == ['/ep_a/queue_info/job_allocation']


def test_detection_failure_is_a_warning(broker, capsys):

    broker.connect('ep_a')
    broker.errors['/sysinfo/'] = (500, 'no psutil')

    detected, _ = join.detect(_client(broker), 'ep_a', 'allocation', {})

    assert detected == {}
    assert 'sysinfo failed' in capsys.readouterr().err


def _client(broker):

    from atomic_wm.client import Client

    return Client(broker='https://127.0.0.1:8013', token='')


# ---------------------------------------------------------------------------
# the join flow
# ---------------------------------------------------------------------------

def test_join_flow_detached(broker, proc, capsys):

    rc = join.main(ARGS_LOCAL + ['--site', 'local', '--detach',
                                 '--scratch', '/tmp/atomic-demo/local_a'])

    assert rc == 0

    # the endpoint was started and left running
    assert proc.instances[0].started
    assert proc.instances[0].detached
    assert not proc.instances[0].stopped

    # sysinfo was asked (partial --declare) and the session closed again
    assert '/ep_local_a/sysinfo/register_session' in broker.paths()
    assert broker.sysinfo_sessions == []

    # the join body is exactly the assembled record
    assert broker.joined == [{
        'name'        : 'local_a',
        'endpoint'    : 'ep_local_a',
        'mode'        : 'allocation',
        'site'        : 'local',
        'kind'        : 'workstation',
        'capabilities': {'cores': 4, 'gpus': 2, 'mem_gb': 32.0,
                         'software': ['lammps']},
        'scratch_base': '/tmp/atomic-demo/local_a'}]

    # ... and a pidfile was written for atomic-leave
    info = pidfile('local_a')
    assert info['pid']      == 4242
    assert info['endpoint'] == 'ep_local_a'

    out = capsys.readouterr().out
    assert 'joined federation' in out
    assert 'atomic-leave local_a' in out


def test_join_reports_the_allocation_and_the_returned_budget(broker, proc,
                                                             capsys):

    broker.allocation = {'n_nodes': 2, 'runtime': 7200}

    rc = join.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                    '--name', 'alloc_a', '--mode', 'allocation', '--detach',
                    '--connect-timeout', '2'])

    assert rc == 0
    assert 'budget' not in broker.joined[0]                  # server derives
    assert broker.joined[0]['capabilities']['cores'] == 16   # detected

    out = capsys.readouterr().out
    assert '2 node(s), 7200 s remaining' in out
    # ... and the budget printed is the one the federation came back with
    assert 'node-hours   : 2.0 (derived from the allocation)' in out


def test_endpoint_never_connects(broker, proc, monkeypatch, capsys):

    FakeProc.hook = None                       # never shows up as connected

    rc = join.main(ARGS_LOCAL + ['--connect-timeout', '0.2'])

    assert rc == 1
    assert proc.instances[0].stopped            # the child was killed
    assert broker.joined == []

    err = capsys.readouterr().err
    assert 'did not connect' in err
    assert 'fake endpoint log' in err           # the log tail is shown


def test_endpoint_dies_while_connecting(broker, proc, capsys):

    def _die(p):
        p.alive      = False
        p.returncode = 1

    FakeProc.hook = _die

    rc = join.main(ARGS_LOCAL)

    assert rc == 1
    assert 'died while connecting' in capsys.readouterr().err


@pytest.mark.parametrize('status,detail', [
    (503, 'plugin not hosted'),
    (404, 'No route: POST /federation/join/default')])
def test_join_without_a_federation_plugin(broker, proc, capsys,
                                          status, detail):

    broker.errors['/federation/join/'] = (status, detail)

    rc = join.main(ARGS_LOCAL)

    assert rc == 1
    assert proc.instances[0].stopped
    assert "does not host the 'federation' plugin" in capsys.readouterr().err


def test_join_without_a_task_dispatcher(broker, proc, capsys):

    # the federation answered -- it is the dispatcher that is missing
    broker.errors['/federation/join/'] = (503, 'dispatcher plugin not '
                                               'available')

    rc = join.main(ARGS_LOCAL)
    err = capsys.readouterr().err

    assert rc == 1
    assert 'no task dispatcher' in err
    assert '--plugins task_dispatcher,federation' in err


def test_join_404_from_the_plugin_points_at_the_endpoint(broker, proc,
                                                         capsys):

    # the plugin answered -- it is the endpoint it cannot see
    broker.errors['/federation/join/'] = (404, "endpoint 'ep_local_a' "
                                               "is not connected")

    rc = join.main(ARGS_LOCAL)

    err = capsys.readouterr().err

    assert rc == 1
    assert "does not host the 'federation' plugin" not in err
    assert 'does not see the endpoint as connected' in err
    assert 'endpoint log' in err


def test_duplicate_name_is_explained(broker, proc, capsys):

    broker.resources.append({'name': 'local_a'})

    rc = join.main(ARGS_LOCAL)

    assert rc == 1
    assert 'already in the federation' in capsys.readouterr().err


def test_join_needs_a_core_count(broker, proc, capsys):

    broker.metrics = {}                        # sysinfo knows nothing

    rc = join.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                    '--name', 'nocores', '--mode', 'allocation',
                    '--connect-timeout', '2'])

    assert rc == 1
    assert broker.joined == []
    assert '--declare cores=' in capsys.readouterr().err


def test_sigint_leaves_the_federation(broker, proc, monkeypatch):

    handlers = {}
    monkeypatch.setattr(signal, 'signal',
                        lambda sig, handler: handlers.setdefault(sig,
                                                                 handler))

    def fire():
        assert broker.joined_event.wait(10)
        deadline = time.time() + 10
        while signal.SIGINT not in handlers and time.time() < deadline:
            time.sleep(0.01)
        handlers[signal.SIGINT](int(signal.SIGINT), None)

    thread = threading.Thread(target=fire)
    thread.start()
    try:
        rc = join.main(ARGS_LOCAL)
    finally:
        thread.join(15)

    assert rc == 0
    assert broker.left == ['local_a']            # leave/default/local_a
    assert proc.instances[0].stopped
    # a clean teardown leaves no pidfile behind
    assert endpoint_proc.read_pidfile('local_a') is None


def test_sigint_during_the_connect_wait_stops_the_endpoint(broker, proc,
                                                           monkeypatch,
                                                           capsys):

    handlers = {}
    monkeypatch.setattr(signal, 'signal',
                        lambda sig, handler: handlers.setdefault(sig,
                                                                 handler))
    FakeProc.hook = None                        # never connects

    def fire():
        deadline = time.time() + 10
        while signal.SIGINT not in handlers and time.time() < deadline:
            time.sleep(0.01)
        handlers[signal.SIGINT](int(signal.SIGINT), None)

    thread = threading.Thread(target=fire)
    thread.start()
    try:
        # the connect timeout is long: only the signal can end this
        rc = join.main(ARGS_LOCAL + ['--connect-timeout', '30'])
    finally:
        thread.join(15)

    assert rc == 1
    assert proc.instances[0].stopped            # no orphaned endpoint
    assert broker.joined == []
    assert endpoint_proc.read_pidfile('local_a') is None
    assert 'nothing was joined' in capsys.readouterr().out


def test_pidfile_exists_before_the_join_completes(broker, proc, monkeypatch):

    # the orphan window: the pidfile must be there before the (up to
    # 60 s) connect wait, or a SIGKILL during it leaves behind an
    # endpoint nobody can find
    seen     = {}
    original = join.wait_connected

    def _spy(*args, **kw):
        seen['pidfile'] = endpoint_proc.read_pidfile('local_a')
        return original(*args, **kw)

    monkeypatch.setattr(join, 'wait_connected', _spy)

    assert join.main(ARGS_LOCAL + ['--detach']) == 0
    assert seen['pidfile']['pid'] == 4242


def test_http_broker_url_is_flagged(broker, proc, capsys):

    args = [a if a != 'https://127.0.0.1:8013' else 'http://127.0.0.1:8013'
            for a in ARGS_LOCAL]

    assert join.main(args + ['--detach']) == 0
    assert 'plain http' in capsys.readouterr().err


def test_foreground_notices_a_dead_endpoint(broker, proc, monkeypatch):

    def die_later(p):
        broker.connect(p.endpoint)
        threading.Timer(0.1, _kill, args=[p]).start()

    def _kill(p):
        p.alive      = False
        p.returncode = 1

    FakeProc.hook = die_later

    rc = join.main(ARGS_LOCAL)

    assert rc == 1
    assert broker.left == ['local_a']            # still tidied up


# ---------------------------------------------------------------------------
# atomic-leave
# ---------------------------------------------------------------------------

class FakeProcTable:
    """A tiny process table with recording kill/alive functions."""

    def __init__(self, procs):

        self.procs  = dict(procs)               # pid -> cmdline
        self.killed = []                        # (pid, signal)

    def kill(self, pid, sig):

        self.killed.append((pid, sig))

        if pid not in self.procs:
            raise ProcessLookupError(pid)

        if sig in (signal.SIGTERM, signal.SIGKILL):
            self.procs.pop(pid)

    def alive(self, pid):

        return pid in self.procs

    def listing(self):

        return sorted(self.procs.items())

    def cmdline(self, pid):

        return self.procs.get(pid, '')


@pytest.fixture
def table(monkeypatch):

    def _make(procs):
        tbl = FakeProcTable(procs)
        monkeypatch.setattr(endpoint_proc, 'iter_processes', tbl.listing)
        monkeypatch.setattr(endpoint_proc, 'pid_alive', tbl.alive)
        monkeypatch.setattr(endpoint_proc, 'cmdline_of', tbl.cmdline)
        monkeypatch.setattr(os, 'kill', tbl.kill)
        return tbl

    return _make


# a pilot's child endpoint is `<pool>_<pilot id>` = `fed-<name>_p.<hex>`
PILOT_A = ('/ve/bin/python /ve/bin/radical-orbit-endpoint.py '
           '-n fed-local_a_p.a1b2c3d4e5 --plugins default')
PILOT_L = ('/ve/bin/python /ve/bin/radical-orbit-endpoint.py '
           '-n fed-local_p.0f0f0f0f0f --plugins default')
EP_A    = ('/ve/bin/python /ve/bin/radical-orbit-endpoint.py '
           '--name ep_local_a --url https://b:1 --plugins default')


def test_pilot_pids_matches_only_this_resources_pilots():

    procs = [(10, PILOT_A),
             (11, PILOT_L),
             (12, EP_A),
             (13, 'vim fed-local_a_p.a1b2c3d4e5.log')]

    # `local` must not claim `local_a`'s pilots (and vice versa) -- the
    # name is matched as a whole argument, not as a prefix
    assert endpoint_proc.pilot_pids('local_a', procs) == [10]
    assert endpoint_proc.pilot_pids('local',   procs) == [11]
    assert endpoint_proc.pilot_pids('other',   procs) == []


def test_endpoint_pids_matches_the_whole_name():

    procs = [(10, PILOT_A), (12, EP_A),
             (14, EP_A.replace('ep_local_a', 'ep_local_ab'))]

    assert endpoint_proc.endpoint_pids('ep_local_a', procs)  == [12]
    assert endpoint_proc.endpoint_pids('ep_local_ab', procs) == [14]
    assert endpoint_proc.endpoint_pids('ep_nope', procs)     == []


def test_leave_stops_endpoint_and_pilots(broker, table, capsys):

    broker.resources.append({'name': 'local_a'})
    endpoint_proc.write_pidfile('local_a', 100, 'ep_local_a')

    tbl = table({100: EP_A,
                 200: PILOT_A,
                 300: PILOT_A.replace('fed-local_a_', 'fed-other_')})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'local_a'])

    assert rc == 0
    assert broker.left == ['local_a']

    killed = [pid for pid, sig in tbl.killed if sig == signal.SIGTERM]
    assert killed == [100, 200]                 # not the other resource's
    assert 300 in tbl.procs

    # the pidfile is gone, so a second leave is harmless
    assert endpoint_proc.read_pidfile('local_a') is None
    assert 'terminated 1 surviving pilot' in capsys.readouterr().out


def test_leave_without_a_pidfile(broker, table, capsys):

    broker.resources.append({'name': 'local_a'})
    table({})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'local_a'])

    assert rc == 0
    assert broker.left == ['local_a']
    assert 'no pidfile' in capsys.readouterr().err


def test_leave_finds_the_endpoint_without_a_pidfile(broker, table, capsys):

    # the CLI was killed before it could write a pidfile -- the endpoint
    # is still found in the process table
    broker.resources.append({'name': 'local_a'})
    tbl = table({100: EP_A, 200: PILOT_A})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'local_a'])

    assert rc == 0
    assert [pid for pid, sig in tbl.killed if sig == signal.SIGTERM] \
        == [100, 200]
    assert 'found endpoint ep_local_a in the process table' \
        in capsys.readouterr().out


def test_leave_ignores_a_stale_pidfile(broker, table, capsys):

    broker.resources.append({'name': 'local_a'})
    endpoint_proc.write_pidfile('local_a', 100, 'ep_local_a')

    # pid 100 was reused by something else entirely
    tbl = table({100: '/usr/bin/rsync -a /data /backup'})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'local_a'])

    assert rc == 0
    assert tbl.killed == []                     # the innocent pid survives
    assert 'pidfile stale' in capsys.readouterr().err
    assert endpoint_proc.read_pidfile('local_a') is None


def test_leave_tolerates_an_unknown_resource(broker, table):

    table({})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'gone'])

    assert rc == 0                              # 404 == already left


def test_leave_without_a_federation_plugin(broker, table, capsys):

    broker.errors['/federation/leave/'] = (404, 'No route: POST '
                                                '/federation/leave/default/x')
    table({})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'local_a'])

    assert rc == 1                              # not 'already gone'
    assert "does not host the 'federation' plugin" in capsys.readouterr().err


def test_leave_reports_a_broker_error(broker, table, capsys):

    broker.errors['/federation/leave/'] = (500, 'boom')
    endpoint_proc.write_pidfile('local_a', 100, 'ep_local_a')
    table({100: EP_A})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'local_a'])

    assert rc == 1                              # the failure is visible
    assert endpoint_proc.read_pidfile('local_a') is None   # ... but we tidied


def test_leave_can_keep_the_endpoint(broker, table):

    broker.resources.append({'name': 'local_a'})
    endpoint_proc.write_pidfile('local_a', 100, 'ep_local_a')
    tbl = table({100: EP_A})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'local_a', '--keep-endpoint'])

    assert rc == 0
    assert tbl.killed == []


def test_detach_then_leave_round_trip(broker, proc, table, capsys):

    rc = join.main(ARGS_LOCAL + ['--detach'])
    assert rc == 0
    assert pidfile('local_a')['pid'] == 4242

    capsys.readouterr()

    # the detached endpoint, as `atomic-leave` will find it: same pid,
    # same pidfile, plus a pilot it spawned meanwhile
    tbl = table({4242: EP_A, 4300: PILOT_A})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'local_a'])

    assert rc == 0
    assert broker.left      == ['local_a']
    assert broker.resources == []
    assert [pid for pid, sig in tbl.killed if sig == signal.SIGTERM] \
        == [4242, 4300]
    assert endpoint_proc.read_pidfile('local_a') is None


# ---------------------------------------------------------------------------
# the real child process (no fakes below this line)
# ---------------------------------------------------------------------------

STUB = """\
import sys, time
sys.stderr.write('stub endpoint: %s\\n' % ' '.join(sys.argv[1:]))
sys.stderr.flush()
time.sleep(60)
"""


def test_real_endpoint_process_start_and_stop(tmp_path):

    stub = tmp_path / 'radical-orbit-endpoint.py'
    stub.write_text(STUB)

    ep = endpoint_proc.EndpointProcess('real_a', 'https://127.0.0.1:1',
                                       binary=str(stub))
    pid = ep.start()
    try:
        assert pid > 0
        assert ep.is_alive()
        assert ep.returncode is None

        # the log carries the command header and the child's own output
        deadline = time.time() + 5
        while 'stub endpoint' not in ep.log_tail(50) and time.time() < deadline:
            time.sleep(0.05)

        tail = ep.log_tail(50)
        assert str(stub) in tail                       # the header line
        assert '--name ep_real_a' in tail
        assert 'stub endpoint' in tail                 # stdout/err captured

        # ... and it runs in its own session, so a Ctrl-C on our terminal
        # does not race the orderly shutdown
        assert os.getsid(pid) == pid

    finally:
        ep.stop(timeout=5)

    assert not ep.is_alive()
    assert ep.returncode is not None                   # reaped, no zombie
    assert not endpoint_proc.pid_alive(pid) or _is_zombie(pid)


def _is_zombie(pid):

    try:
        with open('/proc/%d/stat' % pid, 'r', encoding='utf-8') as fin:
            return fin.read().rsplit(')', 1)[-1].split()[0] == 'Z'
    except OSError:
        return False


def test_kill_pids_terminates_a_real_child():

    child = subprocess.Popen([sys.executable, '-c',
                              'import time; time.sleep(60)'])
    try:
        # a killed child of *ours* stays a zombie until it is reaped, and
        # a zombie still answers `kill(pid, 0)` -- so reap it in parallel,
        # the way the real target (a pilot, not our child) disappears
        reaper = threading.Thread(target=child.wait)
        reaper.start()

        gone = endpoint_proc.kill_pids([child.pid], timeout=5)

        reaper.join(5)
        assert child.returncode == -signal.SIGTERM
        assert gone == [child.pid]

    finally:
        if child.poll() is None:                       # pragma: no cover
            child.kill()
            child.wait(5)


# ---------------------------------------------------------------------------
# the endpoint child's environment and command line
# ---------------------------------------------------------------------------

def test_child_env_follows_the_spike_rules(monkeypatch):

    monkeypatch.setenv('RADICAL_LOG_LVL', 'DEBUG_9')
    monkeypatch.setenv('RADICAL_ORBIT_LOG_FILE', '/tmp/nope.log')
    monkeypatch.setenv('RADICAL_ORBIT_RHAPSODY_BACKEND', 'concurrent')
    monkeypatch.setenv('PATH', '/usr/bin:/bin')

    env = endpoint_proc.child_env(log_level='DEBUG')

    import sys
    vbin = os.path.dirname(os.path.abspath(sys.executable))

    assert env['PATH'].split(os.pathsep)[0] == vbin
    assert 'RADICAL_LOG_LVL' not in env
    assert 'RADICAL_ORBIT_LOG_FILE' not in env
    assert env['RADICAL_ORBIT_LOG_LVL'] == 'DEBUG'
    assert env['RADICAL_ORBIT_RHAPSODY_BACKEND'] == 'concurrent'


def test_command_line(monkeypatch, tmp_path):

    binary = tmp_path / 'radical-orbit-endpoint.py'
    binary.write_text('#!/usr/bin/env python3\n')

    argv = endpoint_proc.command('ep_a', 'https://host:8013',
                                 token='tok', cert='/tmp/cert.pem',
                                 plugins='default', log_level='info',
                                 binary=str(binary))

    assert argv[1] == str(binary)
    assert argv[2:] == ['--name', 'ep_a', '--url', 'https://host:8013',
                        '--plugins', 'default', '--log-level', 'INFO',
                        '--token', 'tok', '--cert', '/tmp/cert.pem']

    # no token / cert -> the endpoint resolves them itself
    argv = endpoint_proc.command('ep_a', 'https://host:8013',
                                 binary=str(binary))
    assert '--token' not in argv
    assert '--cert' not in argv


def test_missing_endpoint_binary_is_reported(tmp_path):

    with pytest.raises(endpoint_proc.EndpointError):
        endpoint_proc.find_endpoint_bin(str(tmp_path / 'nope.py'))


def test_pidfile_round_trip(tmp_path):

    endpoint_proc.write_pidfile('x', 4711, 'ep_x', 'https://b:1')
    info = pidfile('x')

    assert info['pid']      == 4711
    assert info['endpoint'] == 'ep_x'
    assert info['broker']   == 'https://b:1'

    endpoint_proc.remove_pidfile('x')
    assert endpoint_proc.read_pidfile('x') is None


def test_pidfile_tolerates_a_plain_pid(tmp_path):

    path = endpoint_proc.pid_path('y')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fout:
        fout.write('4712\n')

    assert pidfile('y')['pid'] == 4712

    with open(path, 'w', encoding='utf-8') as fout:
        fout.write('garbage\n')

    assert endpoint_proc.read_pidfile('y') is None


# ---------------------------------------------------------------------------
# --member: the class-pool member grammar
# ---------------------------------------------------------------------------

def test_parse_member_maps_the_known_keys():

    member = join.parse_member(
        'gpu:queue=GPU,account=abc123,nodes=2,cpus=64,gpus=8,walltime=3600,'
        'min_pilots=1,max_pilots=3,node_hours=8,class=gpu,backend=concurrent,'
        'shared_fs=false')

    assert member['member']           == 'gpu'
    assert member['queue']            == 'GPU'
    assert member['account']          == 'abc123'
    assert member['nodes']            == 2
    assert member['cpus_per_node']    == 64
    assert member['gpus_per_node']    == 8
    assert member['walltime_sec']     == 3600
    assert member['min_pilots']       == 1
    assert member['max_pilots']       == 3
    assert member['budget']           == {'node_hours': 8.0}
    assert member['class']            == 'gpu'
    assert member['rhapsody_backend'] == 'concurrent'
    assert member['shared_fs']        is False


def test_parse_member_software_is_always_a_list():

    # the record shape must not depend on how many tags were typed: a
    # fragment without '=' continues the previous key
    one  = join.parse_member('cpu:queue=RM,software=lammps')
    many = join.parse_member('cpu:queue=RM,software=lammps,pytorch,vasp')

    assert one['software']  == ['lammps']
    assert many['software'] == ['lammps', 'pytorch', 'vasp']
    # ... and a scalar key after a list is a key of its own again
    assert join.parse_member('cpu:software=a,b,site=NERSC') == {
        'member': 'cpu', 'software': ['a', 'b'],
        'attributes': {'site': 'NERSC'}}


def test_parse_member_unknown_keys_become_attributes():

    member = join.parse_member('cpu:queue=RM,site=NERSC,mem_gb_per_node=256,'
                               'tier=1.5,zones=a,b')

    assert member['attributes'] == {'site': 'NERSC', 'mem_gb_per_node': 256,
                                    'tier': 1.5, 'zones': ['a', 'b']}
    assert 'site' not in member


def test_parse_member_rejects_a_leading_fragment_without_equals():

    with pytest.raises(join.UsageError) as exc:
        join.parse_member('local_b:a,b')

    # the exact message the plan asks for -- a dropped token would be worse
    assert "'a' is not key=value" in str(exc.value)


@pytest.mark.parametrize('spec', [
    'cpu',                      # no ':' at all
    'cpu:',                     # nothing after it
    'CPU:queue=RM',             # upper case
    'c.pu:queue=RM',            # a dot -- that is the member id separator
    '_cpu:queue=RM',            # leading underscore
    ':queue=RM',                # no name
    'cpu:queue=RM,queue=GPU',   # the same key twice
    'cpu:queue=RM,nodes=many',  # not a number
    'cpu:queue=RM,nodes=0',     # not positive
    'cpu:queue=RM,node_hours=0',
    'cpu:queue=RM,shared_fs=maybe',
    'cpu:queue=RM,class=GPU',   # never lower-cased for you
    'cpu:queue=RM,queue2=a,b,walltime=x',
    'cpu:nodes=1,nodes2=,=oops',
])
def test_parse_member_rejects_junk(spec):

    with pytest.raises(join.UsageError):
        join.parse_member(spec)


def test_parse_member_rejects_a_scalar_key_given_a_list():

    with pytest.raises(join.UsageError) as exc:
        join.parse_member('cpu:queue=RM,GPU')

    assert 'queue' in str(exc.value)


def test_members_must_be_uniquely_named():

    with pytest.raises(join.UsageError):
        join.parse_members(['cpu:queue=RM', 'cpu:queue=GPU'])


MEMBER_CPU = ('cpu:queue=local,nodes=1,cpus=2,walltime=1800,node_hours=2,'
              'software=lammps,pytorch,site=NERSC')
MEMBER_GPU = ('gpu:queue=local,nodes=1,cpus=1,gpus=1,walltime=1800,'
              'node_hours=1,max_pilots=1,software=pytorch,site=NERSC')


def _member_args(*extra):

    return parsed(['--broker', 'https://x', '--name', 'local_b',
                   '--mode', 'login', '--site', 'NERSC', '--kind', 'hpc',
                   '--member', MEMBER_CPU, '--member', MEMBER_GPU] +
                  list(extra))


def test_member_and_the_flat_login_flags_are_mutually_exclusive():

    with pytest.raises(join.UsageError) as exc:
        _member_args('--queue', 'RM')

    assert '--queue' in str(exc.value)
    assert 'mutually exclusive' in str(exc.value)


def test_member_is_rejected_in_allocation_mode():

    with pytest.raises(join.UsageError) as exc:
        parsed(['--broker', 'https://x', '--name', 'a', '--mode',
                'allocation', '--member', MEMBER_CPU])

    assert '--member' in str(exc.value)


@pytest.mark.parametrize('spec,missing', [
    ('cpu:nodes=1,cpus=2,walltime=60,node_hours=1',      'queue'),
    ('cpu:queue=local,cpus=2,walltime=60,node_hours=1',  'nodes'),
    ('cpu:queue=local,nodes=1,walltime=60,node_hours=1', 'cpus'),
    ('cpu:queue=local,nodes=1,cpus=2,node_hours=1',      'walltime'),
    ('cpu:queue=local,nodes=1,cpus=2,walltime=60',       'node_hours'),
])
def test_a_member_must_describe_its_pilots(spec, missing):

    with pytest.raises(join.UsageError) as exc:
        parsed(['--broker', 'https://x', '--name', 'a', '--mode', 'login',
                '--member', spec])

    assert missing in str(exc.value)


def test_a_member_queue_must_not_be_the_dispatcher_sentinel():

    with pytest.raises(join.UsageError):
        parsed(['--broker', 'https://x', '--name', 'a', '--mode', 'login',
                '--member', 'cpu:queue=default,nodes=1,cpus=2,walltime=60,'
                            'node_hours=1'])


def test_a_member_min_pilots_must_not_exceed_max_pilots():

    with pytest.raises(join.UsageError):
        parsed(['--broker', 'https://x', '--name', 'a', '--mode', 'login',
                '--member', 'cpu:queue=local,nodes=1,cpus=2,walltime=60,'
                            'node_hours=1,min_pilots=3,max_pilots=1'])


def test_the_record_carries_the_members_and_no_flat_pool():

    record = join.assemble_record(_member_args())

    assert [m['member'] for m in record['members']] == ['cpu', 'gpu']
    assert 'pool' not in record                  # the flat block is out
    assert record['members'][1]['gpus_per_node'] == 1
    assert record['members'][1]['attributes']    == {'site': 'NERSC'}
    assert record['members'][0]['budget']        == {'node_hours': 2.0}


def test_the_record_capabilities_are_the_member_aggregate():

    record = join.assemble_record(_member_args())
    caps   = record['capabilities']

    # cores = sum(nodes x cpus_per_node), gpus likewise, software = union
    assert caps['cores']    == 1 * 2 + 1 * 1
    assert caps['gpus']     == 1
    assert caps['software'] == ['lammps', 'pytorch']
    # and the budget is the sum of the members' own budgets
    assert record['budget'] == {'node_hours': 3.0}


def test_an_explicit_node_hours_wins_over_the_member_sum():

    record = join.assemble_record(_member_args('--node-hours', '9'))

    assert record['budget'] == {'node_hours': 9.0}


def test_declared_capabilities_still_win():

    record = join.assemble_record(_member_args('--declare', 'cores=99'))

    assert record['capabilities']['cores'] == 99


def test_joined_members_are_echoed_back(capsys):

    args   = _member_args()
    record = join.assemble_record(args)

    join._report_joined('local_b', record, dict(record, members=[
        dict(record['members'][0], member_id='local_b.cpu',
             pool_name='fed-cpu', **{'class': 'cpu'})]))

    out = capsys.readouterr().out
    assert 'members' in out
    assert 'cpu [fed-cpu]' in out
    assert '1x2c' in out
    assert 'software=lammps,pytorch' in out


def test_pilot_pids_matches_a_class_pool_pilot():

    # a class pool names its pilots '<pool>_<member_id>_<pid>'
    procs = [(20, 'radical-orbit-endpoint --name fed-gpu_local_a.gpu_'
                  'p.a1b2c3d4e5 --plugins default'),
             (21, 'radical-orbit-endpoint --name fed-cpu_local.cpu_'
                  'p.a1b2c3d4e5 --plugins default')]

    assert endpoint_proc.pilot_pids('local_a', procs) == [20]
    assert endpoint_proc.pilot_pids('local',   procs) == [21]
    assert endpoint_proc.pilot_pids('local_a.gpu', procs) == []


# ---------------------------------------------------------------------------
# atomic-resources
# ---------------------------------------------------------------------------

RECORD = {'name'        : 'local_a',
          'endpoint'    : 'ep_local_a',
          'mode'        : 'allocation',
          'site'        : 'local',
          'kind'        : 'workstation',
          'capabilities': {'cores': 4, 'gpus': 0, 'mem_gb': 8.0,
                           'software': ['lammps']},
          'budget'      : {'node_hours': 4.0},
          'usage'       : {'node_hours_used': 1.25,
                           'node_hours_remaining': 2.75,
                           'pilots_active': 1, 'tasks_running': 3,
                           'tasks_done': 12},
          'liveness'    : 'ok'}


def test_resources_table(broker, capsys):

    broker.resources.append(RECORD)

    rc = resources.main(['--broker', 'https://127.0.0.1:8013', '--token', ''])
    out = capsys.readouterr().out

    assert rc == 0
    assert 'RESOURCE' in out and 'NODE-H' in out and 'LIVENESS' in out
    assert 'local_a' in out
    assert '1.25/2.75' in out                    # node-hours used/remaining
    assert '3/12' in out                         # tasks running/done
    assert 'lammps' in out


def test_resources_marks_stale_usage(broker, capsys):

    stale = json.loads(json.dumps(RECORD))
    stale['usage']['stale'] = True
    broker.resources.append(stale)

    resources.main(['--broker', 'https://127.0.0.1:8013', '--token', ''])
    out = capsys.readouterr().out

    assert '1.25/2.75*' in out
    assert 'stale' in out


def test_resources_derives_remaining_node_hours():

    record = json.loads(json.dumps(RECORD))
    record['usage'].pop('node_hours_remaining')

    assert resources.node_hours(record) == '1.25/2.75'

    record['usage'] = {}
    assert resources.node_hours(record) == '-/4'

    record.pop('budget')
    assert resources.node_hours(record) == '-/-'


def test_resources_json(broker, capsys):

    broker.resources.append(RECORD)

    rc = resources.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                         '--json'])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == [RECORD]


def test_resources_empty(broker, capsys):

    resources.main(['--broker', 'https://127.0.0.1:8013', '--token', ''])

    assert 'no resources' in capsys.readouterr().out


def test_resources_without_a_federation_plugin(broker, capsys):

    broker.errors['/federation/resources/'] = (503, 'not hosted')

    rc = resources.main(['--broker', 'https://127.0.0.1:8013', '--token', ''])

    assert rc == 1
    assert "does not host the 'federation' plugin" in capsys.readouterr().err

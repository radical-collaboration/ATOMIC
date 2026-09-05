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


def test_budget_defaults_from_the_allocation():

    args = parsed(['--broker', 'https://x', '--name', 'a', '--mode',
                   'allocation'])

    rec = join.assemble_record(args, {'cores': 8},
                               {'n_nodes': 2, 'runtime': 1800})
    assert rec['budget'] == {'node_hours': 1.0}

    # no allocation information at all -> the federation's own default
    rec = join.assemble_record(args, {'cores': 8}, None)
    assert rec['budget'] == {'node_hours': join.DEFAULT_NODE_HOURS}


def test_declared_budget_wins():

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
                   'budget'      : {'node_hours': 1.0},
                   'scratch_base': '/tmp/atomic-demo/local_a'}


def test_login_record_carries_the_pool():

    args = parsed(['--broker', 'https://x', '--name', 'b', '--mode', 'login',
                   '--queue', 'RM', '--account', 'abc123', '--nodes', '2',
                   '--cpus', '128', '--walltime', '3600',
                   '--node-hours', '20'])
    rec  = join.assemble_record(args, {'cores': 8})

    assert rec['pool'] == {'queue'           : 'RM',
                           'account'         : 'abc123',
                           'nodes'           : 2,
                           'cpus_per_node'   : 128,
                           'gpus_per_node'   : 0,
                           'walltime_sec'    : 3600,
                           'max_pilots'      : 2,
                           'rhapsody_backend': 'concurrent'}
    assert rec['budget'] == {'node_hours': 20.0}


def test_detection_is_skipped_when_everything_is_declared(broker):

    broker.connect('ep_a')
    client = _client(broker)

    detected, alloc = join.detect(client, 'ep_a', 'login',
                                  {'cores': 1, 'gpus': 0, 'mem_gb': 2})

    assert detected == {}
    assert alloc is None
    assert broker.calls == []


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
        'budget'      : {'node_hours': join.DEFAULT_NODE_HOURS},
        'scratch_base': '/tmp/atomic-demo/local_a'}]

    # ... and a pidfile was written for atomic-leave
    info = pidfile('local_a')
    assert info['pid']      == 4242
    assert info['endpoint'] == 'ep_local_a'

    out = capsys.readouterr().out
    assert 'joined federation' in out
    assert 'atomic-leave local_a' in out


def test_join_uses_the_allocation_for_size_and_budget(broker, proc):

    broker.allocation = {'n_nodes': 2, 'runtime': 7200}

    rc = join.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                    '--name', 'alloc_a', '--mode', 'allocation', '--detach',
                    '--connect-timeout', '2'])

    assert rc == 0
    assert broker.joined[0]['budget'] == {'node_hours': 4.0}
    assert broker.joined[0]['capabilities']['cores'] == 16   # detected


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


def test_join_without_a_federation_plugin(broker, proc, capsys):

    broker.errors['/federation/join/'] = (503, 'plugin not hosted')

    rc = join.main(ARGS_LOCAL)

    assert rc == 1
    assert proc.instances[0].stopped
    assert "does not host the 'federation' plugin" in capsys.readouterr().err


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


@pytest.fixture
def table(monkeypatch):

    def _make(procs):
        tbl = FakeProcTable(procs)
        monkeypatch.setattr(endpoint_proc, 'iter_processes', tbl.listing)
        monkeypatch.setattr(endpoint_proc, 'pid_alive', tbl.alive)
        monkeypatch.setattr(os, 'kill', tbl.kill)
        return tbl

    return _make


def test_pilot_pids_matches_only_this_resources_pilots():

    procs = [(10, 'python radical-orbit-endpoint.py --name fed-local_a_1'),
             (11, 'python radical-orbit-endpoint.py --name fed-other_b_1'),
             (12, 'python radical-orbit-endpoint.py --name ep_local_a'),
             (13, 'vim fed-local_a_1.log')]

    assert endpoint_proc.pilot_pids('local_a', procs) == [10]


def test_leave_stops_endpoint_and_pilots(broker, table, capsys):

    broker.resources.append({'name': 'local_a'})
    endpoint_proc.write_pidfile('local_a', 100, 'ep_local_a')

    tbl = table({100: 'radical-orbit-endpoint.py --name ep_local_a',
                 200: 'radical-orbit-endpoint.py --name fed-local_a_1',
                 300: 'radical-orbit-endpoint.py --name fed-other_1'})

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
    table({100: 'radical-orbit-endpoint.py --name ep_local_a'})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'local_a'])

    assert rc == 1                              # the failure is visible
    assert endpoint_proc.read_pidfile('local_a') is None   # ... but we tidied


def test_leave_can_keep_the_endpoint(broker, table):

    broker.resources.append({'name': 'local_a'})
    endpoint_proc.write_pidfile('local_a', 100, 'ep_local_a')
    tbl = table({100: 'radical-orbit-endpoint.py --name ep_local_a'})

    rc = leave.main(['--broker', 'https://127.0.0.1:8013', '--token', '',
                     'local_a', '--keep-endpoint'])

    assert rc == 0
    assert tbl.killed == []


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
    assert 'NAME' in out and 'NODE-H' in out and 'LIVENESS' in out
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

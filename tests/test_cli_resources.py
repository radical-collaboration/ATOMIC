"""Unit tests for ``atomic-resources``' two-level table.

A resource row is the aggregate; underneath it sits one indented row per
**member** -- one shape of pilot the resource is willing to run -- with
its own class, pool, size, software, node-hours and task counts.  Every
test here renders from a canned grouped response, so nothing needs a
broker.
"""

import json

import pytest

from atomic_wm.cli import resources
from atomic_wm.client import members_of

from fake_broker import FakeBroker


# ---------------------------------------------------------------------------
# canned records
# ---------------------------------------------------------------------------

def _member(name, cls, cpus=2, gpus=0, software=(), used=0.2, left=1.8,
            pilots=1, running=2, done=3, site='NERSC'):

    return {'member'       : name,
            'member_id'    : 'local_b.%s' % name,
            'class'        : cls,
            'pool_name'    : 'fed-%s' % cls,
            'queue'        : 'local',
            'nodes'        : 1,
            'cpus_per_node': cpus,
            'gpus_per_node': gpus,
            'walltime_sec' : 1800,
            'software'     : list(software),
            'attributes'   : {'site': site},
            'budget'       : {'node_hours': used + left},
            'usage'        : {'node_hours_used'     : used,
                              'node_hours_remaining': left,
                              'pilots_active'       : pilots,
                              'tasks_running'       : running,
                              'tasks_done'          : done},
            'liveness'     : 'ok'}


GROUPED = {
    'name'        : 'local_b',
    'endpoint'    : 'ep_local_b',
    'mode'        : 'login',
    'site'        : 'NERSC',
    'kind'        : 'hpc',
    'capabilities': {'cores': 3, 'gpus': 1,
                     'software': ['lammps', 'pytorch']},
    'budget'      : {'node_hours': 3.0},
    'usage'       : {'node_hours_used': 0.31, 'node_hours_remaining': 2.69,
                     'pilots_active': 1, 'tasks_running': 2, 'tasks_done': 5},
    'liveness'    : 'ok',
    'members'     : [
        _member('cpu', 'cpu', cpus=2, software=['lammps', 'pytorch'],
                used=0.20, left=1.80, pilots=1, running=2, done=3),
        _member('gpu', 'gpu', cpus=1, gpus=1, software=['pytorch'],
                used=0.11, left=0.89, pilots=0, running=0, done=2)],
}

# a record from a broker that predates class pools: no `members` at all
FLAT = {
    'name'        : 'local_a',
    'endpoint'    : 'ep_local_a',
    'mode'        : 'allocation',
    'site'        : 'Rutgers',
    'kind'        : 'workstation',
    'capabilities': {'cores': 4, 'gpus': 0, 'mem_gb': 8.0,
                     'software': ['lammps']},
    'budget'      : {'node_hours': 4.0},
    'pool_name'   : 'fed-local_a',
    'usage'       : {'node_hours_used': 1.25, 'node_hours_remaining': 2.75,
                     'pilots_active': 1, 'tasks_running': 3,
                     'tasks_done': 12},
    'liveness'    : 'ok',
}


def _lines(records):

    return resources.render(records).splitlines()


def _row(records, starts):

    for line in _lines(records):
        if line.startswith(starts):
            return line
    raise AssertionError('no row starting %r in\n%s'
                         % (starts, resources.render(records)))


# ---------------------------------------------------------------------------
# the grouped table
# ---------------------------------------------------------------------------

def test_the_header_names_the_member_columns():

    head = _lines([GROUPED])[0].split()

    assert head == ['RESOURCE', 'MEMBER', 'CLASS/POOL', 'SITE', 'SIZE',
                    'SOFTWARE', 'NODE-H', 'PILOTS', 'TASKS', 'LIVENESS']


def test_a_resource_row_is_followed_by_one_row_per_member():

    lines = _lines([GROUPED])

    assert lines[1].startswith('local_b')
    assert lines[2].startswith('  └ cpu')
    assert lines[3].startswith('  └ gpu')


def test_the_resource_row_counts_its_members_and_sums_their_usage():

    row = _row([GROUPED], 'local_b')

    assert '2 members' in row
    assert '0.31/2.69' in row          # the aggregate the federation reports
    assert '2/5' in row                # tasks running/done
    assert 'lammps,pytorch' in row     # the union


def test_a_member_row_carries_its_class_pool_size_and_own_usage():

    cpu = _row([GROUPED], '  └ cpu')
    gpu = _row([GROUPED], '  └ gpu')

    assert 'cpu/fed-cpu' in cpu
    assert '1x2c' in cpu and '+1g' not in cpu
    assert '0.20/1.80' in cpu
    assert '2/3' in cpu

    assert 'gpu/fed-gpu' in gpu
    assert '1x1c+1g' in gpu            # GPUs are part of the declared size
    assert 'pytorch' in gpu and 'lammps' not in gpu
    assert '0.11/0.89' in gpu


def test_a_member_row_prefers_its_own_site():

    record = json.loads(json.dumps(GROUPED))
    record['members'][1]['attributes']['site'] = 'PSC'

    assert 'PSC' in _row([record], '  └ gpu')
    assert 'NERSC' in _row([record], '  └ cpu')


def test_the_legend_explains_size_and_says_declared():

    text = resources.render([GROUPED])

    assert 'SIZE: nodes x cores/node' in text
    # nothing reserves a GPU this round -- the table must not imply it does
    assert 'declared' in text and 'reserved' in text


def test_a_stale_member_is_marked_and_explained():

    record = json.loads(json.dumps(GROUPED))
    record['members'][0]['usage']['stale'] = True

    text = resources.render([record])

    assert '0.20/1.80*' in text
    assert 'stale' in text


# ---------------------------------------------------------------------------
# a member whose pilots die at submit
# ---------------------------------------------------------------------------

QUOTA = 'psij submit_tunneled failed: [Errno 122] Disk quota exceeded'


def _failing(record=None, paused=None, member=0):
    """GROUPED with one member the dispatcher cannot start a pilot on."""

    record = json.loads(json.dumps(record or GROUPED))
    m      = record['members'][member]
    m['state']  = 'failing'
    record['state'] = 'failing'
    m['usage'].update({'pilots_active' : 0,
                       'pilot_error'   : QUOTA,
                       'pilot_failures': 5,
                       'paused_until'  : paused})

    return record


def test_the_state_column_shows_the_derived_word():

    record = _failing()

    # the member that is failing says so; the resource row above it too,
    # and its healthy sibling does not
    assert _row([record], '  └ cpu').rstrip().endswith('failing')
    assert _row([record], 'local_b').rstrip().endswith('failing')
    assert _row([record], '  └ gpu').rstrip().endswith('ok')


def test_a_failing_member_row_is_followed_by_the_pilot_reason():

    lines = _lines([_failing()])

    assert lines[2].startswith('  └ cpu')
    assert lines[3] == '    ! pilot: %s' % QUOTA
    # the sibling row follows, un-annotated
    assert lines[4].startswith('  └ gpu')


def test_a_paused_member_says_until_when():

    line = [x for x in _lines([_failing(paused=1757000000.0)])
            if x.startswith('    ! pilot')][0]

    assert QUOTA in line
    assert 'paused until' in line


def test_the_legend_explains_the_pilot_line():

    assert '!:' in resources.render([_failing()])
    # ... and only when there is one
    assert '!:' not in resources.render([GROUPED])


def test_a_healthy_table_carries_no_pilot_line():

    assert '! pilot' not in resources.render([GROUPED])


def test_the_state_falls_back_to_liveness():
    # a broker that does not derive a state word: the column reads as before
    record = json.loads(json.dumps(GROUPED))
    record['liveness'] = 'suspect'
    record['members'][0]['liveness'] = 'suspect'

    assert _row([record], 'local_b').rstrip().endswith('suspect')
    assert _row([record], '  └ cpu').rstrip().endswith('suspect')


# ---------------------------------------------------------------------------
# a record without members
# ---------------------------------------------------------------------------

def test_a_record_without_members_renders_a_single_row():

    lines = _lines([FLAT])

    assert lines[1].startswith('local_a')
    assert not any(line.startswith('  └') for line in lines)


def test_the_derived_member_supplies_the_size_and_class():

    row = _row([FLAT], 'local_a')

    assert '1x4c' in row                     # the allocation is the pilot
    assert 'cpu/fed-local_a' in row          # no GPUs declared -> class cpu
    assert '1.25/2.75' in row


def test_a_derived_member_of_a_gpu_resource_is_the_gpu_class():

    record = json.loads(json.dumps(FLAT))
    record['capabilities']['gpus'] = 4

    assert members_of(record)[0]['class'] == 'gpu'


def test_a_derived_allocation_member_has_no_queue_of_its_own():

    # an allocation-mode resource never declared a queue; putting the join
    # mode in a queue column would be a word that is not a queue
    assert members_of(FLAT)[0]['queue'] == ''


def test_a_login_record_without_members_derives_from_its_pool_block():

    record = json.loads(json.dumps(FLAT))
    record['mode'] = 'login'
    record['pool'] = {'queue': 'RM', 'account': 'abc', 'nodes': 2,
                      'cpus_per_node': 128, 'gpus_per_node': 4,
                      'walltime_sec': 3600, 'max_pilots': 2}

    member = members_of(record)[0]

    assert member['member']        == 'default'
    assert member['member_id']     == 'local_a.default'
    assert member['class']         == 'gpu'
    assert member['nodes']         == 2
    assert member['cpus_per_node'] == 128
    assert member['derived']       is True
    assert '2x128c+4g' in _row([record], 'local_a')


def test_members_of_passes_a_grouped_record_through():

    members = members_of(GROUPED)

    assert [m['member'] for m in members] == ['cpu', 'gpu']
    assert not any(m.get('derived') for m in members)


def test_a_mixed_federation_renders_both_shapes():

    lines = _lines([FLAT, GROUPED])

    assert lines[1].startswith('local_a')
    assert lines[2].startswith('local_b')
    assert lines[3].startswith('  └ cpu')


def test_empty_federation():

    assert 'no resources' in resources.render([])


# ---------------------------------------------------------------------------
# --json passthrough
# ---------------------------------------------------------------------------

@pytest.fixture
def broker(monkeypatch):

    return FakeBroker().install(monkeypatch)


def test_json_passes_the_members_through(broker, capsys):

    broker.resources.append(GROUPED)

    rc  = resources.main(['--broker', 'https://127.0.0.1:8013',
                          '--token', '', '--json'])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    # the raw records, unchanged -- scripts and smoke.py read these
    assert out == [GROUPED]
    assert [m['member_id'] for m in out[0]['members']] == ['local_b.cpu',
                                                           'local_b.gpu']


def test_json_passes_the_pilot_failure_fields_through(broker, capsys):

    record = _failing(paused=17.0)
    broker.resources.append(record)

    rc  = resources.main(['--broker', 'https://127.0.0.1:8013',
                          '--token', '', '--json'])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out == [record]                     # unchanged, keys and all
    usage = out[0]['members'][0]['usage']
    assert usage['pilot_error']    == QUOTA
    assert usage['pilot_failures'] == 5
    assert usage['paused_until']   == 17.0


def test_the_table_is_rendered_from_the_broker_response(broker, capsys):

    broker.resources.append(GROUPED)

    rc  = resources.main(['--broker', 'https://127.0.0.1:8013', '--token', ''])
    out = capsys.readouterr().out

    assert rc == 0
    assert '  └ gpu' in out
    assert 'fed-gpu' in out

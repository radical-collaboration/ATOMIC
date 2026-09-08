"""Unit tests for ``atomic-resources``' two column sets.

A resource row says what the resource is and how its work is going;
underneath it sits one indented row per **pilot** the resource runs -- an
allocation is one pilot named after its endpoint, a login-mode resource
submits one shape of pilot per capability class, named
``<endpoint>/<shape>``.  Every test here renders from a canned response,
so nothing needs a broker.

The three payload shapes the client has to render are all here: a
federation that names the endpoint, the pilot kind and the time left per
row (``SHAPED``), one that reports rows without those fields
(``GROUPED``), and one that predates class pools and reports no rows at
all (``FLAT``).
"""

import json

import pytest

from atomic_wm.cli import resources
from atomic_wm.client import pilots_of

from fake_broker import FakeBroker


# ---------------------------------------------------------------------------
# canned records
# ---------------------------------------------------------------------------

def _member(name, cls, cpus=2, gpus=0, software=(), used=0.2, left=1.8,
            pilots=1, running=2, done=3, failed=0, site='NERSC'):

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
            'attributes'   : {'site': site, 'mem_gb_per_node': 64},
            'budget'       : {'node_hours': used + left},
            'usage'        : {'node_hours_used'     : used,
                              'node_hours_remaining': left,
                              'pilots_active'       : pilots,
                              'tasks_running'       : running,
                              'tasks_done'          : done,
                              'tasks_failed'        : failed},
            'liveness'     : 'ok'}


# shape 2: rows, but no `endpoint` / `pilot` / `remaining_sec` on them
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
                     'pilots_active': 1, 'tasks_running': 2,
                     'tasks_done': 5, 'tasks_failed': 1},
    'liveness'    : 'ok',
    'members'     : [
        _member('cpu', 'cpu', cpus=2, software=['lammps', 'pytorch'],
                used=0.20, left=1.80, pilots=1, running=2, done=3),
        # a shape that holds no pilot right now
        _member('gpu', 'gpu', cpus=1, gpus=1, software=['pytorch'],
                used=0.11, left=0.89, pilots=0, running=0, done=2)],
}

# shape 1: the federation names the endpoint, the pilot kind and the time
# left per row, and derives `idle` itself
SHAPED = {
    'name'        : 'odo',
    'endpoint'    : 'ep_odo',
    'mode'        : 'allocation',
    'site'        : 'OLCF',
    'kind'        : 'hpc',
    'capabilities': {'cores': 112, 'gpus': 8, 'mem_gb': 256,
                     'software': ['lammps', 'pytorch']},
    'usage'       : {'node_hours_used': 1.5, 'tasks_running': 0,
                     'tasks_done': 3, 'tasks_failed': 0, 'pilots_active': 1},
    'liveness'    : 'ok',
    'state'       : 'ok',
    'members'     : [
        {'member'       : 'default',
         'member_id'    : 'odo.default',
         'endpoint'     : 'ep_odo',
         'pilot'        : 'endpoint',
         'class'        : 'gpu',
         'pool_name'    : 'fed-gpu',
         'nodes'        : 2,
         'cpus_per_node': 112,
         'gpus_per_node': 8,
         'walltime_sec' : 5400,
         'remaining_sec': 4140,
         'software'     : ['lammps', 'pytorch'],
         'attributes'   : {'site': 'OLCF', 'mem_gb_per_node': 256},
         'usage'        : {'tasks_running': 0, 'tasks_done': 3,
                           'tasks_failed': 0, 'pilots_active': 1},
         'liveness'     : 'ok',
         'state'        : 'ok'}],
}

# shape 3: a record from a broker that predates class pools -- no rows
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
# the two headers
# ---------------------------------------------------------------------------

def test_the_two_headers_name_the_two_column_sets():

    head = _lines([GROUPED])

    assert head[0].split() == ['RESOURCE', 'SITE', 'SOFTWARE', 'CLASSES',
                               'RUN', 'DONE', 'FAILED', 'STATE']
    assert head[1].split() == ['PILOT', 'MODE', 'NODES', 'CPN', 'GPN',
                               'MPN', 'RUNTIME', 'LEFT', 'RUN', 'DONE',
                               'FAILED', 'STATE']


def test_the_table_never_says_member():

    text = resources.render([SHAPED, GROUPED, FLAT])

    assert 'member' not in text.lower()


# ---------------------------------------------------------------------------
# shape 1: endpoint, pilot kind and time left per row
# ---------------------------------------------------------------------------

def test_an_allocation_row_is_named_after_the_endpoint():

    lines = _lines([SHAPED])

    assert lines[2].startswith('odo')
    assert lines[3].startswith('  └ ep_odo')
    assert 'alloc' in lines[3]


def test_a_pilot_row_carries_its_size_and_its_hours():

    row = _row([SHAPED], '  └ ep_odo').split()

    # name, mode, nodes, cpn, gpn, mpn, runtime, left, run, done, failed
    assert row[1:] == ['ep_odo', 'alloc', '2', '112', '8', '256',
                       '1.50', '1.15', '0', '3', '0', 'ok']


def test_the_resource_row_shows_site_software_and_classes():

    row = _row([SHAPED], 'odo')

    assert 'OLCF' in row
    assert 'lammps,pytorch' in row          # the union over the pilot rows
    assert 'fed-gpu' in row                 # the class pool it serves
    # the record's own counts: it counts what is not placed yet, too
    assert row.split()[-4:] == ['0', '3', '0', 'ok']


# ---------------------------------------------------------------------------
# shape 2: rows without the endpoint, the kind or the time left
# ---------------------------------------------------------------------------

def test_login_rows_are_named_endpoint_slash_shape():

    lines = _lines([GROUPED])

    assert lines[2].startswith('local_b')
    assert lines[3].startswith('  └ ep_local_b/cpu')
    assert lines[4].startswith('  └ ep_local_b/gpu')
    assert 'login' in lines[3]


def test_a_shape_without_a_pilot_is_idle():

    # the federation sends no state word: a shape that holds no pilot and
    # has not failed is idle -- declared, simply not running anything
    assert _row([GROUPED], '  └ ep_local_b/gpu').rstrip().endswith('idle')
    assert _row([GROUPED], '  └ ep_local_b/cpu').rstrip().endswith('ok')


def test_the_hours_columns_never_go_negative():

    # a walltime that has run out comes back slightly negative from more
    # than one batch system; 0.00 is what that means
    assert resources.hours(-90)   == '0.00'
    assert resources.hours(0)     == '0.00'
    assert resources.hours(5400)  == '1.50'
    assert resources.hours(None)  == '-'
    assert resources.hours('now') == '-'


def test_a_row_without_a_reported_time_left_says_so():

    row = _row([GROUPED], '  └ ep_local_b/cpu').split()

    assert row[7] == '0.50'                 # runtime: 1800 s
    assert row[8] == '-'                    # left: this federation says none


def test_the_resource_row_shows_the_worst_state_of_its_pilots():

    record = json.loads(json.dumps(GROUPED))
    record['members'][0]['state'] = 'suspect'

    # ok + suspect + idle -> suspect
    assert _row([record], 'local_b').rstrip().endswith('suspect')

    # one busy shape is enough to call the resource ok, even next to an
    # idle one (GROUPED's gpu shape holds no pilot)
    record['members'][0].pop('state', None)
    assert _row([record], 'local_b').rstrip().endswith('ok')

    # a resource whose shapes are ALL idle runs nothing at all, and says
    # so -- its own `ok` liveness says nothing about the work
    for m in record['members']:
        m.pop('state', None)
        m['usage']['pilots_active'] = 0
    assert record['liveness'] == 'ok'
    assert _row([record], 'local_b').rstrip().endswith('idle')

    # ... and the word the federation derives for the resource is believed
    record['state'] = 'ok'
    assert _row([record], 'local_b').rstrip().endswith('ok')


def test_the_states_are_ranked_the_way_the_federation_ranks_them():

    # `idle` is the quietest word there is: one busy shape wins over it
    assert resources.worst_state(['ok', 'idle'])           == 'ok'
    assert resources.worst_state(['idle', 'idle'])         == 'idle'
    assert resources.worst_state(['ok', 'stale'])          == 'stale'
    assert resources.worst_state(['stale', 'suspect'])     == 'suspect'
    assert resources.worst_state(['suspect', 'failing'])   == 'failing'
    assert resources.worst_state(['failing', 'lost'])      == 'lost'
    # a word this build does not know is news, and news wins
    assert resources.worst_state(['lost', 'melted'])       == 'melted'
    assert resources.worst_state(['', None])               == '-'


def test_the_resource_row_badges_every_class_pool():

    assert 'fed-cpu,fed-gpu' in _row([GROUPED], 'local_b')


def test_the_resource_row_counts_come_from_the_record():

    row = _row([GROUPED], 'local_b').split()

    # 2/5/1 is what the record reports -- NOT the 2/5/0 its rows sum to
    # (and the state is `ok`: one busy shape is a working resource, even
    # though its other shape is idle)
    assert row[-4:] == ['2', '5', '1', 'ok']


def test_the_counts_are_summed_where_the_record_reports_none():

    record = json.loads(json.dumps(GROUPED))
    record.pop('usage')

    row = _row([record], 'local_b').split()

    assert row[-4:] == ['2', '5', '0', 'ok']


def test_a_stale_row_is_marked_and_explained():

    record = json.loads(json.dumps(GROUPED))
    record['members'][0]['usage']['stale'] = True
    record['usage']['stale'] = True

    text = resources.render([record])

    assert _row([record], '  └ ep_local_b/cpu').rstrip().endswith('ok*')
    assert _row([record], 'local_b').rstrip().endswith('ok*')
    assert 'stale' in text


def test_the_legend_says_declared_not_reserved():

    text = resources.render([GROUPED])

    assert 'CPN/GPN/MPN' in text
    # nothing reserves a GPU this round -- the table must not imply it does
    assert 'declared' in text and 'reserved' in text
    assert 'RUNTIME/LEFT' in text and 'hours' in text


def test_a_long_name_is_truncated_with_an_ellipsis():

    record = json.loads(json.dumps(GROUPED))
    record['name']     = 'a_very_long_resource_name_indeed'
    record['endpoint'] = 'ep_a_very_long_endpoint_name'

    lines = _lines([record])

    assert lines[2].startswith('a_very_long_resource_na…')
    # the shape name is cut with the endpoint it hangs off
    assert lines[3].startswith('  └ ep_a_very_long_endpoint…')
    # 24 characters, ellipsis included
    assert len(lines[2].split()[0])              == 24
    assert len(lines[3].split()[1].lstrip('└ ')) == 24


# ---------------------------------------------------------------------------
# a shape whose pilots die at submit
# ---------------------------------------------------------------------------

QUOTA = 'psij submit_tunneled failed: [Errno 122] Disk quota exceeded'


def _failing(record=None, paused=None, member=0):
    """GROUPED with one shape the dispatcher cannot start a pilot on."""

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

    # the shape that is failing says so; the resource row above it too,
    # and its idle sibling does not
    assert _row([record], '  └ ep_local_b/cpu').rstrip().endswith('failing')
    assert _row([record], 'local_b').rstrip().endswith('failing')
    assert _row([record], '  └ ep_local_b/gpu').rstrip().endswith('idle')


def test_a_failing_row_is_followed_by_the_pilot_reason():

    lines = _lines([_failing()])

    assert lines[3].startswith('  └ ep_local_b/cpu')
    assert lines[4] == '    ! pilot: %s' % QUOTA
    # the sibling row follows, un-annotated
    assert lines[5].startswith('  └ ep_local_b/gpu')


def test_a_paused_row_says_until_when():

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
    assert _row([record], '  └ ep_local_b/cpu').rstrip().endswith('suspect')


# ---------------------------------------------------------------------------
# shape 3: a record without rows of its own
# ---------------------------------------------------------------------------

def test_a_record_without_rows_still_gets_one_pilot_row():

    lines = _lines([FLAT])

    assert lines[2].startswith('local_a')
    # the allocation IS the pilot, and it is named after the endpoint
    assert lines[3].startswith('  └ ep_local_a')
    assert 'alloc' in lines[3]


def test_the_derived_row_supplies_the_size_and_the_class():

    row = _row([FLAT], '  └ ep_local_a').split()

    assert row[3:7] == ['1', '4', '0', '8']  # nodes, cpn, gpn, mpn
    assert 'fed-local_a' in _row([FLAT], 'local_a')
    assert row[7] == '-'                     # no walltime on such a record


def test_a_derived_row_of_a_gpu_resource_is_the_gpu_class():

    record = json.loads(json.dumps(FLAT))
    record['capabilities']['gpus'] = 4

    assert pilots_of(record)[0]['class'] == 'gpu'


def test_a_derived_allocation_row_has_no_queue_of_its_own():

    # an allocation-mode resource never declared a queue; putting the join
    # mode in a queue column would be a word that is not a queue
    assert pilots_of(FLAT)[0]['queue'] == ''


def test_a_login_record_without_rows_derives_from_its_pool_block():

    record = json.loads(json.dumps(FLAT))
    record['mode'] = 'login'
    record['pool'] = {'queue': 'RM', 'account': 'abc', 'nodes': 2,
                      'cpus_per_node': 128, 'gpus_per_node': 4,
                      'walltime_sec': 3600, 'max_pilots': 2}

    pilot = pilots_of(record)[0]

    assert pilot['member']        == 'default'
    assert pilot['member_id']     == 'local_a.default'
    assert pilot['pilot_name']    == 'ep_local_a/default'
    assert pilot['class']         == 'gpu'
    assert pilot['nodes']         == 2
    assert pilot['cpus_per_node'] == 128
    assert pilot['derived']       is True

    row = _row([record], '  └ ep_local_a/default').split()

    assert row[2:8] == ['login', '2', '128', '4', '8', '1.00']


def test_pilots_of_passes_a_grouped_record_through():

    pilots = pilots_of(GROUPED)

    assert [p['member'] for p in pilots] == ['cpu', 'gpu']
    assert not any(p.get('derived') for p in pilots)


def test_a_mixed_federation_renders_both_shapes():

    lines = _lines([FLAT, GROUPED])

    assert lines[2].startswith('local_a')
    assert lines[3].startswith('  └ ep_local_a')
    assert lines[4].startswith('local_b')
    assert lines[5].startswith('  └ ep_local_b/cpu')


def test_empty_federation():

    assert 'no resources' in resources.render([])


# ---------------------------------------------------------------------------
# --json passthrough
# ---------------------------------------------------------------------------

@pytest.fixture
def broker(monkeypatch):

    return FakeBroker().install(monkeypatch)


def test_json_passes_the_records_through(broker, capsys):

    broker.resources.append(GROUPED)

    rc  = resources.main(['--broker', 'https://127.0.0.1:8013',
                          '--token', '', '--json'])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    # the raw records, unchanged -- scripts and smoke.py read these, and
    # the node-hours the table no longer shows live on in here
    assert out == [GROUPED]
    assert [m['member_id'] for m in out[0]['members']] == ['local_b.cpu',
                                                           'local_b.gpu']
    assert out[0]['members'][0]['usage']['node_hours_used'] == 0.20


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
    assert '  └ ep_local_b/gpu' in out
    assert 'fed-gpu' in out

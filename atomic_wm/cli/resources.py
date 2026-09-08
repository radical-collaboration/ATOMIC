"""``atomic-resources`` -- list the resources in the ATOMIC federation.

Reads ``GET /broker/federation/resources/default`` and renders two
independent column sets: one row per resource -- what it is, where it is,
what it has installed, which capability class pools it serves and how its
work is going -- and, indented under it, one row per **pilot** the
resource runs.  An allocation is a single pilot, named after its
endpoint; a login-mode resource submits one shape of pilot per class it
serves, each named ``<endpoint>/<shape>``, and a shape that holds no
pilot right now reads ``idle``.  ``--json`` prints the records as they
came from the broker (for scripts and the demo's own checks), node-hours
and all.

A GPU in a pilot's size is what the operator **declared**, not a
reservation: nothing pins a GPU to a task this round.

Usage figures are refreshed by the federation on every call; when that
refresh fails the record keeps its last values and is flagged
``"stale": true`` -- such rows are marked with a ``*`` rather than
hidden, because a stale number still says more than a blank.

The last column is the federation's derived ``state``: the endpoint's
liveness (``ok`` / ``suspect`` / ``lost``), ``idle`` -- nothing running
here -- or ``failing``: reachable, but holding no pilot because the ones
it submitted keep dying.  Such a row carries an indented ``! pilot: ...``
line with what the batch system actually said, which used to reach only
the broker log.
"""

import argparse
import json
import sys
import time

from typing import Any, Dict, List, Optional, Sequence

from ..client import Client, ClientError, add_connection_args, pilots_of

TOOL = 'atomic-resources'

# the two column sets: header, and the row key it renders
RESOURCE_COLUMNS = [('RESOURCE', 'resource'),
                    ('SITE',     'site'),
                    ('SOFTWARE', 'software'),
                    ('CLASSES',  'classes'),
                    ('RUN',      'run'),
                    ('DONE',     'done'),
                    ('FAILED',   'failed'),
                    ('STATE',    'state')]

PILOT_COLUMNS = [('PILOT',   'pilot'),
                 ('MODE',    'mode'),
                 ('NODES',   'nodes'),
                 ('CPN',     'cpn'),
                 ('GPN',     'gpn'),
                 ('MPN',     'mpn'),
                 ('RUNTIME', 'runtime'),
                 ('LEFT',    'left'),
                 ('RUN',     'run'),
                 ('DONE',    'done'),
                 ('FAILED',  'failed'),
                 ('STATE',   'state')]

DASH = '-'

# what an indented pilot row is prefixed with (the header of the pilot
# column set is indented by as much, minus the branch)
BRANCH = '  └ '
INDENT = '  '

# a free-form name (`atomic-join --endpoint` takes any) is cut to this
NAME_WIDTH = 24

# how bad a state word is, for the worst-of a resource row shows.  `idle`
# ranks below `ok`: a resource with one busy shape and one quiet one is
# working, and `idle` on a resource row means it runs nothing at all
STATE_RANK = {'lost': 5, 'failing': 4, 'suspect': 3, 'stale': 2,
              'ok': 1, 'idle': 0}


# ---------------------------------------------------------------------------
def error(msg: str) -> None:
    sys.stderr.write('%s: error: %s\n' % (TOOL, msg))


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """The ``atomic-resources`` command line."""

    parser = argparse.ArgumentParser(
        prog=TOOL,
        description='list the resources joined into the ATOMIC federation')

    add_connection_args(parser)

    parser.add_argument('--json', action='store_true',
                        help='print the raw resource records as JSON')

    return parser


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _num(value: Any, digits: int = 1) -> str:
    """Format a number for the table; ``-`` for anything unusable."""

    if value is None or isinstance(value, bool):
        return DASH

    if isinstance(value, (int, float)):
        if isinstance(value, int) or float(value).is_integer():
            return '%d' % int(value)
        return '%.*f' % (digits, value)

    return str(value)


def hours(seconds: Any) -> str:
    """Seconds as hours with two decimals; ``-`` when there are none.

    A walltime that has run out is reported as a small negative number by
    more than one batch system; ``0.00`` is what that means.
    """

    if seconds is None or isinstance(seconds, bool) \
            or not isinstance(seconds, (int, float)):
        return DASH

    return '%.2f' % max(0.0, float(seconds) / 3600.0)


def clip(name: str) -> str:
    """A name the table has room for -- the rest is one ``…``."""

    text = str(name)

    if len(text) <= NAME_WIDTH:
        return text

    return text[:NAME_WIDTH - 1] + '…'


def worst_state(words: Sequence[str]) -> str:
    """The worst of the words handed in, by the federation's own ranking.

    Anything the federation invented after this was written outranks the
    words we know -- an unknown state is news, and news belongs on the
    resource row.
    """

    known = [w for w in words if w]

    if not known:
        return DASH

    return sorted(known, key=lambda w: (STATE_RANK.get(w, 6), w))[-1]


TASK_KEYS = ('tasks_running', 'tasks_done', 'tasks_failed')


def _tasks(usage: Dict[str, Any]) -> Dict[str, str]:
    """The run / done / failed cells of either row kind."""

    return {'run'   : _num(usage.get('tasks_running')),
            'done'  : _num(usage.get('tasks_done')),
            'failed': _num(usage.get('tasks_failed'))}


def _summed(pilots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Task counts summed over the pilot rows.

    Only for a record that carries no usage of its own: the record counts
    the tasks it has not placed yet too, so a sum would under-count.
    """

    out: Dict[str, Any] = {}

    for key in TASK_KEYS:
        seen = [(p.get('usage') or {}).get(key) for p in pilots]
        seen = [v for v in seen
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if seen:
            out[key] = sum(seen)

    return out


def classes_of(pilots: Sequence[Dict[str, Any]]) -> str:
    """The class pools this resource serves, as badges: ``fed-cpu,fed-gpu``."""

    names = []

    for pilot in pilots:
        name = str(pilot.get('pool_name') or pilot.get('class')
                   or pilot.get('cls') or '')
        if name and name not in names:
            names.append(name)

    return ','.join(names) if names else DASH


def software_of(record: Dict[str, Any],
                pilots: Sequence[Dict[str, Any]]) -> str:
    """The union of what the pilot rows have installed."""

    soft: List[str] = []

    for pilot in pilots:
        for name in pilot.get('software') or []:
            if str(name) not in soft:
                soft.append(str(name))

    if not soft:
        soft = [str(s) for s in (record.get('capabilities') or {})
                                .get('software') or []]

    return ','.join(soft) if soft else DASH


def state_cell(word: str, usage: Dict[str, Any]) -> str:
    """The state word, marked ``*`` when its numbers could not be refreshed."""

    return (word or DASH) + ('*' if usage.get('stale') else '')


def pilot_note(record: Dict[str, Any]) -> str:
    """The indented ``! pilot: ...`` line under a row, or ``''``.

    Every pilot of this shape failed at submit and only the broker log
    said so -- this is that log line, on the row it belongs to.
    """

    usage = record.get('usage') or {}
    error = usage.get('pilot_error')

    if not error:
        return ''

    note  = '    ! pilot: %s' % str(error).strip()
    until = usage.get('paused_until')

    if isinstance(until, (int, float)) and not isinstance(until, bool) \
            and until > 0:
        note += ' (paused until %s)' % time.strftime(
            '%H:%M:%S', time.localtime(until))

    return note


def resource_state(record: Dict[str, Any],
                   pilots: Sequence[Dict[str, Any]]) -> str:
    """The state word of one resource row.

    A federation that derives one for the resource is believed, exactly as
    a row's own word is.  Without one the row shows the worst of its
    pilots', plus the resource's liveness where that is not ``ok`` (an
    ``ok`` endpoint says nothing about the work running on it).  One busy
    shape is enough to call the resource ``ok``; ``idle`` reaches this row
    only when every shape is idle -- the resource runs nothing at all.
    """

    word = str(record.get('state') or '')

    if word:
        return word

    live   = str(record.get('liveness') or '')
    states = [live] if live and live != 'ok' else []
    states += [str(p.get('state') or '') for p in pilots]
    word   = worst_state(states)

    return live if word == DASH and live else word


def resource_row(record: Dict[str, Any],
                 pilots: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """The row of one resource: what it is, and how its work is going.

    The counts come from the record's own usage -- it counts the tasks
    the class pool has not placed yet as well, which no pilot row does --
    and are summed over the pilot rows only where the record carries no
    usage at all.
    """

    usage  = record.get('usage') or {}
    counts = usage if any(k in usage for k in TASK_KEYS) \
                   else _summed(pilots)

    row = {
        'resource': clip(record.get('name') or DASH),
        'site'    : str(record.get('site') or DASH),
        'software': software_of(record, pilots),
        'classes' : classes_of(pilots),
        'state'   : state_cell(resource_state(record, pilots), usage),
        'note'    : '',
    }
    row.update(_tasks(counts))

    return row


def pilot_row(pilot: Dict[str, Any]) -> Dict[str, str]:
    """One indented row per pilot the resource runs."""

    usage = pilot.get('usage') or {}

    row = {
        'pilot'  : BRANCH + clip(pilot.get('pilot_name') or DASH),
        'mode'   : str(pilot.get('mode') or DASH),
        'nodes'  : _num(pilot.get('nodes')),
        'cpn'    : _num(pilot.get('cpus_per_node')),
        'gpn'    : _num(pilot.get('gpus_per_node')),
        'mpn'    : _num(pilot.get('mem_gb_per_node')),
        'runtime': hours(pilot.get('walltime_sec')),
        'left'   : hours(pilot.get('remaining_sec')),
        'state'  : state_cell(str(pilot.get('state') or ''), usage),
        'note'   : pilot_note(pilot),
    }
    row.update(_tasks(usage))

    return row


def rows_for(record: Dict[str, Any]) -> Dict[str, Any]:
    """What one resource contributes: its own row, then its pilot rows."""

    pilots = pilots_of(record)

    return {'resource': resource_row(record, pilots),
            'pilots'  : [pilot_row(p) for p in pilots]}


def _widths(rows: Sequence[Dict[str, str]],
            columns: Sequence[Any]) -> Dict[str, int]:

    return {key: max([len(head)] + [len(r[key]) for r in rows])
            for head, key in columns}


def _line(row: Dict[str, str], columns: Sequence[Any],
          widths: Dict[str, int]) -> str:

    return '  '.join(row[key].ljust(widths[key])
                     for _, key in columns).rstrip()


def render(records: Sequence[Dict[str, Any]]) -> str:
    """The whole table (both headers + rows + legend)."""

    if not records:
        return ('no resources in the federation -- join one with '
                '`atomic-join --name … --mode …`')

    blocks = [rows_for(record) for record in records]
    res    = [b['resource'] for b in blocks]
    pilots = [p for b in blocks for p in b['pilots']]

    rwidth = _widths(res, RESOURCE_COLUMNS)
    pwidth = _widths(pilots, PILOT_COLUMNS) if pilots else {}

    # the pilot header sits where the pilot rows do, one indent in
    pwidth['pilot'] = max(pwidth.get('pilot', 0), len(INDENT + 'PILOT'))

    lines = ['  '.join(head.ljust(rwidth[key])
                       for head, key in RESOURCE_COLUMNS).rstrip()]

    if pilots:
        head = dict((key, name) for name, key in PILOT_COLUMNS)
        head['pilot'] = INDENT + 'PILOT'
        lines.append(_line(head, PILOT_COLUMNS, pwidth))

    for block in blocks:
        lines.append(_line(block['resource'], RESOURCE_COLUMNS, rwidth))
        for row in block['pilots']:
            lines.append(_line(row, PILOT_COLUMNS, pwidth))
            # the row's own bad news, under it and outside the columns: a
            # quota or a queue error is far too long to be a table cell
            if row.get('note'):
                lines.append(row['note'])

    every = res + pilots

    lines.append('')
    lines.append('CPN/GPN/MPN: cores, GPUs and GB of memory per node '
                 '(declared -- not reserved)')
    lines.append('RUNTIME/LEFT: how long a pilot runs, and what is left '
                 'of that, in hours')
    lines.append("STATE: ok | idle -- declared, running nothing | suspect "
                 "| lost | failing")

    if any('*' in r['state'] for r in every):
        lines.append('*: usage could not be refreshed -- values are stale')

    if any(r.get('note') for r in every):
        lines.append("!: no pilot of that shape survived submission -- "
                     "the row's state is 'failing'")

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    """Fetch and print the federation's resources."""

    try:
        client = Client(broker=args.broker, token=args.token, cert=args.cert)
    except ValueError as e:
        error(str(e))
        return 2

    try:
        records: List[Dict[str, Any]] = client.fed_resources()
    except ClientError as e:
        error(str(e))
        if e.status in (404, 503):
            sys.stderr.write("the broker does not host the 'federation' "
                             "plugin\n")
        return 1

    records = sorted(records, key=lambda r: str(r.get('name', '')))

    if args.json:
        sys.stdout.write(json.dumps(records, indent=2) + '\n')
    else:
        sys.stdout.write(render(records) + '\n')

    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console script entry point."""

    args = build_parser().parse_args(argv)

    return run(args)


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.exit(main())

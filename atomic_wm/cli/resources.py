"""``atomic-resources`` -- list the resources in the ATOMIC federation.

Reads ``GET /broker/federation/resources/default`` and renders a two-level
table: one row per resource, then one indented row per **member** -- one
shape of pilot the resource is willing to run, each of which sits in the
capability class pool for its class (``fed-cpu``, ``fed-gpu``).  The
resource row is the aggregate of its members.  ``--json`` prints the
records as they came from the broker (for scripts and the demo's own
checks).

A GPU in a member's size is what the operator **declared**, not a
reservation: nothing pins a GPU to a task this round.

Usage figures are refreshed by the federation on every call; when that
refresh fails the record keeps its last values and is flagged
``"stale": true`` -- such rows are marked with a ``*`` rather than
hidden, because a stale number still says more than a blank.
"""

import argparse
import json
import sys

from typing import Any, Dict, List, Optional, Sequence

from ..client import Client, ClientError, add_connection_args, members_of

TOOL = 'atomic-resources'

# column header, and the row key it renders
COLUMNS = [('RESOURCE',   'resource'),
           ('MEMBER',     'member'),
           ('CLASS/POOL', 'class_pool'),
           ('SITE',       'site'),
           ('SIZE',       'size'),
           ('SOFTWARE',   'software'),
           ('NODE-H',     'node_hours'),
           ('PILOTS',     'pilots'),
           ('TASKS',      'tasks'),
           ('LIVENESS',   'liveness')]

DASH = '-'

# what an indented member row is prefixed with
BRANCH = '  └ '


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


def node_hours(record: Dict[str, Any]) -> str:
    """``used/remaining`` node-hours of a resource or of one member.

    ``remaining`` is what the federation reports; if it does not (yet)
    report one we derive it from the declared budget.  Both record shapes
    carry ``usage`` and ``budget`` under the same keys, so one function
    serves the resource row and the member rows.
    """

    usage = record.get('usage') or {}
    used  = usage.get('node_hours_used')
    left  = usage.get('node_hours_remaining')

    if left is None:
        budget = (record.get('budget') or {}).get('node_hours')
        if budget is not None and isinstance(used, (int, float)):
            left = max(0.0, float(budget) - float(used))
        elif budget is not None and used is None:
            left = budget

    text = '%s/%s' % (_num(used, 2), _num(left, 2))

    if usage.get('stale'):
        text += '*'

    return text


def size_of(member: Dict[str, Any]) -> str:
    """``1x128c+4g`` -- one pilot of this member: nodes x cores (+ GPUs)."""

    nodes = member.get('nodes')
    cpus  = member.get('cpus_per_node')

    if nodes is None and cpus is None:
        return DASH

    text = '%sx%sc' % (_num(nodes), _num(cpus))
    gpus = member.get('gpus_per_node') or 0

    if isinstance(gpus, (int, float)) and gpus:
        text += '+%sg' % _num(gpus)

    return text


def class_pool(member: Dict[str, Any]) -> str:
    """``gpu/fed-gpu`` -- the member's capability class and its pool."""

    cls  = str(member.get('class') or member.get('cls') or '')
    pool = str(member.get('pool_name') or '')

    if cls and pool:
        return '%s/%s' % (cls, pool)

    return cls or pool or DASH


def _tasks(usage: Dict[str, Any]) -> str:

    return '%s/%s' % (_num(usage.get('tasks_running')),
                      _num(usage.get('tasks_done')))


def row(record: Dict[str, Any], members: Sequence[Dict[str, Any]]
        ) -> Dict[str, str]:
    """The aggregate row of one resource.

    ``SIZE`` names the member count -- the sizes themselves differ per
    member and are shown on the member rows below.  A resource whose
    members had to be derived (a broker without class pools) has exactly
    one, and shows its size here instead.
    """

    caps  = record.get('capabilities') or {}
    usage = record.get('usage') or {}
    soft  = caps.get('software') or []
    lone  = len(members) == 1 and members[0].get('derived')

    return {
        'resource'  : str(record.get('name', DASH)),
        'member'    : '' if not lone else str(members[0].get('member') or ''),
        'class_pool': '' if not lone else class_pool(members[0]),
        'site'      : str(record.get('site') or DASH),
        'size'      : size_of(members[0]) if lone else
                      '%d member%s' % (len(members),
                                       '' if len(members) == 1 else 's'),
        'software'  : ','.join(str(s) for s in soft) if soft else DASH,
        'node_hours': node_hours(record),
        'pilots'    : _num(usage.get('pilots_active')),
        'tasks'     : _tasks(usage),
        'liveness'  : str(record.get('liveness') or DASH),
    }


def member_row(record: Dict[str, Any],
               member: Dict[str, Any]) -> Dict[str, str]:
    """One indented row per member of a resource."""

    usage = member.get('usage') or {}
    attrs = member.get('attributes') or {}
    soft  = member.get('software') or []
    name  = str(member.get('member') or DASH)

    return {
        'resource'  : BRANCH + name,
        'member'    : name,
        'class_pool': class_pool(member),
        'site'      : str(attrs.get('site') or record.get('site') or DASH),
        'size'      : size_of(member),
        'software'  : ','.join(str(s) for s in soft) if soft else DASH,
        'node_hours': node_hours(member),
        'pilots'    : _num(usage.get('pilots_active')),
        'tasks'     : _tasks(usage),
        'liveness'  : str(member.get('liveness') or record.get('liveness')
                          or DASH),
    }


def rows_for(record: Dict[str, Any]) -> List[Dict[str, str]]:
    """The rows one resource contributes: itself, then its members."""

    members = members_of(record)
    out     = [row(record, members)]

    if len(members) == 1 and members[0].get('derived'):
        # nothing was grouped -- the resource row IS the member row
        return out

    return out + [member_row(record, m) for m in members]


def render(records: Sequence[Dict[str, Any]]) -> str:
    """The whole table (header + rows + legend)."""

    if not records:
        return ('no resources in the federation -- join one with '
                '`atomic-join --name … --mode …`')

    rows: List[Dict[str, str]] = []
    for record in records:
        rows += rows_for(record)

    widths = {key: max([len(head)] + [len(r[key]) for r in rows])
              for head, key in COLUMNS}

    lines = ['  '.join(head.ljust(widths[key]) for head, key in COLUMNS)]

    for r in rows:
        lines.append('  '.join(r[key].ljust(widths[key])
                               for _, key in COLUMNS).rstrip())

    lines.append('')
    lines.append('SIZE: nodes x cores/node (+GPUs/node, declared -- not '
                 'reserved)')
    lines.append('NODE-H: used/remaining   TASKS: running/done')

    if any('*' in r['node_hours'] for r in rows):
        lines.append('*: usage could not be refreshed -- values are stale')

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

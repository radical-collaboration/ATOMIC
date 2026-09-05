"""``atomic-resources`` -- list the resources in the ATOMIC federation.

Reads ``GET /broker/federation/resources/default`` and renders one row per
resource: what it is, what it offers, how much of its node-hour budget is
gone, and what it is doing right now.  ``--json`` prints the records as
they came from the broker (for scripts and the demo's own checks).

Usage figures are refreshed by the federation on every call; when that
refresh fails the record keeps its last values and is flagged
``"stale": true`` -- such rows are marked with a ``*`` rather than
hidden, because a stale number still says more than a blank.
"""

import argparse
import json
import sys

from typing import Any, Dict, List, Optional, Sequence

from ..client import Client, ClientError, add_connection_args

TOOL = 'atomic-resources'

# column header, and how wide it is at least
COLUMNS = [('NAME',     'name'),
           ('SITE',     'site'),
           ('MODE',     'mode'),
           ('CORES',    'cores'),
           ('GPUS',     'gpus'),
           ('MEM_GB',   'mem_gb'),
           ('SOFTWARE', 'software'),
           ('NODE-H',   'node_hours'),
           ('PILOTS',   'pilots'),
           ('TASKS',    'tasks'),
           ('LIVENESS', 'liveness')]

DASH = '-'


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
    """``used/remaining`` node-hours of a resource.

    ``remaining`` is what the federation reports; if it does not (yet)
    report one we derive it from the declared budget.
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


def row(record: Dict[str, Any]) -> Dict[str, str]:
    """One table row from a resource record."""

    caps  = record.get('capabilities') or {}
    usage = record.get('usage') or {}
    soft  = caps.get('software') or []

    return {
        'name'      : str(record.get('name', DASH)),
        'site'      : str(record.get('site') or DASH),
        'mode'      : str(record.get('mode') or DASH),
        'cores'     : _num(caps.get('cores')),
        'gpus'      : _num(caps.get('gpus')),
        'mem_gb'    : _num(caps.get('mem_gb')),
        'software'  : ','.join(str(s) for s in soft) if soft else DASH,
        'node_hours': node_hours(record),
        'pilots'    : _num(usage.get('pilots_active')),
        'tasks'     : '%s/%s' % (_num(usage.get('tasks_running')),
                                 _num(usage.get('tasks_done'))),
        'liveness'  : str(record.get('liveness') or DASH),
    }


def render(records: Sequence[Dict[str, Any]]) -> str:
    """The whole table (header + rows + legend)."""

    if not records:
        return ('no resources in the federation -- join one with '
                '`atomic-join --name … --mode …`')

    rows   = [row(r) for r in records]
    widths = {key: max([len(head)] + [len(r[key]) for r in rows])
              for head, key in COLUMNS}

    lines = ['  '.join(head.ljust(widths[key]) for head, key in COLUMNS)]

    for r in rows:
        lines.append('  '.join(r[key].ljust(widths[key])
                               for _, key in COLUMNS).rstrip())

    lines.append('')
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

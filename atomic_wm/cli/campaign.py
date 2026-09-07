"""``atomic-campaign`` -- submit and inspect ATOMIC campaigns.

    atomic-campaign submit SPEC.json --sweep temperature=300,600,900 [--wait]
    atomic-campaign status  CID
    atomic-campaign results CID [--json]
    atomic-campaign list
    atomic-campaign cancel  CID

``SPEC.json`` is either a bare workflow spec (``{"name", "params", "stages"}``)
or a full campaign request (``{"workflow": …, "sweep": …}`` -- e.g.
``examples/campaign_sweep.json``); ``--sweep`` overrides the sweep in the
file and may be repeated for a multi-parameter (cartesian) sweep.

Everything goes over HTTP to the gateway through :class:`atomic_wm.client
.Client`; the campaign routes always use the reserved session id ``default``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from typing import Any, Dict, List, Optional, Sequence

from ..client import ClientError, add_connection_args, client_from_args

TOOL = 'atomic-campaign'

POLL_SEC     = 2.0
TERMINAL     = ('DONE', 'FAILED', 'CANCELED', 'INTERRUPTED')
FAILED_STATES = ('FAILED', 'CANCELED', 'INTERRUPTED')


# ---------------------------------------------------------------------------
def _err(msg: str) -> int:
    sys.stderr.write('%s: error: %s\n' % (TOOL, msg))
    return 1


# ---------------------------------------------------------------------------
def parse_sweep(values: Optional[Sequence[str]]) -> Dict[str, List[Any]]:
    """``['temperature=300,600,900']`` -> ``{'temperature': [300,600,900]}``.

    Each value is parsed as JSON when it can be (so numbers stay numbers and
    ``"true"`` becomes a bool) and kept as a string otherwise.
    """

    sweep: Dict[str, List[Any]] = {}
    for item in values or []:
        if '=' not in item:
            raise ValueError('--sweep needs PARAM=v1,v2,... (got %r)' % item)
        key, _, raw = item.partition('=')
        key = key.strip()
        if not key:
            raise ValueError('--sweep needs a parameter name (got %r)' % item)
        parsed: List[Any] = []
        for part in raw.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                parsed.append(json.loads(part))
            except ValueError:
                parsed.append(part)
        if not parsed:
            raise ValueError('--sweep %r has no values' % key)
        sweep[key] = parsed
    return sweep


# ---------------------------------------------------------------------------
def load_spec(path: str) -> Dict[str, Any]:
    """Read a spec file; accept both the bare workflow and the full request."""

    with open(path, encoding='utf-8') as fd:
        data = json.load(fd)
    if not isinstance(data, dict):
        raise ValueError('%s: expected a JSON object' % path)
    if isinstance(data.get('workflow'), dict):
        return {'workflow': data['workflow'],
                'sweep'   : data.get('sweep') or {}}
    return {'workflow': data, 'sweep': {}}


# ---------------------------------------------------------------------------
def _params_label(params: Dict[str, Any]) -> str:
    if not params:
        return '-'
    return ' '.join('%s=%s' % (k, params[k]) for k in sorted(params))


# stage states in which no member has been bound yet, so any resource on
# the record is still advisory
_UNPLACED_STATES = ('PENDING', 'STAGING', 'SUBMITTED')


# ---------------------------------------------------------------------------
def _stage_cell(stage: Dict[str, Any]) -> str:
    """``md:DONE@local_b/cpu`` -- the placement the last poll reported.

    A capability class pool binds a member only when it dispatches, so a
    stage may legitimately carry no resource at all (queued, or its
    resource left the federation) -- then only name and state are shown.
    """

    state = str(stage.get('state') or '?')
    res   = stage.get('resource')
    mem   = stage.get('member')
    label = '%s:%s' % (stage.get('name') or '?', state)

    # while a stage is only submitted, `resource` is the advisory value
    # the class pool answered with -- the binding choice is made when the
    # dispatcher places the task, and `member_id` is how we know it was
    if not res or (state in _UNPLACED_STATES and not stage.get('member_id')):
        return label

    return '%s@%s%s' % (label, res, '/%s' % mem if mem else '')


# ---------------------------------------------------------------------------
def _print_table(rows: List[List[str]], header: List[str]) -> None:
    """Print a left-aligned table sized to its content."""

    widths = [len(h) for h in header]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    line = '  '.join(h.ljust(widths[i]) for i, h in enumerate(header))
    print(line.rstrip())
    print('  '.join('-' * w for w in widths))
    for row in rows:
        print('  '.join(c.ljust(widths[i])
                        for i, c in enumerate(row)).rstrip())


# ---------------------------------------------------------------------------
def print_campaign(camp: Dict[str, Any]) -> None:
    """The compact per-campaign table: workflow, params, stages."""

    state = camp.get('state') or '?'
    head  = '%s  %s  [%s]' % (camp.get('campaign_id') or '?',
                              camp.get('name') or '', state)
    print(head.strip())
    if camp.get('reason'):
        print('reason: %s' % camp['reason'])

    rows = []
    for wf in camp.get('workflows') or []:
        stages = ' '.join(_stage_cell(s) for s in (wf.get('stages') or []))
        rows.append([str(wf.get('id') or '?'),
                     _params_label(wf.get('params') or {}),
                     str(wf.get('state') or '?'),
                     stages or '-'])
    if rows:
        print()
        _print_table(rows, ['WORKFLOW', 'PARAMS', 'STATE', 'STAGES'])
    else:
        print('(no workflows)')


# ---------------------------------------------------------------------------
def print_results(results: Dict[str, Any]) -> None:
    """A scalar view of the collected metrics (``--json`` gives the rest)."""

    rows = []
    for wf in results.get('workflows') or []:
        params  = _params_label(wf.get('params') or {})
        metrics = wf.get('metrics') or {}
        if not metrics:
            rows.append([str(wf.get('id') or '?'), params,
                         '-', str(wf.get('state') or '?'), '-'])
            continue
        for stage in sorted(metrics):
            doc     = metrics[stage] or {}
            summary = doc.get('summary') if isinstance(doc, dict) else None
            if isinstance(summary, dict):
                text = ' '.join('%s=%s' % (k, _fmt(v))
                                for k, v in sorted(summary.items()))
            else:
                text = '(no summary)'
            rows.append([str(wf.get('id') or '?'), params, stage,
                         str(wf.get('state') or '?'), text])
    if not rows:
        print('no results collected yet')
        return
    _print_table(rows, ['WORKFLOW', 'PARAMS', 'STAGE', 'STATE', 'SUMMARY'])


# ---------------------------------------------------------------------------
def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return '%.4g' % value
    return str(value)


# ---------------------------------------------------------------------------
def cmd_submit(client: Any, args: argparse.Namespace) -> int:

    try:
        spec = load_spec(args.spec)
    except (OSError, ValueError) as exc:
        return _err(str(exc))

    try:
        sweep = parse_sweep(args.sweep)
    except ValueError as exc:
        return _err(str(exc))
    if not sweep:
        sweep = spec.get('sweep') or {}

    try:
        camp = client.campaign_submit(spec['workflow'], sweep)
    except ClientError as exc:
        return _err(str(exc))

    cid = camp.get('campaign_id')
    print('campaign %s submitted: %d workflow(s)'
          % (cid, len(camp.get('workflows') or [])))
    if not args.wait:
        print('follow with: %s status %s' % (TOOL, cid))
        return 0
    return wait_for(client, cid, args)


# ---------------------------------------------------------------------------
def wait_for(client: Any, cid: str, args: argparse.Namespace) -> int:
    """Poll until the campaign is terminal; non-zero exit if it did not pass.

    ``--timeout`` bounds the wait: on expiry the campaign is printed as it
    stands and the exit code is 3 (still running), so a script never hangs.
    """

    last     = None
    limit    = float(getattr(args, 'timeout', 0) or 0)
    deadline = (time.time() + limit) if limit > 0 else None
    while True:
        try:
            camp = client.campaign(cid)
        except ClientError as exc:
            return _err(str(exc))
        state = camp.get('state')
        if state != last:
            print('campaign %s: %s' % (cid, state))
            last = state
        if state in TERMINAL:
            print()
            print_campaign(camp)
            if state in FAILED_STATES:
                return 1
            return 0
        if deadline is not None and time.time() >= deadline:
            print()
            print_campaign(camp)
            _err('campaign %s still %s after %.0f s' % (cid, state, limit))
            return 3
        time.sleep(POLL_SEC)


# ---------------------------------------------------------------------------
def cmd_status(client: Any, args: argparse.Namespace) -> int:

    try:
        camp = client.campaign(args.cid)
    except ClientError as exc:
        return _err(str(exc))
    if args.json:
        print(json.dumps(camp, indent=2, default=str))
        return 0
    print_campaign(camp)
    return 0 if camp.get('state') not in FAILED_STATES else 1


# ---------------------------------------------------------------------------
def cmd_results(client: Any, args: argparse.Namespace) -> int:

    try:
        results = client.campaign_results(args.cid)
    except ClientError as exc:
        return _err(str(exc))
    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0
    print_results(results)
    return 0


# ---------------------------------------------------------------------------
def cmd_list(client: Any, args: argparse.Namespace) -> int:

    try:
        camps = client.campaigns()
    except ClientError as exc:
        return _err(str(exc))
    if isinstance(camps, dict):
        camps = camps.get('campaigns') or []
    if args.json:
        print(json.dumps(camps, indent=2, default=str))
        return 0
    if not camps:
        print('no campaigns')
        return 0
    rows = []
    for camp in camps:
        rows.append([str(camp.get('campaign_id') or '?'),
                     str(camp.get('name') or ''),
                     str(camp.get('state') or '?'),
                     str(camp.get('n_workflows')
                         if camp.get('n_workflows') is not None else '?'),
                     _stamp(camp.get('created_at')
                            or camp.get('started_at'))])
    _print_table(rows, ['CAMPAIGN', 'NAME', 'STATE', 'WORKFLOWS', 'STARTED'])
    return 0


# ---------------------------------------------------------------------------
def _stamp(value: Any) -> str:
    try:
        return time.strftime('%H:%M:%S', time.localtime(float(value)))
    except (TypeError, ValueError):
        return '-'


# ---------------------------------------------------------------------------
def cmd_cancel(client: Any, args: argparse.Namespace) -> int:

    try:
        out = client.campaign_cancel(args.cid)
    except ClientError as exc:
        return _err(str(exc))
    if out.get('canceled'):
        print('campaign %s: cancel requested' % args.cid)
    else:
        print('campaign %s: not canceled (%s)'
              % (args.cid, out.get('reason') or out.get('state') or '?'))
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        prog=TOOL,
        description='submit and inspect ATOMIC campaigns')
    add_connection_args(parser)
    subs = parser.add_subparsers(dest='command')

    sub = subs.add_parser('submit', help='submit a campaign')
    sub.add_argument('spec', metavar='SPEC.json',
                     help='workflow spec (or a full campaign request)')
    sub.add_argument('--sweep', action='append', metavar='PARAM=v1,v2,...',
                     help='sweep values; repeat for a cartesian sweep')
    sub.add_argument('--wait', action='store_true',
                     help='poll until the campaign is finished '
                          '(exit non-zero if it failed)')
    sub.add_argument('--timeout', type=float, default=0.0, metavar='SEC',
                     help='give up waiting after SEC seconds (exit 3); '
                          '0 (default) waits forever')
    sub.set_defaults(func=cmd_submit)

    sub = subs.add_parser('status', help='show one campaign')
    sub.add_argument('cid', metavar='CID')
    sub.add_argument('--json', action='store_true', help='raw JSON')
    sub.set_defaults(func=cmd_status)

    sub = subs.add_parser('results', help='show collected results')
    sub.add_argument('cid', metavar='CID')
    sub.add_argument('--json', action='store_true', help='raw JSON')
    sub.set_defaults(func=cmd_results)

    sub = subs.add_parser('list', help='list campaigns')
    sub.add_argument('--json', action='store_true', help='raw JSON')
    sub.set_defaults(func=cmd_list)

    sub = subs.add_parser('cancel', help='cancel a campaign')
    sub.add_argument('cid', metavar='CID')
    sub.set_defaults(func=cmd_cancel)

    return parser


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console script entry point."""

    parser = build_parser()
    args   = parser.parse_args(argv)
    if not getattr(args, 'command', None):
        parser.print_help()
        return 2

    try:
        client = client_from_args(args)
    except ValueError as exc:
        return _err(str(exc))

    try:
        return args.func(client, args)
    except KeyboardInterrupt:
        sys.stderr.write('\n%s: interrupted\n' % TOOL)
        return 130                                  # 128 + SIGINT
    except ClientError as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.exit(main())

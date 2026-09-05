#!/usr/bin/env python3
"""demo/local/smoke.py -- prove the ATOMIC WM demo actually ran.

Submits ``examples/workflow_vacancy.json`` as a campaign with the sweep
``temperature=300,600,900``, waits for it, and then asserts everything the
demo claims on stage:

1. the campaign reached ``DONE``,
2. three workflows, all ``DONE``, every stage placed on a resource,
3. every stage ran on a resource that advertises the software the stage
   requires (``md`` -> ``lammps``, ``train`` -> ``pytorch``),
4. at least two *distinct* resources were used -- the point of the
   federation is that the campaign spreads,
5. ``final_accuracy`` is strictly decreasing with temperature
   (300 > 600 > 900) -- the fake trainer guarantees this by construction,
   so a violation means results got mixed up between workflows,
6. the central store holds the six JSON outputs plus a manifest per stage.

Run it after ``demo/local/up.sh``::

    ve3/bin/python demo/local/smoke.py

Exit codes: 0 all good, 1 an assertion failed, 2 could not run at all
(broker unreachable, bad arguments, campaign never finished).

The module is written so that every check is a small pure function over
plain JSON -- ``demo/local/test_smoke_helpers.py`` exercises them with
canned payloads, no broker needed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TOOL = 'smoke.py'

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

DEFAULT_SPEC = os.path.join(REPO, 'examples', 'workflow_vacancy.json')
DEFAULT_SWEEP = 'temperature=300,600,900'

# campaign/workflow states we stop polling on
TERMINAL = frozenset(['DONE', 'FAILED', 'CANCELED', 'CANCELLED',
                      'INTERRUPTED', 'ABORTED'])

# what a healthy run ends in
SUCCESS = 'DONE'

# every collected stage carries one of these next to its outputs
MANIFEST = 'manifest.json'

EXIT_OK = 0
EXIT_ASSERT = 1
EXIT_ERROR = 2


# ---------------------------------------------------------------------------
class SmokeError(RuntimeError):
    """Something kept the smoke test from running (not an assertion)."""


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def log(msg: str = '') -> None:
    if msg:
        sys.stdout.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), msg))
    else:
        sys.stdout.write('\n')
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write('%s: %s\n' % (TOOL, msg))
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# tolerant accessors
# ---------------------------------------------------------------------------
#
# The campaign plugin is written against the same contract as this file,
# but a field may still be spelled slightly differently.  Reading through
# these helpers means a rename shows up as one clear failure message
# ("no resource recorded for ...") instead of a KeyError traceback.

def first(obj: Any, *keys: str, default: Any = None) -> Any:
    """First present, non-``None`` value among `keys` of a dict."""

    if not isinstance(obj, dict):
        return default

    for key in keys:
        val = obj.get(key)
        if val is not None:
            return val

    return default


def state_of(obj: Any) -> str:
    """Upper-cased ``state`` of a campaign / workflow / stage record."""

    return str(first(obj, 'state', 'status', default='') or '').upper()


def campaign_id_of(payload: Any) -> str:
    cid = first(payload, 'campaign_id', 'cid', 'id', default='')

    return str(cid or '')


def workflows_of(payload: Any) -> List[Dict[str, Any]]:
    items = first(payload, 'workflows', 'workflow_instances', default=[])

    if not isinstance(items, list):
        return []

    return [w for w in items if isinstance(w, dict)]


def workflow_id_of(wf: Any) -> str:
    return str(first(wf, 'id', 'wf_id', 'workflow_id', default='') or '')


def stages_of(wf: Any) -> List[Dict[str, Any]]:
    items = first(wf, 'stages', 'stage_runs', default=[])

    if not isinstance(items, list):
        return []

    return [s for s in items if isinstance(s, dict)]


def stage_name_of(stage: Any) -> str:
    return str(first(stage, 'name', 'stage', default='') or '')


def resource_of(stage: Any) -> str:
    """Resource a stage ran on (``''`` if the plugin recorded none)."""

    return str(first(stage, 'resource', 'resource_name', default='') or '')


def params_of(obj: Any) -> Dict[str, Any]:
    params = first(obj, 'params', 'parameters', default={})

    return params if isinstance(params, dict) else {}


def software_by_resource(records: Iterable[Any]) -> Dict[str, List[str]]:
    """``{resource name: [software, ...]}`` from federation records."""

    out: Dict[str, List[str]] = {}

    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        name = str(rec.get('name') or '')
        if not name:
            continue
        caps = rec.get('capabilities')
        soft = (caps or {}).get('software') if isinstance(caps, dict) else None
        if soft is None:
            soft = rec.get('software')
        out[name] = [str(s) for s in soft] if isinstance(soft, list) else []

    return out


# ---------------------------------------------------------------------------
# the workflow spec
# ---------------------------------------------------------------------------

def load_spec(path: str) -> Dict[str, Any]:
    """Read and minimally validate the workflow spec."""

    try:
        with open(path, encoding='utf-8') as fin:
            spec = json.load(fin)
    except OSError as e:
        raise SmokeError('cannot read workflow spec %s: %s' % (path, e))
    except ValueError as e:
        raise SmokeError('workflow spec %s is not valid JSON: %s' % (path, e))

    if not isinstance(spec, dict) or not isinstance(spec.get('stages'), list):
        raise SmokeError('workflow spec %s has no "stages" list' % path)

    return spec


def stage_requirements(spec: Dict[str, Any]) -> Dict[str, List[str]]:
    """``{stage name: [required software, ...]}`` from the spec."""

    out: Dict[str, List[str]] = {}

    for stage in spec.get('stages') or []:
        if not isinstance(stage, dict):
            continue
        name = str(stage.get('name') or '')
        if not name:
            continue
        reqs = stage.get('requirements')
        soft = (reqs or {}).get('software') if isinstance(reqs, dict) else None
        out[name] = [str(s) for s in soft] if isinstance(soft, list) else []

    return out


def stage_outputs(spec: Dict[str, Any]) -> Dict[str, List[str]]:
    """``{stage name: [declared output file, ...]}`` from the spec."""

    out: Dict[str, List[str]] = {}

    for stage in spec.get('stages') or []:
        if not isinstance(stage, dict):
            continue
        name = str(stage.get('name') or '')
        if not name:
            continue
        files = stage.get('outputs')
        out[name] = [str(f) for f in files] if isinstance(files, list) else []

    return out


def parse_sweep(text: str) -> Dict[str, List[Any]]:
    """``'temperature=300,600,900'`` -> ``{'temperature': [300, 600, 900]}``.

    Several parameters are separated by ``;``.  Values are converted to
    int/float where they look numeric, so the sweep matches what the
    workflow spec's ``params`` hold.
    """

    sweep: Dict[str, List[Any]] = {}

    for part in (text or '').split(';'):
        part = part.strip()
        if not part:
            continue
        if '=' not in part:
            raise SmokeError('--sweep expects KEY=V1,V2,..., got %r' % part)
        key, _, raw = part.partition('=')
        key = key.strip()
        values = [v.strip() for v in raw.split(',') if v.strip()]
        if not key or not values:
            raise SmokeError('--sweep expects KEY=V1,V2,..., got %r' % part)
        sweep[key] = [_number(v) for v in values]

    if not sweep:
        raise SmokeError('--sweep is empty')

    return sweep


def _number(text: str) -> Any:
    """``'300'`` -> 300, ``'0.5'`` -> 0.5, anything else stays a string."""

    try:
        return int(text)
    except ValueError:
        pass

    try:
        return float(text)
    except ValueError:
        return text


# ---------------------------------------------------------------------------
# placements
# ---------------------------------------------------------------------------

class Placement:
    """One stage of one workflow, and where it ran."""

    def __init__(self, wf_id: str, params: Dict[str, Any], stage: str,
                 state: str, resource: str, task_id: str):
        self.wf_id = wf_id
        self.params = params
        self.stage = stage
        self.state = state
        self.resource = resource
        self.task_id = task_id

    def __repr__(self) -> str:                              # pragma: no cover
        return ('Placement(%s, %s, %s, %s)'
                % (self.wf_id, self.stage, self.state, self.resource))


def placements_of(campaign: Any) -> List[Placement]:
    """Flatten a campaign record into one entry per stage run."""

    out: List[Placement] = []

    for wf in workflows_of(campaign):
        wf_id = workflow_id_of(wf)
        params = params_of(wf)
        for stage in stages_of(wf):
            out.append(Placement(
                wf_id=wf_id,
                params=params,
                stage=stage_name_of(stage),
                state=state_of(stage),
                resource=resource_of(stage),
                task_id=str(first(stage, 'task_id', 'task',
                                  default='') or '')))

    return out


def render_placements(places: Sequence[Placement],
                      sweep_key: str = 'temperature') -> str:
    """A compact `workflow / param / stage / resource` table."""

    head = ('workflow', sweep_key, 'stage', 'state', 'resource', 'task')
    rows = [head]

    for pl in places:
        rows.append((pl.wf_id or '-',
                     str(pl.params.get(sweep_key, '-')),
                     pl.stage or '-',
                     pl.state or '-',
                     pl.resource or '-',
                     pl.task_id or '-'))

    widths = [max(len(row[i]) for row in rows) for i in range(len(head))]
    lines = []

    for idx, row in enumerate(rows):
        lines.append('  '.join(cell.ljust(widths[i])
                               for i, cell in enumerate(row)).rstrip())
        if idx == 0:
            lines.append('  '.join('-' * w for w in widths))

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def final_accuracy_of(envelope: Any) -> Optional[float]:
    """``final_accuracy`` out of a training stage's output envelope."""

    if not isinstance(envelope, dict):
        return None

    summary = envelope.get('summary')
    for holder in (summary, envelope):
        if isinstance(holder, dict):
            val = holder.get('final_accuracy')
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val)

    return None


def accuracy_series(results: Any, stage: str,
                    sweep_key: str = 'temperature'
                    ) -> List[Tuple[Any, Optional[float], str]]:
    """``[(param value, final_accuracy, workflow id), ...]``, sorted by param.

    Entries whose parameter value is not numeric keep their relative order
    at the end -- the caller reports them as failures.
    """

    rows: List[Tuple[Any, Optional[float], str]] = []

    for wf in workflows_of(results):
        metrics = first(wf, 'metrics', 'results', default={})
        envelope = metrics.get(stage) if isinstance(metrics, dict) else None
        rows.append((params_of(wf).get(sweep_key),
                     final_accuracy_of(envelope),
                     workflow_id_of(wf)))

    def key(row: Tuple[Any, Optional[float], str]) -> Tuple[int, float]:
        val = row[0]
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return (0, float(val))
        return (1, 0.0)

    return sorted(rows, key=key)


# ---------------------------------------------------------------------------
# checks -- each returns a list of failure descriptions (empty == passed)
# ---------------------------------------------------------------------------

def check_campaign_done(campaign: Any) -> List[str]:

    state = state_of(campaign)

    if state == SUCCESS:
        return []

    reason = first(campaign, 'error', 'reason', 'message', default='')

    return ['campaign state is %r, expected %r%s'
            % (state or '<missing>', SUCCESS,
               ' (%s)' % reason if reason else '')]


def check_workflows(campaign: Any, expected: int,
                    stages: Sequence[str]) -> List[str]:
    """Count, states and stage completeness of the workflows."""

    fails: List[str] = []
    wfs = workflows_of(campaign)

    if len(wfs) != expected:
        fails.append('campaign has %d workflows, expected %d'
                     % (len(wfs), expected))

    for wf in wfs:
        wid = workflow_id_of(wf) or '<unnamed>'
        state = state_of(wf)
        if state != SUCCESS:
            reason = first(wf, 'error', 'reason', 'message', default='')
            fails.append('workflow %s is %r, expected %r%s'
                         % (wid, state or '<missing>', SUCCESS,
                            ' (%s)' % reason if reason else ''))

        found = [stage_name_of(s) for s in stages_of(wf)]
        for name in stages:
            if name not in found:
                fails.append('workflow %s has no stage %r (has: %s)'
                             % (wid, name, ', '.join(found) or 'none'))

        for stage in stages_of(wf):
            if state_of(stage) != SUCCESS:
                fails.append('workflow %s stage %s is %r, expected %r'
                             % (wid, stage_name_of(stage) or '<unnamed>',
                                state_of(stage) or '<missing>', SUCCESS))

    return fails


def check_placement(places: Sequence[Placement],
                    software: Dict[str, List[str]],
                    requirements: Dict[str, List[str]],
                    min_resources: int = 2) -> List[str]:
    """Every stage on a capable resource, and the campaign spread out."""

    fails: List[str] = []
    used = set()

    for pl in places:

        if not pl.resource:
            fails.append('no resource recorded for workflow %s stage %s'
                         % (pl.wf_id or '<unnamed>', pl.stage or '<unnamed>'))
            continue

        used.add(pl.resource)

        if pl.resource not in software:
            fails.append('workflow %s stage %s ran on %r, which the '
                         'federation does not list (known: %s)'
                         % (pl.wf_id, pl.stage, pl.resource,
                            ', '.join(sorted(software)) or 'none'))
            continue

        have = set(software[pl.resource])
        need = set(requirements.get(pl.stage, []))
        missing = sorted(need - have)

        if missing:
            fails.append('stage %s of workflow %s ran on %r, which does not '
                         'advertise %s (it has: %s)'
                         % (pl.stage, pl.wf_id, pl.resource,
                            ', '.join(missing),
                            ', '.join(sorted(have)) or 'nothing'))

    if len(used) < min_resources:
        fails.append('the campaign used %d resource(s) (%s), expected at '
                     'least %d -- it did not spread across the federation'
                     % (len(used), ', '.join(sorted(used)) or 'none',
                        min_resources))

    return fails


def check_accuracy(results: Any, stage: str, expected: int,
                   sweep_key: str = 'temperature') -> List[str]:
    """Three accuracies, strictly decreasing with temperature."""

    fails: List[str] = []
    rows = accuracy_series(results, stage, sweep_key)

    if len(rows) != expected:
        fails.append('results carry %d workflows, expected %d'
                     % (len(rows), expected))

    usable: List[Tuple[float, float, str]] = []

    for value, acc, wid in rows:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            fails.append('workflow %s has no numeric %s (got %r)'
                         % (wid or '<unnamed>', sweep_key, value))
            continue
        if acc is None:
            fails.append('workflow %s (%s=%s) has no %s.summary.'
                         'final_accuracy in its results'
                         % (wid or '<unnamed>', sweep_key, value, stage))
            continue
        usable.append((float(value), acc, wid))

    for (v0, a0, w0), (v1, a1, w1) in zip(usable, usable[1:]):
        if not a0 > a1:
            fails.append('final_accuracy is not strictly decreasing with %s: '
                         '%s=%g -> %.6f (%s) but %s=%g -> %.6f (%s)'
                         % (sweep_key, sweep_key, v0, a0, w0,
                            sweep_key, v1, a1, w1))

    return fails


def check_store(store_root: str, cid: str, campaign: Any,
                outputs: Dict[str, List[str]]) -> List[str]:
    """The central store holds every declared output plus its manifest."""

    fails: List[str] = []

    cdir = os.path.join(store_root, cid)

    if not os.path.isdir(cdir):
        siblings = _listdir(store_root)
        return ['no campaign directory %s (store holds: %s)'
                % (cdir, ', '.join(siblings) or 'nothing')]

    for wf in workflows_of(campaign):

        wid = workflow_id_of(wf)
        wdir = os.path.join(cdir, wid)

        if not os.path.isdir(wdir):
            fails.append('no store directory for workflow %s: %s '
                         '(campaign dir holds: %s)'
                         % (wid or '<unnamed>', wdir,
                            ', '.join(_listdir(cdir)) or 'nothing'))
            continue

        for stage in stages_of(wf):

            sname = stage_name_of(stage)
            sdir = os.path.join(wdir, sname)

            if not os.path.isdir(sdir):
                fails.append('no store directory for stage %s of workflow '
                             '%s: %s (workflow dir holds: %s)'
                             % (sname, wid, sdir,
                                ', '.join(_listdir(wdir)) or 'nothing'))
                continue

            wanted = list(outputs.get(sname, [])) + [MANIFEST]

            for fname in wanted:
                path = os.path.join(sdir, fname)
                if not os.path.isfile(path):
                    fails.append('missing %s in %s (holds: %s)'
                                 % (fname, sdir,
                                    ', '.join(_listdir(sdir)) or 'nothing'))
                    continue
                if fname.endswith('.json'):
                    fails.extend(_bad_json(path))

    return fails


def _listdir(path: str) -> List[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _bad_json(path: str) -> List[str]:
    try:
        with open(path, encoding='utf-8') as fin:
            json.load(fin)
    except (OSError, ValueError) as e:
        return ['%s is not readable JSON: %s' % (path, e)]

    return []


# ---------------------------------------------------------------------------
# talking to the broker
# ---------------------------------------------------------------------------

# variables smoke.py needs and env.sh owns; kept short on purpose -- the
# rest of env.sh only matters to processes the demo *starts*
DEMO_ENV_KEYS = ('RADICAL_ORBIT_BROKER_URL', 'RADICAL_ORBIT_BROKER_CERT',
                 'ATOMIC_STORE_ROOT', 'ATOMIC_WM_STATE')


def load_demo_env(env_sh: Optional[str] = None) -> List[str]:
    """Fill in demo defaults from ``env.sh`` when nobody sourced it.

    The documented sequence is ``./demo/local/up.sh && ve3/bin/python
    demo/local/smoke.py``, and ``up.sh`` exports into its own subshell --
    so by the time smoke.py runs, the caller's environment may know
    nothing about the demo.  Rather than duplicate the defaults here,
    read them back out of ``env.sh`` itself.

    Only *missing* variables are filled in: an explicit setting in the
    caller's environment always wins.  Returns the names it set.
    """

    env_sh = env_sh or os.path.join(HERE, 'env.sh')

    if os.environ.get('RADICAL_ORBIT_BROKER_URL'):
        return []

    if not os.path.isfile(env_sh):
        return []

    try:
        proc = subprocess.run(
            ['bash', '-c', 'source "$1" > /dev/null 2>&1; env -0', '_', env_sh],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        warn('could not read %s (%s) -- pass --broker explicitly' % (env_sh, e))
        return []

    added = []

    for item in proc.stdout.decode('utf-8', 'replace').split('\0'):
        key, _, value = item.partition('=')
        if key in DEMO_ENV_KEYS and value and not os.environ.get(key):
            os.environ[key] = value
            added.append(key)

    return added


def make_client(args: argparse.Namespace) -> Any:
    """A ``atomic_wm.client.Client`` -- imported here, not at module level.

    Keeping the import lazy means every helper above can be unit tested
    with a plain python that has no ``requests`` and no ``atomic_wm``.
    """

    try:
        from atomic_wm.client import Client
    except ImportError as e:
        raise SmokeError('cannot import atomic_wm.client (%s) -- run this '
                         'with the orbit venv python, after '
                         'demo/local/up.sh has installed atomic-wm[cli]' % e)

    try:
        return Client(broker=args.broker, token=args.token, cert=args.cert)
    except ValueError as e:
        raise SmokeError(str(e))


def progress_of(campaign: Any) -> str:
    """``'DONE 2/6 RUNNING 1/6'``-ish one-liner for the wait loop."""

    counts: Dict[str, int] = {}
    total = 0

    for pl in placements_of(campaign):
        total += 1
        counts[pl.state or '?'] = counts.get(pl.state or '?', 0) + 1

    if not total:
        return 'no stages yet'

    return ' '.join('%s %d/%d' % (state, counts[state], total)
                    for state in sorted(counts))


def wait_for_campaign(client: Any, cid: str, timeout: float,
                      poll: float) -> Dict[str, Any]:
    """Poll ``campaign(cid)`` until its **state** is terminal.

    Never times out on pilot state -- pilots warm up on their own schedule
    and a slow pilot is not a failure, a stuck task is.
    """

    deadline = time.time() + timeout
    last = ''
    campaign: Dict[str, Any] = {}

    while True:

        campaign = client.campaign(cid) or {}
        state = state_of(campaign)
        line = '%s -- %s' % (state or '?', progress_of(campaign))

        if line != last:
            log('campaign : %s' % line)
            last = line

        if state in TERMINAL:
            return campaign

        if time.time() >= deadline:
            raise SmokeError('campaign %s still %r after %.0fs (%s)'
                             % (cid, state or '<missing>', timeout,
                                progress_of(campaign)))

        time.sleep(poll)


def store_root_of(args: argparse.Namespace, client: Any) -> str:
    """Where the campaign plugin copies collected outputs to."""

    if args.store_root:
        return os.path.expanduser(args.store_root)

    env = os.environ.get('ATOMIC_STORE_ROOT')
    if env:
        return os.path.expanduser(env)

    # last resort: ask the plugin (GET store/default), then the default
    # from the contract
    try:
        info = client.request('GET', '/broker/atomic_campaign/store/default')
        root = first(info, 'root', 'store_root', 'path', default='')
        if root:
            return os.path.expanduser(str(root))
    except Exception as e:                          # noqa: BLE001 - advisory
        warn('could not ask the campaign plugin for its store root: %s' % e)

    return os.path.expanduser('~/.radical/orbit/atomic_store')


def dump(path: str, payload: Any) -> None:
    """Keep a copy of a payload next to the logs -- gold for debugging."""

    try:
        with open(path, 'w', encoding='utf-8') as fout:
            json.dump(payload, fout, indent=2, sort_keys=True)
            fout.write('\n')
    except (OSError, TypeError, ValueError) as e:
        warn('could not write %s: %s' % (path, e))


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        prog=TOOL,
        description='end-to-end smoke test for the ATOMIC WM local demo')

    # deliberately not imported from atomic_wm.client.add_connection_args:
    # this module must stay importable without `requests`
    parser.add_argument('--broker', default=None,
                        help='gateway URL (default $RADICAL_ORBIT_BROKER_URL)')
    parser.add_argument('--token', default=None,
                        help='bearer token (default $RADICAL_ORBIT_TOKEN)')
    parser.add_argument('--cert', default=None,
                        help='broker TLS cert '
                             '(default $RADICAL_ORBIT_BROKER_CERT)')

    parser.add_argument('--spec', default=DEFAULT_SPEC, metavar='FILE',
                        help='workflow spec to submit (default: %(default)s)')
    parser.add_argument('--sweep', default=DEFAULT_SWEEP, metavar='K=V,V,…',
                        help='parameter sweep (default: %(default)s)')
    parser.add_argument('--timeout', type=float, default=600.0, metavar='SEC',
                        help='how long the campaign may take '
                             '(default: %(default)s)')
    parser.add_argument('--poll', type=float, default=3.0, metavar='SEC',
                        help='poll interval (default: %(default)s)')
    parser.add_argument('--min-resources', type=int, default=2, metavar='N',
                        help='distinct resources the campaign must use '
                             '(default: %(default)s)')
    parser.add_argument('--store-root', default=None, metavar='DIR',
                        help='central store (default $ATOMIC_STORE_ROOT, '
                             'else the plugin is asked)')
    parser.add_argument('--run-dir', default=os.path.join(HERE, 'run'),
                        metavar='DIR',
                        help='where logs and payload dumps go '
                             '(default: %(default)s)')
    parser.add_argument('--no-store-check', action='store_true',
                        help='skip the central-store assertions (for a '
                             'broker on another host)')

    return parser


# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:

    started = time.time()

    spec = load_spec(args.spec)
    sweep = parse_sweep(args.sweep)

    sweep_key = sorted(sweep)[0]
    expected = 1
    for values in sweep.values():
        expected *= len(values)

    requirements = stage_requirements(spec)
    outputs = stage_outputs(spec)
    stages = list(requirements)

    if not stages:
        raise SmokeError('workflow spec %s declares no named stages'
                         % args.spec)

    # the training stage is the one whose accuracy we compare; by
    # construction that is the last stage of the workflow
    metric_stage = stages[-1]

    run_dir = os.path.abspath(args.run_dir)
    os.makedirs(run_dir, exist_ok=True)

    added = load_demo_env()
    if added:
        log('env      : took %s from demo/local/env.sh'
            % ', '.join(sorted(added)))

    client = make_client(args)

    log('broker   : %s' % client.broker)
    log('spec     : %s (%s)' % (args.spec, ' -> '.join(stages)))
    log('sweep    : %s (%d workflows)'
        % (', '.join('%s=%s' % (k, ','.join(str(v) for v in vals))
                     for k, vals in sorted(sweep.items())), expected))

    resources = client.fed_resources()
    software = software_by_resource(resources)

    if not software:
        raise SmokeError('the federation lists no resources -- did '
                         'demo/local/up.sh finish?')

    log('resources: %s'
        % '; '.join('%s [%s]' % (name, ','.join(software[name]) or '-')
                    for name in sorted(software)))

    submitted = client.campaign_submit(spec, sweep)
    cid = campaign_id_of(submitted)

    if not cid:
        raise SmokeError('campaign submit returned no campaign_id: %r'
                         % (submitted,))

    log('campaign : %s submitted' % cid)

    campaign = wait_for_campaign(client, cid, args.timeout, args.poll)
    results = client.campaign_results(cid) or {}

    # refresh: usage counters only settle once the tasks are done
    try:
        software = software_by_resource(client.fed_resources()) or software
    except Exception as e:                          # noqa: BLE001 - advisory
        warn('could not refresh the resource list: %s' % e)

    dump(os.path.join(run_dir, 'smoke-campaign.json'), campaign)
    dump(os.path.join(run_dir, 'smoke-results.json'), results)

    places = placements_of(campaign)

    log()
    sys.stdout.write(render_placements(places, sweep_key) + '\n')
    log()

    for value, acc, wid in accuracy_series(results, metric_stage, sweep_key):
        log('accuracy : %s=%-6s %s  final_accuracy=%s'
            % (sweep_key, value, wid,
               '%.6f' % acc if acc is not None else '<missing>'))
    log()

    fails = gather_failures(campaign=campaign, results=results,
                            places=places, software=software,
                            requirements=requirements, stages=stages,
                            metric_stage=metric_stage, expected=expected,
                            sweep_key=sweep_key,
                            min_resources=args.min_resources)

    fails += store_failures(args, client, cid, campaign, outputs)

    elapsed = time.time() - started

    if fails:
        return report_failures(fails, run_dir, elapsed)

    log('OK       : campaign %s, %d workflows, %d stages, %d resources, '
        '%.0fs' % (cid, expected, len(places),
                   len({p.resource for p in places if p.resource}), elapsed))

    return EXIT_OK


# ---------------------------------------------------------------------------
def gather_failures(campaign: Any, results: Any,
                    places: Sequence[Placement],
                    software: Dict[str, List[str]],
                    requirements: Dict[str, List[str]],
                    stages: Sequence[str], metric_stage: str,
                    expected: int, sweep_key: str,
                    min_resources: int) -> List[str]:
    """Every assertion the demo makes, as a flat list of descriptions."""

    fails: List[str] = []

    fails += check_campaign_done(campaign)
    fails += check_workflows(campaign, expected, stages)
    fails += check_placement(places, software, requirements, min_resources)
    fails += check_accuracy(results, metric_stage, expected, sweep_key)

    return fails


# ---------------------------------------------------------------------------
def store_failures(args: argparse.Namespace, client: Any, cid: str,
                   campaign: Any,
                   outputs: Dict[str, List[str]]) -> List[str]:
    """The central-store assertions (skippable for a remote broker)."""

    if args.no_store_check:
        log('store    : checks skipped (--no-store-check)')
        return []

    store_root = store_root_of(args, client)

    log('store    : %s' % os.path.join(store_root, cid))

    return check_store(store_root, cid, campaign, outputs)


# ---------------------------------------------------------------------------
def report_failures(fails: Sequence[str], run_dir: str,
                    elapsed: float) -> int:
    """Print the failed assertions and where to look next."""

    log()
    warn('%d assertion(s) failed after %.0fs:' % (len(fails), elapsed))

    for idx, item in enumerate(fails, 1):
        warn('  %d. %s' % (idx, item))

    warn('')
    warn('logs to read:')
    warn('  %s/broker.log -- plugin errors, task submission' % run_dir)
    warn('  %s/smoke-campaign.json, %s/smoke-results.json' % (run_dir,
                                                              run_dir))
    warn('  %s/<resource>/endpoint.log -- pilot start-up'
         % os.environ.get('ATOMIC_WM_STATE', '~/.radical/orbit/atomic'))
    warn('  demo/local/README.md, section "Troubleshooting"')

    return EXIT_ASSERT


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:

    args = build_parser().parse_args(argv)

    try:
        return run(args)
    except SmokeError as e:
        warn(str(e))
        return EXIT_ERROR
    except KeyboardInterrupt:                               # pragma: no cover
        warn('interrupted')
        return EXIT_ERROR
    except Exception as e:                                  # noqa: BLE001
        # a client error (broker down, 4xx/5xx from a plugin) lands here;
        # print it with its type so the cause is obvious
        warn('%s: %s' % (type(e).__name__, e))
        warn(hint_for(str(e)))
        return EXIT_ERROR


# ---------------------------------------------------------------------------
def hint_for(message: str) -> str:
    """Turn a known-shaped transport error into an actionable line."""

    text = message.lower()

    if 'certificate_verify_failed' in text or 'certificate is not valid' \
            in text:
        return ('hint: the demo broker cert is CN=localhost.localdomain '
                'with no SAN, so it never matches 127.0.0.1 by hostname.  '
                'A *pinned* cert authenticates the peer on its own, so a '
                'client must verify it with check_hostname=False -- which '
                'radical.orbit (runtime.py::_tls_context) and '
                'atomic_wm.client.Client both do.  Seeing this means '
                '$RADICAL_ORBIT_BROKER_CERT points at the wrong file, or '
                'the client lost that pinning.')

    if 'connection refused' in text or 'cannot reach broker' in text:
        return ('hint: no broker on that URL -- run demo/local/up.sh, or '
                'check demo/local/run/broker.log')

    if '503' in text and 'federation' in text:
        return ('hint: 503 from the federation usually means the broker was '
                'started without the plugin -- check the --plugins line in '
                'demo/local/run/broker.log')

    return 'see demo/local/README.md, section "Troubleshooting"'


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.exit(main())

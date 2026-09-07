"""``atomic-join`` -- start an endpoint and join it to the federation.

One command turns a machine -- or a running compute allocation -- into a
federated resource:

1. start an orbit endpoint as a child process (``ep_<name>``),
2. wait until the broker reports it as connected,
3. detect the machine's capabilities (``sysinfo``, and in allocation mode
   ``queue_info/job_allocation``); ``--declare`` overrides what is
   detected,
4. ``POST /broker/federation/join/default`` with the assembled resource
   record,
5. stay in the foreground until Ctrl-C, then leave the federation and
   stop the endpoint again (``--detach`` instead writes a pidfile and
   exits -- ``atomic-leave <name>`` then does the teardown).

See ``docs/join.md``; the resource record is specified in
``plans/00-overview.md`` §"The contract".
"""

import argparse
import os
import re
import signal
import sys
import threading
import time

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import endpoint_proc
from ..client import Client, ClientError, add_connection_args
from ..endpoint_proc import EndpointError, EndpointProcess

TOOL = 'atomic-join'

# resource names travel through pool names, directory names and URLs
NAME_RE   = re.compile(r'^[a-z0-9][a-z0-9_.-]*$')

# A member's short name carries NO dot: the member id the dispatcher sees is
# '<resource>.<member>' and a resource name may contain dots, so the *last*
# dot is the separator and the member half must not add more.
MEMBER_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')

# a capability class names a pool ('fed-<class>').  A name that does not
# match is rejected, never lower-cased: silently turning 'GPU' into 'gpu'
# is how a second, invisible pool appears.
CLASS_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')

# capability keys which can be declared (and their type)
DECLARE_KEYS = {'cores': int, 'gpus': int, 'mem_gb': float}

# `--member` keys which map onto a member's pool description.  Everything
# NOT listed here becomes an entry in the member's `attributes` -- which is
# how free-form labels (site=…, mem_gb_per_node=…) reach the dispatcher.
MEMBER_INT_KEYS = {'nodes'     : 'nodes',
                   'cpus'      : 'cpus_per_node',
                   'gpus'      : 'gpus_per_node',
                   'walltime'  : 'walltime_sec',
                   'min_pilots': 'min_pilots',
                   'max_pilots': 'max_pilots'}

MEMBER_STR_KEYS = {'queue'  : 'queue',
                   'account': 'account',
                   'class'  : 'class',
                   'scratch': 'scratch_base',
                   'backend': 'rhapsody_backend'}

# keys with a shape of their own
MEMBER_LIST_KEYS  = {'software'}
MEMBER_FLOAT_KEYS = {'node_hours'}
MEMBER_BOOL_KEYS  = {'shared_fs'}

MEMBER_KEYS = (set(MEMBER_INT_KEYS) | set(MEMBER_STR_KEYS)
               | MEMBER_LIST_KEYS | MEMBER_FLOAT_KEYS | MEMBER_BOOL_KEYS)

# what a member must describe before the federation can size a pilot for it
MEMBER_REQUIRED = ['queue', 'nodes', 'cpus', 'walltime', 'node_hours']

BOOL_WORDS = {'true' : True,  'yes': True,  'on' : True,  '1': True,
              'false': False, 'no' : False, 'off': False, '0': False}

# how long we wait for the endpoint to show up as connected
CONNECT_TIMEOUT = 60.0

# poll intervals of the connect wait / the foreground loop / the liveness
# report in the foreground loop
POLL_INTERVAL     = 1.0
LOOP_INTERVAL     = 1.0
LIVENESS_INTERVAL = 10.0

GIB = 1024 ** 3


# ---------------------------------------------------------------------------
class UsageError(Exception):
    """Bad command line -- reported as a usage error (exit 2)."""


class JoinError(Exception):
    """The join could not be completed (exit 1); carries a hint."""

    def __init__(self, msg: str, hint: str = ''):
        super().__init__(msg)
        self.hint = hint


class Interrupted(Exception):
    """A signal arrived before the join was complete."""


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

def info(msg: str) -> None:
    sys.stdout.write('%s\n' % msg)
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write('%s: warning: %s\n' % (TOOL, msg))


def error(msg: str, hint: str = '') -> None:
    sys.stderr.write('%s: error: %s\n' % (TOOL, msg))
    if hint:
        sys.stderr.write('%s\n' % hint)


# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """The ``atomic-join`` command line."""

    parser = argparse.ArgumentParser(
        prog=TOOL,
        description='start an orbit endpoint and join it to the ATOMIC '
                    'resource federation',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='examples:\n'
               '  %(prog)s --broker https://r3:8003 --name perlmutter_a \\\n'
               '      --mode allocation --site NERSC --software lammps,pytorch\n'
               '  %(prog)s --broker https://psc:8003 --name bridges_login \\\n'
               '      --mode login --site PSC --queue RM --account abc123 \\\n'
               '      --nodes 1 --cpus 128 --walltime 3600 --node-hours 20\n')

    add_connection_args(parser)

    parser.add_argument('--name', required=True,
                        help='resource name, unique in the federation')
    parser.add_argument('--mode', required=True,
                        choices=['allocation', 'login'],
                        help='allocation: this process runs inside a compute '
                             'allocation and the whole allocation is the '
                             'resource; login: pilots are submitted to the '
                             'batch system on demand')
    parser.add_argument('--site', default='',
                        help='site the resource belongs to (free text)')
    parser.add_argument('--kind', default='workstation',
                        choices=['hpc', 'cluster', 'workstation'],
                        help='what kind of machine this is '
                             '(default: workstation)')
    parser.add_argument('--declare', default=None, metavar='K=V,…',
                        help='override detected capabilities: '
                             'cores=,gpus=,mem_gb=')
    parser.add_argument('--software', action='append', default=[],
                        metavar='NAME,…',
                        help='software available here (comma separated, '
                             'may be repeated)')
    parser.add_argument('--node-hours', type=float, default=None,
                        metavar='H',
                        help='node-hour budget this join contributes '
                             '(required in login mode; in allocation mode '
                             'the federation derives one from the '
                             'allocation if you do not give it)')
    parser.add_argument('--scratch', default=None, metavar='DIR',
                        help='scratch base for tasks on this resource '
                             '(must be under $HOME or /tmp)')

    grp = parser.add_argument_group('login mode')
    grp.add_argument('--member', action='append', default=[],
                     metavar='NAME:K=V,…',
                     help='declare one member of this resource -- one shape '
                          'of pilot it is willing to run.  Repeatable; '
                          'mutually exclusive with the flat flags below.  '
                          'Keys: queue, account, nodes, cpus, gpus, '
                          'walltime, min_pilots, max_pilots, node_hours, '
                          'software, class, scratch, shared_fs, backend; '
                          'any other key becomes an attribute '
                          '(site=NERSC).  A comma separated value list '
                          'continues the previous key, so '
                          '"software=lammps,pytorch" is one list.  '
                          'Example: --member '
                          'gpu:queue=gpu,nodes=1,cpus=64,gpus=8,'
                          'walltime=3600,node_hours=8,software=pytorch,'
                          'site=NERSC')
    grp.add_argument('--queue',   default=None,
                     help='batch queue/partition pilots are submitted to')
    grp.add_argument('--account', default=None,
                     help='allocation account charged for pilots')
    grp.add_argument('--nodes',   type=int, default=None,
                     help='nodes per pilot')
    grp.add_argument('--cpus',    type=int, default=None, metavar='N',
                     help='cores per node')
    grp.add_argument('--gpus-per-node', type=int, default=None, metavar='N',
                     help='GPUs per node (default: 0)')
    grp.add_argument('--walltime', type=int, default=None, metavar='SEC',
                     help='pilot walltime in seconds')
    grp.add_argument('--max-pilots', type=int, default=None, metavar='N',
                     help='concurrent pilots allowed (default: 2)')
    grp.add_argument('--rhapsody-backend', default='concurrent',
                     help='rhapsody backend the pilots run '
                          '(default: concurrent)')

    grp = parser.add_argument_group('endpoint process')
    grp.add_argument('--endpoint', default=None, metavar='NAME',
                     help='endpoint name (default: ep_<name>)')
    grp.add_argument('--plugins', default='default',
                     help='endpoint plugins to load (default: default)')
    grp.add_argument('--endpoint-bin', default=None, metavar='PATH',
                     help='path to radical-orbit-endpoint.py '
                          '(default: $ATOMIC_ENDPOINT_BIN, then $PATH)')
    grp.add_argument('--log-level', default='INFO',
                     choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                     help='endpoint log level (default: INFO)')
    grp.add_argument('--connect-timeout', type=float,
                     default=CONNECT_TIMEOUT, metavar='SEC',
                     help='how long to wait for the endpoint to connect '
                          '(default: %d)' % CONNECT_TIMEOUT)
    grp.add_argument('--detach', action='store_true',
                     help='write a pidfile and exit, leaving the endpoint '
                          'running (teardown: atomic-leave <name>)')

    return parser


def parse_declare(text: Optional[str]) -> Dict[str, Any]:
    """Parse ``--declare cores=4,gpus=0,mem_gb=8``."""

    out: Dict[str, Any] = {}

    for item in (text or '').split(','):
        item = item.strip()
        if not item:
            continue
        if '=' not in item:
            raise UsageError('--declare expects KEY=VALUE, got %r' % item)

        key, _, val = item.partition('=')
        key, val    = key.strip(), val.strip()

        if key not in DECLARE_KEYS:
            raise UsageError('--declare: unknown capability %r (known: %s)'
                             % (key, ', '.join(sorted(DECLARE_KEYS))))
        try:
            out[key] = DECLARE_KEYS[key](val)
        except ValueError:
            raise UsageError('--declare %s: not a number: %r' % (key, val))

        if out[key] < 0:
            raise UsageError('--declare %s: must not be negative' % key)

    return out


def _member_fragments(name: str, rest: str) -> List[Tuple[str, List[str]]]:
    """Split ``k=v,v2,k2=v3`` into ``[(key, [values]), …]``.

    The one non-obvious rule of the grammar: a fragment **without** ``=``
    continues the previous key, so ``software=a,b`` is one key with two
    values while ``site=NERSC`` stays a single value.  A leading fragment
    without ``=`` has no previous key and is a usage error -- never a
    silently dropped token.
    """

    pairs: List[Tuple[str, List[str]]] = []

    for item in rest.split(','):
        item = item.strip()
        if not item:
            continue
        if '=' not in item:
            if not pairs:
                raise UsageError('--member %s:%s: %r is not key=value'
                                 % (name, rest, item))
            pairs[-1][1].append(item)
            continue
        key, _, val = item.partition('=')
        key, val    = key.strip(), val.strip()
        if not key:
            raise UsageError('--member %s:%s: %r has an empty key'
                             % (name, rest, item))
        if any(key == known for known, _ in pairs):
            raise UsageError('--member %s: key %r given more than once'
                             % (name, key))
        pairs.append((key, [val] if val else []))

    if not pairs:
        raise UsageError('--member %s: expects at least one key=value '
                         '(got %r)' % (name, rest))

    return pairs


def _member_scalar(name: str, key: str, values: List[str]) -> str:
    """One value for a key that takes exactly one."""

    if len(values) != 1:
        raise UsageError('--member %s: %s takes a single value, got %s'
                         % (name, key, ','.join(values) or 'none'))
    return values[0]


def _member_int(name: str, key: str, values: List[str]) -> int:
    """A non-negative integer; ``min_pilots`` may be 0, the rest may not."""

    text = _member_scalar(name, key, values)
    try:
        number = int(text)
    except ValueError:
        raise UsageError('--member %s: %s is not a number: %r'
                         % (name, key, text))
    floor = 0 if key in ('min_pilots', 'gpus') else 1
    if number < floor:
        raise UsageError('--member %s: %s must be >= %d, got %d'
                         % (name, key, floor, number))
    return number


def _attribute_value(values: List[str]) -> Any:
    """An attribute value: a number where it looks like one, else text.

    A single value stays a scalar (``site=NERSC``); several become a list
    (``labels=a,b``), which is what the dispatcher's matcher understands.
    """

    def one(text: str) -> Any:
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return text

    if len(values) == 1:
        return one(values[0])
    return [str(v) for v in values]


def parse_member(spec: str) -> Dict[str, Any]:
    """Parse one ``--member NAME:key=value,…`` into a member record.

    See ``docs/join.md``; the record is the wire shape the federation's
    ``join`` route expects in its ``members`` list.
    """

    text = (spec or '').strip()

    if ':' not in text:
        raise UsageError('--member expects NAME:key=value,…, got %r' % spec)

    name, _, rest = text.partition(':')
    name = name.strip()

    if not MEMBER_NAME_RE.match(name):
        raise UsageError('--member name must match %s (no dots -- the dot '
                         'separates resource and member), got: %r'
                         % (MEMBER_NAME_RE.pattern, name))

    member: Dict[str, Any] = {'member': name, 'software': [],
                              'attributes': {}}

    for key, values in _member_fragments(name, rest):
        _member_assign(member, name, key, values)

    return member


def _member_assign(member: Dict[str, Any], name: str, key: str,
                   values: List[str]) -> None:
    """Put one parsed ``key=value…`` onto the member record.

    Known keys land on the member's pool description; **every other key
    becomes an attribute**, which is how free-form labels (``site=NERSC``,
    ``mem_gb_per_node=256``) reach the dispatcher's matcher.
    """

    if key in MEMBER_INT_KEYS:
        member[MEMBER_INT_KEYS[key]] = _member_int(name, key, values)

    elif key in MEMBER_LIST_KEYS:
        # always a list, however many tags were typed -- the record shape
        # must not depend on that
        member[key] = [v for v in values if v]

    elif key in MEMBER_FLOAT_KEYS:
        member.setdefault('budget', {})['node_hours'] = \
            _member_float(name, key, values)

    elif key in MEMBER_BOOL_KEYS:
        raw = _member_scalar(name, key, values).lower()
        if raw not in BOOL_WORDS:
            raise UsageError('--member %s: %s must be true or false, got %r'
                             % (name, key, raw))
        member[key] = BOOL_WORDS[raw]

    elif key in MEMBER_STR_KEYS:
        member[MEMBER_STR_KEYS[key]] = _member_str(name, key, values)

    else:
        member['attributes'][key] = _attribute_value(values)


def _member_float(name: str, key: str, values: List[str]) -> float:
    """A positive float (the member's node-hour budget)."""

    raw = _member_scalar(name, key, values)

    try:
        number = float(raw)
    except ValueError:
        raise UsageError('--member %s: %s is not a number: %r'
                         % (name, key, raw))
    if number <= 0:
        raise UsageError('--member %s: %s must be > 0' % (name, key))

    return number


def _member_str(name: str, key: str, values: List[str]) -> str:
    """A single-valued text key, with the two that validate themselves."""

    value = _member_scalar(name, key, values)

    if key == 'class' and not CLASS_RE.match(value):
        raise UsageError('--member %s: class must match %s (it names the '
                         'pool fed-<class> and is never lower-cased for '
                         'you), got: %r' % (name, CLASS_RE.pattern, value))
    if key == 'scratch':
        return check_scratch(value)

    return value


def parse_members(specs: Sequence[str]) -> List[Dict[str, Any]]:
    """Parse every ``--member`` and reject duplicate short names."""

    members: List[Dict[str, Any]] = []
    seen: Dict[str, bool] = {}

    for spec in specs or []:
        member = parse_member(spec)
        if member['member'] in seen:
            raise UsageError('--member %s: declared more than once'
                             % member['member'])
        seen[member['member']] = True
        members.append(member)

    return members


def parse_software(values: Sequence[str]) -> List[str]:
    """Flatten repeated/comma separated ``--software`` into a unique list."""

    out: List[str] = []

    for value in values:
        for item in value.split(','):
            item = item.strip()
            if item and item not in out:
                out.append(item)

    return out


def check_scratch(path: str) -> str:
    """Validate ``--scratch`` against the staging plugin's location rule."""

    full  = os.path.abspath(os.path.expanduser(path))
    roots = [os.path.realpath(os.path.expanduser('~')),
             os.path.realpath('/tmp')]
    real  = os.path.realpath(full)

    for root in roots:
        if real == root or real.startswith(root + os.sep):
            return full

    raise UsageError('--scratch must lie under $HOME or /tmp (orbit staging '
                     'rule), got: %s' % full)


def validate(args: argparse.Namespace) -> argparse.Namespace:
    """Validate and normalise the parsed arguments.

    Adds the derived attributes ``declared`` (dict), ``software`` (list),
    ``members`` (list of member records) and ``scratch`` (absolute or
    None); raises :class:`UsageError`.
    """

    if not NAME_RE.match(args.name):
        raise UsageError('--name must match %s, got: %s'
                         % (NAME_RE.pattern, args.name))

    args.declared = parse_declare(args.declare)
    args.software = parse_software(args.software)
    args.members  = parse_members(args.member)

    if args.scratch:
        args.scratch = check_scratch(args.scratch)

    if args.node_hours is not None and args.node_hours <= 0:
        raise UsageError('--node-hours must be > 0')

    if args.mode == 'login':
        _validate_login(args)
    else:
        _validate_allocation(args)

    return args


def _login_flags(args: argparse.Namespace) -> List[Tuple[str, Any]]:
    """The flags which describe a login-mode pilot."""

    return [('--queue',         args.queue),
            ('--account',       args.account),
            ('--nodes',         args.nodes),
            ('--cpus',          args.cpus),
            ('--gpus-per-node', args.gpus_per_node),
            ('--walltime',      args.walltime),
            ('--max-pilots',    args.max_pilots)]


def _validate_members(args: argparse.Namespace) -> None:
    """Login mode with ``--member``: the flat flags are out, members are in.

    A resource declares one member per pilot *shape* it is willing to run;
    each of them is added to its capability class pool.  The flat flags
    describe exactly one shape, so mixing the two would leave it open
    which of them the single implicit member is built from.
    """

    given = [flag for flag, val in _login_flags(args) if val is not None]

    if given:
        raise UsageError('--member and %s are mutually exclusive -- a '
                         'member carries its own queue/size/budget'
                         % ', '.join(given))

    for member in args.members:
        name    = member['member']
        missing = [key for key in MEMBER_REQUIRED
                   if not _member_has(member, key)]
        if missing:
            raise UsageError('--member %s: missing %s (a member must '
                             'describe the pilots it runs)'
                             % (name, ', '.join(missing)))
        if member.get('queue') == 'default':
            raise UsageError("--member %s: queue must not be 'default' "
                             '(reserved by the orbit task dispatcher)' % name)
        if member.get('min_pilots', 0) > member.get('max_pilots', 1):
            raise UsageError('--member %s: min_pilots must not exceed '
                             'max_pilots' % name)


def _member_has(member: Dict[str, Any], key: str) -> bool:
    """Was the ``--member`` key *key* given?  (Its record name may differ.)"""

    if key == 'node_hours':
        return bool((member.get('budget') or {}).get('node_hours'))
    return MEMBER_INT_KEYS.get(key, MEMBER_STR_KEYS.get(key, key)) in member


def _validate_login(args: argparse.Namespace) -> None:
    """In login mode nothing can be detected -- the pilot must be described."""

    if args.members:
        return _validate_members(args)

    required = ('--queue', '--nodes', '--cpus', '--walltime')
    missing  = [flag for flag, val in _login_flags(args)
                if val is None and flag in required]

    if missing:
        raise UsageError('login mode requires %s (the batch system cannot '
                         'be asked for them)' % ', '.join(missing))

    if args.node_hours is None:
        raise UsageError('login mode requires --node-hours (the node-hour '
                         'budget this resource contributes)')

    for flag, val in [('--nodes', args.nodes), ('--cpus', args.cpus),
                      ('--walltime', args.walltime),
                      ('--max-pilots', args.max_pilots)]:
        if val is not None and val <= 0:
            raise UsageError('%s must be > 0' % flag)

    if args.queue == 'default':
        raise UsageError("--queue must not be 'default' (reserved by the "
                         "orbit task dispatcher)")


def _validate_allocation(args: argparse.Namespace) -> None:
    """In allocation mode the allocation itself defines the pilot."""

    if args.members:
        raise UsageError('--member only applies to --mode login: an '
                         'allocation is exactly one member, and the '
                         'allocation itself describes it')

    given = [flag for flag, val in _login_flags(args) if val is not None]

    if given:
        raise UsageError('%s only apply to --mode login (allocation mode '
                         'takes its size from the allocation)'
                         % ', '.join(given))


# ---------------------------------------------------------------------------
# capability detection
# ---------------------------------------------------------------------------

def capabilities_from_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Map ``sysinfo`` metrics onto the record's capability keys."""

    caps: Dict[str, Any] = {}
    cpu = metrics.get('cpu') or {}

    cores = cpu.get('cores_logical', metrics.get('cores_logical'))
    if cores:
        caps['cores'] = int(cores)

    mem = (metrics.get('memory') or {}).get('total')
    if mem:
        caps['mem_gb'] = round(float(mem) / GIB, 1)

    gpus = metrics.get('gpus')
    if isinstance(gpus, list):
        caps['gpus'] = len(gpus)

    return caps


def detect(client: Client, endpoint: str, mode: str,
           declared: Dict[str, Any]) -> Tuple[Dict[str, Any],
                                              Optional[Dict[str, Any]]]:
    """Detect capabilities and (allocation mode) the current allocation.

    Only allocation mode detects: there, the machine the endpoint runs on
    *is* the resource.  In login mode the endpoint sits on a login node
    whose cores and memory say nothing about the pilots it would submit
    — the size comes from the ``--nodes``/``--cpus`` flags instead.

    Detection is best effort: a missing or failing plugin is a warning,
    not an error -- what is missing afterwards must have been declared.
    """

    if mode != 'allocation':
        return {}, None

    detected: Dict[str, Any] = {}

    if not all(key in declared for key in DECLARE_KEYS):
        try:
            detected = capabilities_from_metrics(
                           client.sysinfo_metrics(endpoint))
        except ClientError as e:
            warn('capability detection via sysinfo failed: %s' % e)

    return detected, client.job_allocation(endpoint)


# ---------------------------------------------------------------------------
# the resource record
# ---------------------------------------------------------------------------

def member_capabilities(members: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The aggregate view of a resource's members.

    The record keeps its resource-wide ``capabilities`` next to the member
    list: cores and GPUs are the sum over the members' pilot shapes and
    the software list is their union.  Every existing consumer
    (``atomic-resources --json``, the demo UI, ``smoke.py``) keeps reading
    the aggregate while the member view is added alongside.
    """

    cores = 0
    gpus  = 0
    soft: List[str] = []

    for member in members:
        nodes  = int(member.get('nodes', 1) or 0)
        cores += nodes * int(member.get('cpus_per_node', 0) or 0)
        gpus  += nodes * int(member.get('gpus_per_node', 0) or 0)
        for item in member.get('software') or []:
            if item not in soft:
                soft.append(str(item))

    return {'cores': cores, 'gpus': gpus, 'software': soft}


def member_node_hours(members: Sequence[Dict[str, Any]]) -> float:
    """The resource's declared budget: the sum of its members' budgets."""

    return float(sum(float((m.get('budget') or {}).get('node_hours') or 0.0)
                     for m in members))


def pool_capabilities(args: argparse.Namespace) -> Dict[str, Any]:
    """What one login-mode pilot offers: `nodes` x the per-node numbers."""

    if args.mode != 'login':
        return {}

    if args.members:
        caps = member_capabilities(args.members)
        caps.pop('software', None)
        return caps

    return {'cores': (args.nodes or 0) * (args.cpus or 0),
            'gpus' : (args.nodes or 0) * (args.gpus_per_node or 0)}


def record_capabilities(args: argparse.Namespace,
                        detected: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Any]:
    """The record's resource-wide capabilities (the aggregate view).

    Precedence: ``--declare`` > detected (allocation mode) > derived from
    the pilot description (login mode, flat flags or the sum over the
    members).  The software list is the union of ``--software`` and every
    member's own list.
    """

    caps:    Dict[str, Any] = {}
    derived: Dict[str, Any] = pool_capabilities(args)

    for key in DECLARE_KEYS:
        if key in args.declared:
            caps[key] = args.declared[key]
        elif detected and key in detected:
            caps[key] = detected[key]
        elif key in derived:
            caps[key] = derived[key]

    caps['software'] = list(args.software)

    for item in member_capabilities(args.members).get('software') or []:
        if item not in caps['software']:
            caps['software'].append(item)

    return caps


def assemble_record(args: argparse.Namespace,
                    detected: Optional[Dict[str, Any]] = None,
                    alloc: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
    """Build the federation resource record.

    Capability precedence: ``--declare`` > detected (allocation mode) or
    derived from the pilot description (login mode).  ``budget`` is sent
    only when ``--node-hours`` was given; in allocation mode the
    federation otherwise derives it from the allocation itself
    (nodes x walltime), which it knows better than we do.
    """

    caps = record_capabilities(args, detected)

    record: Dict[str, Any] = {
        'name'        : args.name,
        'endpoint'    : args.endpoint or endpoint_proc.endpoint_name(args.name),
        'mode'        : args.mode,
        'site'        : args.site,
        'kind'        : args.kind,
        'capabilities': caps,
    }

    if args.node_hours is not None:
        record['budget'] = {'node_hours': float(args.node_hours)}
    elif args.members:
        # the resource-level budget is the aggregate view of the members'
        # own budgets -- the federation keeps the authoritative per-member
        # numbers, this keeps every existing reader working
        record['budget'] = {'node_hours': member_node_hours(args.members)}

    if args.scratch:
        record['scratch_base'] = args.scratch

    if args.members:
        record['members'] = [dict(m) for m in args.members]

    elif args.mode == 'login':
        record['pool'] = {
            'queue'           : args.queue,
            'account'         : args.account,
            'nodes'           : args.nodes,
            'cpus_per_node'   : args.cpus,
            'gpus_per_node'   : args.gpus_per_node or 0,
            'walltime_sec'    : args.walltime,
            'max_pilots'      : args.max_pilots or 2,
            'rhapsody_backend': args.rhapsody_backend,
        }

    return record


# ---------------------------------------------------------------------------
# the flow
# ---------------------------------------------------------------------------

def wait_connected(client: Client, endpoint: str, timeout: float,
                   proc: Optional[Any] = None,
                   interval: Optional[float] = None,
                   stop: Optional[threading.Event] = None) -> None:
    """Block until `endpoint` is a connected participant.

    A signal (`stop`) aborts the wait: Ctrl-C during a 60 s connect wait
    must not be ignored.
    """

    interval = POLL_INTERVAL if interval is None else interval
    deadline = time.time() + timeout
    last_err = ''

    while time.time() < deadline:

        _check_stop(stop)

        if proc is not None and not proc.is_alive():
            raise JoinError('the endpoint process died while connecting '
                            '(exit code %s)' % proc.returncode,
                            _log_hint(proc))
        try:
            if client.endpoint_connected(endpoint):
                return
        except ClientError as e:
            last_err = str(e)

        if stop is not None:
            stop.wait(interval)
        else:
            time.sleep(interval)

    hint = _log_hint(proc)
    if last_err:
        hint = ('last error talking to the broker: %s\n%s'
                % (last_err, hint)).strip()

    raise JoinError('endpoint %r did not connect within %.0f s'
                    % (endpoint, timeout), hint)


def _log_hint(proc: Optional[Any]) -> str:
    """Point at (and quote) the endpoint log."""

    if proc is None:
        return ''

    tail = ''
    try:
        tail = proc.log_tail()
    except Exception:                                    # pragma: no cover
        pass

    hint = 'endpoint log: %s' % getattr(proc, 'log', '?')
    if tail:
        hint += '\n--- last lines ---\n%s\n------------------' % tail

    return hint


def _check_stop(stop: Optional[threading.Event]) -> None:
    """Raise :class:`Interrupted` if a signal asked us to stop."""

    if stop is not None and stop.is_set():
        raise Interrupted()


def join_error_hint(e: ClientError, name: str,
                    log: str = '') -> str:
    """Turn a federation error into something actionable."""

    if e.status == 0:
        return ('the broker is not reachable -- check --broker, --cert and '
                'that the broker is running')

    detail = e.detail.lower()

    if e.status in (404, 503) and 'dispatcher' in detail:
        # the federation is there, but it has nothing to route tasks with
        return ('the broker hosts the federation but no task dispatcher -- '
                'start it with `--plugins task_dispatcher,federation`')

    if e.status == 503 or (e.status == 404 and 'no route' in detail):
        return ("the broker does not host the 'federation' plugin -- start "
                "it with `--plugins …,federation`")

    if e.status == 404:
        # the plugin answered: it is the *endpoint* it cannot see
        hint = ('the federation does not see the endpoint as connected -- '
                'it may have dropped out again')
        return hint + ('\nendpoint log: %s' % log if log else '')

    if e.status == 409:
        return ('a resource named %r is already in the federation -- pick '
                'another --name or run `atomic-leave %s` first' % (name, name))

    return ''


def do_leave(client: Client, name: str,
             cancel_tasks: bool = False) -> None:
    """Leave the federation; a failure is reported, never raised.

    ``cancel_tasks`` is off by default: with capability class pools a task
    this resource submitted can legitimately finish on another member, so
    only a full teardown asks for the queue to be emptied.
    """

    try:
        client.fed_leave(name, cancel_tasks=cancel_tasks)
        info('left the federation: %s' % name)
    except ClientError as e:
        if e.status == 404:
            info('resource %r was no longer in the federation' % name)
        else:
            warn('leaving the federation failed: %s' % e)


def teardown(client: Optional[Client], name: str,
             proc: Optional[EndpointProcess],
             cancel_tasks: bool = False) -> None:
    """Leave the federation, stop the endpoint, kill surviving pilots."""

    if client is not None:
        do_leave(client, name, cancel_tasks=cancel_tasks)

    if proc is not None:
        proc.stop()
        info('endpoint stopped')

    pids = endpoint_proc.kill_pilots(name)
    if pids:
        info('terminated %d surviving pilot process(es): %s'
             % (len(pids), ', '.join(str(p) for p in pids)))

    endpoint_proc.remove_pidfile(name)


def install_signal_handlers(stop: threading.Event) -> None:
    """SIGINT/SIGTERM set `stop` -- the foreground loop then leaves."""

    def handler(signum, frame):
        del frame
        info('\nreceived signal %d -- leaving the federation' % signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except ValueError:                               # pragma: no cover
            # not the main thread (tests) -- the caller drives `stop`
            pass


def foreground(client: Client, name: str, endpoint: str,
               proc: EndpointProcess, stop: threading.Event) -> int:
    """Relay endpoint liveness until a signal (or the endpoint) stops us."""

    info('joined -- Ctrl-C to leave the federation and stop the endpoint')

    rc        = 0
    connected = True
    next_check = time.time() + LIVENESS_INTERVAL

    while not stop.wait(LOOP_INTERVAL):

        if not proc.is_alive():
            error('the endpoint process exited (code %s)' % proc.returncode,
                  _log_hint(proc))
            rc = 1
            break

        if time.time() < next_check:
            continue

        next_check = time.time() + LIVENESS_INTERVAL
        try:
            now = client.endpoint_connected(endpoint)
        except ClientError as e:
            warn('cannot query the broker: %s' % e)
            continue

        if now != connected:
            connected = now
            info('endpoint %s is %s' % (endpoint,
                                        'connected' if now else
                                        'DISCONNECTED'))

    teardown(client, name, proc)

    return rc


def connect_and_join(client: Client, args: argparse.Namespace,
                     endpoint: str, proc: Any,
                     stop: Optional[threading.Event] = None
                     ) -> Tuple[Dict[str, Any], Any]:
    """Steps 2-4: wait for the endpoint, detect, join.

    Returns ``(record we sent, record the federation returned)``; raises
    :class:`JoinError`, :class:`Interrupted` or :class:`ClientError`.
    """

    wait_connected(client, endpoint, args.connect_timeout, proc, stop=stop)
    info('endpoint %s is connected' % endpoint)

    detected, alloc = detect(client, endpoint, args.mode, args.declared)
    _check_stop(stop)

    if alloc:
        info('allocation: %s node(s), %s s remaining'
             % (alloc.get('n_nodes'), alloc.get('runtime')))

    record = assemble_record(args, detected, alloc)

    if 'cores' not in record['capabilities']:
        raise JoinError('no core count for this resource',
                        'sysinfo did not report one -- declare it: '
                        '--declare cores=<N>')

    # what actually goes on the record: detected values, overridden by
    # anything --declare'd
    info('capabilities: %s%s' % (_fmt_caps(record['capabilities']),
                                 ' (declared values win)'
                                 if args.declared else ''))

    return record, client.fed_join(record)


# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    """Run the join flow for validated `args`."""

    name     = args.name
    endpoint = args.endpoint or endpoint_proc.endpoint_name(name)

    try:
        client = Client(broker=args.broker, token=args.token, cert=args.cert)
    except ValueError as e:
        error(str(e))
        return 2

    if client.broker.startswith('http://'):
        warn('the broker URL is plain http -- ORBIT brokers always serve '
             'TLS; https:// is almost certainly what you want')

    # signals are handled from here on: a Ctrl-C during the connect wait
    # or the capability detection must still tear the endpoint down
    stop = threading.Event()
    install_signal_handlers(stop)

    # ---------------------------------------------------------- 1. endpoint
    proc = EndpointProcess(name, client.broker,
                           endpoint =endpoint,
                           token    =client.token or None,
                           cert     =client.cert,
                           plugins  =args.plugins,
                           log_level=args.log_level,
                           binary   =args.endpoint_bin)
    try:
        pid = proc.start()
    except EndpointError as e:
        error(str(e))
        return 1

    # the pidfile is written immediately, and in *both* modes: from now
    # on there is a child process, and if this CLI is killed outright
    # `atomic-leave` must be able to find and stop it.
    path = endpoint_proc.write_pidfile(name, pid, endpoint, client.broker)

    info('started endpoint %s (pid %s), log: %s' % (endpoint, pid, proc.log))

    try:
        record, full = connect_and_join(client, args, endpoint, proc, stop)

    except (Interrupted, JoinError, ClientError, KeyboardInterrupt) as e:
        _report_join_failure(e, name, proc.log)
        abort(name, proc)
        return 1

    _report_joined(name, record, full)

    # ------------------------------------------------------------ 5. serve
    if args.detach:
        proc.detach()
        info('detached -- pidfile: %s' % path)
        info('teardown: atomic-leave %s' % name)
        return 0

    if stop.is_set():
        # the signal arrived between the join and here -- honour it
        teardown(client, name, proc)
        return 0

    return foreground(client, name, endpoint, proc, stop)


def _report_join_failure(e: BaseException, name: str, log: str) -> None:
    """Explain why the join did not happen (see :func:`join_error_hint`)."""

    if isinstance(e, Interrupted):
        info('interrupted -- stopping the endpoint (nothing was joined)')

    elif isinstance(e, KeyboardInterrupt):               # pragma: no cover
        error('interrupted -- stopping the endpoint (nothing was joined)')

    elif isinstance(e, ClientError):
        error('join failed: %s' % e, join_error_hint(e, name, log))

    else:                                                # JoinError
        error(str(e), getattr(e, 'hint', ''))


def abort(name: str, proc: EndpointProcess) -> None:
    """Undo a join that never completed: stop the child, drop the pidfile."""

    proc.stop()

    pids = endpoint_proc.kill_pilots(name)
    if pids:
        info('terminated %d surviving pilot process(es): %s'
             % (len(pids), ', '.join(str(p) for p in pids)))

    endpoint_proc.remove_pidfile(name)


def member_size(member: Dict[str, Any]) -> str:
    """``1x128c+4g`` -- what one of this member's pilots asks the batch for."""

    text = '%sx%sc' % (member.get('nodes', 1),
                       member.get('cpus_per_node', 0))
    gpus = int(member.get('gpus_per_node') or 0)
    if gpus:
        text += '+%dg' % gpus
    return text


def member_lines(members: Sequence[Dict[str, Any]]) -> List[str]:
    """One echo line per member -- what was parsed out of ``--member``."""

    lines: List[str] = []

    for member in members:
        cls  = member.get('class') or member.get('cls') or ''
        soft = ','.join(str(s) for s in member.get('software') or [])
        attr = ','.join('%s=%s' % (k, v)
                        for k, v in sorted((member.get('attributes')
                                            or {}).items()))
        bits = ['%s [%s]' % (member.get('member', '?'),
                             member.get('pool_name')
                             or ('class %s' % cls if cls else 'class derived')),
                'queue=%s' % member.get('queue', '?'),
                member_size(member),
                'node-hours=%s' % (member.get('budget')
                                   or {}).get('node_hours', '?')]
        if soft:
            bits.append('software=%s' % soft)
        if attr:
            bits.append(attr)
        lines.append(' '.join(bits))

    return lines


def _fmt_caps(caps: Dict[str, Any]) -> str:

    return ', '.join('%s=%s' % (k, caps[k])
                     for k in ['cores', 'gpus', 'mem_gb'] if k in caps)


def _report_joined(name: str, record: Dict[str, Any],
                   full: Any) -> None:
    """Summarise what was joined."""

    full = full if isinstance(full, dict) else {}
    caps = full.get('capabilities') or record['capabilities']

    # the budget is reported as the *federation* recorded it: in
    # allocation mode without --node-hours it derives one from the
    # allocation itself, and that is the number that counts
    budget = (full.get('budget') or record.get('budget') or {})

    info('joined federation as %r (%s, %s)'
         % (name, record['mode'], record['kind']))
    info('  capabilities : %s%s'
         % (_fmt_caps(caps),
            (', software=' + ','.join(caps.get('software') or []))
            if caps.get('software') else ''))
    info('  node-hours   : %s%s'
         % (budget.get('node_hours', 'derived by the federation'),
            '' if record.get('budget') else ' (derived from the allocation)'))

    # echo the members back: a typo in a --member spec must be visible
    members = full.get('members') or record.get('members') or []
    if members:
        info('  members      : %d' % len(members))
        for line in member_lines(members):
            info('    %s' % line)
    else:
        info('  pool         : %s' % full.get('pool_name', 'fed-%s' % name))

    if full.get('scratch_base') or record.get('scratch_base'):
        info('  scratch      : %s' % (full.get('scratch_base')
                                      or record.get('scratch_base')))


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console script entry point."""

    parser = build_parser()
    args   = parser.parse_args(argv)

    try:
        args = validate(args)
    except UsageError as e:
        error(str(e))
        return 2

    return run(args)


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.exit(main())

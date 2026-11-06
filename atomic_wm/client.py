"""HTTP client for the ATOMIC WM demo — gateway, federation and campaign routes.

Seeded by the supervisor with the contract's signatures (see
plans/00-overview.md §"The contract"); P2 implements the bodies, P4's CLI and
tests use it (mocked in tests). Keep names and signatures stable — other
pieces are written against them.

Conventions:
- Every broker-hosted route lives under ``/broker/<plugin>/<route>``.
- Federation and campaign routes always use the reserved session id
  ``default`` (never register a session for them).
- Endpoint-side plugins reached via ``/<endpoint>/<plugin>/<route>``;
  ``sysinfo`` metrics are session-scoped (register, query, unregister),
  ``queue_info/job_allocation`` is session-less.
- TLS is always on; verify against ``cert`` (``$RADICAL_ORBIT_BROKER_CERT``).
  ``token`` may be empty for ``--no-auth`` brokers.
"""

from __future__ import annotations

import os
import ssl
import time

from typing import Any, Dict, List, Optional

try:
    import requests
    import requests.adapters
except ImportError as _e:                                # pragma: no cover
    # the base install is deliberately stdlib-only (a pilot python must be
    # able to run the workload tools without reaching the network); the
    # HTTP client lives in the `cli` extra
    raise ImportError("the ATOMIC CLI needs 'requests' -- install it with "
                      "`pip install 'atomic-wm[cli]'`") from _e

DEFAULT_SID = 'default'

# the broker's own hosted plugins live under this pseudo endpoint name
BROKER_EP = 'broker'


class ClientError(RuntimeError):
    """Raised for non-2xx responses; carries ``status`` and ``detail``.

    ``status`` is 0 for transport level failures (broker unreachable, TLS
    problem, timeout) — those never reached an HTTP status code.
    """

    def __init__(self, status: int, detail: str, url: str = ''):
        super().__init__(f'HTTP {status} — {detail}' + (f' ({url})' if url else ''))
        self.status = status
        self.detail = detail
        self.url = url


# ORBIT's operator-placed fallbacks -- the same files
# `radical-orbit-endpoint.py` falls back to (CLI > env > file), so a
# machine set up for ORBIT needs no --cert/--token here either
DEFAULT_CERT_FILE  = '~/.radical/orbit/broker_cert.pem'
DEFAULT_TOKEN_FILE = '~/.radical/orbit/broker.token'


def _default_cert() -> Optional[str]:
    """``~/.radical/orbit/broker_cert.pem`` if the operator placed one."""

    path = os.path.expanduser(DEFAULT_CERT_FILE)

    return path if os.path.exists(path) else None


def _default_token() -> str:
    """Token from ``$RADICAL_ORBIT_TOKEN``, the env, or the token file.

    An empty result is normal and means "no ``Authorization`` header" --
    which is what a ``--no-auth`` broker wants.
    """

    for var in ['RADICAL_ORBIT_TOKEN', 'RADICAL_ORBIT_BROKER_TOKEN']:
        token = (os.environ.get(var) or '').strip()
        if token:
            return token

    try:
        with open(os.path.expanduser(DEFAULT_TOKEN_FILE), 'r',
                  encoding='utf-8') as fin:
            return fin.read().strip()
    except OSError:
        return ''


class PinnedCertAdapter(requests.adapters.HTTPAdapter):
    """Verify the broker against one pinned certificate, no hostname match.

    This mirrors what ``radical.orbit`` itself does for endpoints
    (``runtime.py``: ``create_default_context(cafile=cert)`` with
    ``check_hostname = False``): a broker certificate handed to us
    explicitly is the **only** trust root, and the name in it is not
    checked -- ORBIT's self-signed broker certs carry no SAN for the
    address a client actually dials (``127.0.0.1``, an FQDN, a tunnel).
    Without this every CLI call fails with ``CERTIFICATE_VERIFY_FAILED``
    while the endpoint next to it connects happily.
    """

    def __init__(self, cafile: str, **kw: Any):

        self._cafile = cafile

        super().__init__(**kw)

    def _pinned(self, kw: Dict[str, Any]) -> Dict[str, Any]:

        ctx = ssl.create_default_context(cafile=self._cafile)
        ctx.verify_mode    = ssl.CERT_REQUIRED
        ctx.check_hostname = False

        kw['ssl_context']     = ctx
        kw['assert_hostname'] = False        # urllib3 matches it itself
        return kw

    def init_poolmanager(self, *args: Any, **kw: Any) -> Any:

        return super().init_poolmanager(*args, **self._pinned(kw))

    def proxy_manager_for(self, proxy: str, **kw: Any) -> Any:
        # requests routes a proxied HTTPS call through a *separate*
        # ProxyManager, built here, not through init_poolmanager -- so
        # behind an HTTP proxy (OLCF: https_proxy=proxy.ccs.ornl.gov) the
        # pin was silently dropped and the self-signed broker cert failed
        # default verification, while the endpoint next to it (its own
        # ssl context through the same CONNECT tunnel) joined fine.
        return super().proxy_manager_for(proxy, **self._pinned(kw))


class Client:
    """Thin synchronous HTTP client (``requests``) for the demo CLIs."""

    def __init__(self,
                 broker: Optional[str] = None,
                 token: Optional[str] = None,
                 cert: Optional[str] = None,
                 timeout: float = 30.0):
        self.broker = (broker or os.environ.get('RADICAL_ORBIT_BROKER_URL')
                       or '').rstrip('/')
        self.token = token if token is not None else _default_token()
        self.cert = cert or os.environ.get(
            'RADICAL_ORBIT_BROKER_CERT') or _default_cert()
        self.timeout = timeout
        if not self.broker:
            raise ValueError('broker URL required (--broker or '
                             '$RADICAL_ORBIT_BROKER_URL)')

        self.session = requests.Session()

        if self.cert:
            self.cert = os.path.expanduser(self.cert)
            if not os.path.exists(self.cert):
                raise ValueError('broker cert not found: %s' % self.cert)
            try:
                self.session.mount('https://', PinnedCertAdapter(self.cert))
            except (ssl.SSLError, OSError) as e:
                raise ValueError('broker cert is not usable: %s (%s)'
                                 % (self.cert, e)) from e

    # ---------------------------------------------------------------- core
    def url(self, path: str) -> str:
        """Absolute URL for a gateway `path` (leading slash optional)."""

        return '%s/%s' % (self.broker, path.lstrip('/'))

    def headers(self) -> Dict[str, str]:
        """Auth headers — empty for a ``--no-auth`` broker (token empty)."""

        if not self.token:
            return {}

        return {'Authorization': 'Bearer %s' % self.token}

    def request(self, method: str, path: str,
                json: Any = None) -> Any:
        """Issue one request; return parsed JSON; raise ClientError on error."""

        url = self.url(path)

        # TLS is always on: verify against the broker cert when we have
        # one, else fall back to the system trust store.
        verify: Any = self.cert if self.cert else True

        try:
            resp = self.session.request(method, url, json=json,
                                        headers=self.headers(),
                                        timeout=self.timeout,
                                        verify=verify)
        except requests.exceptions.SSLError as e:
            # the full urllib3 chain is 300 characters of noise; what the
            # caller needs is the one thing that fixes it
            raise ClientError(0, 'TLS verification failed -- pass --cert '
                                 '<broker cert> (or set '
                                 '$RADICAL_ORBIT_BROKER_CERT)', url) from e

        except requests.RequestException as e:
            raise ClientError(0, 'cannot reach broker: %s' % e, url) from e

        if resp.status_code >= 400:
            raise ClientError(resp.status_code, _detail(resp), url)

        return _body(resp)

    # ------------------------------------------------------------- gateway
    def endpoints(self) -> List[Dict[str, Any]]:
        """``GET /endpoints`` → list of participants (name, plugins, connected…)."""

        data = self.request('GET', '/endpoints')

        return list((data or {}).get('endpoints') or [])

    def endpoint_connected(self, name: str) -> bool:

        for ep in self.endpoints():
            if ep.get('name') == name:
                return bool(ep.get('connected'))

        return False

    def endpoint_plugins(self, name: str) -> List[str]:
        """Plugin instance names hosted by a connected endpoint (or ``[]``)."""

        for ep in self.endpoints():
            if ep.get('name') == name:
                return list(ep.get('plugins') or [])

        return []

    # ---------------------------------------------------- endpoint plugins
    def sysinfo_metrics(self, endpoint: str) -> Dict[str, Any]:
        """Register a sysinfo session on ``endpoint``, fetch metrics, unregister."""

        reg = self.request('POST', '/%s/sysinfo/register_session' % endpoint,
                           json={})
        sid = (reg or {}).get('sid')
        if not sid:
            raise ClientError(0, 'sysinfo did not return a session id',
                              self.url('/%s/sysinfo/register_session'
                                       % endpoint))
        try:
            metrics = self.request('GET', '/%s/sysinfo/metrics/%s'
                                         % (endpoint, sid))
        finally:
            # best effort: the session expires on its own anyway
            try:
                self.request('POST', '/%s/sysinfo/unregister_session/%s'
                                    % (endpoint, sid))
            except ClientError:
                pass

        return metrics or {}

    def job_allocation(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """``GET /<endpoint>/queue_info/job_allocation`` or None if not in one.

        Also None when the endpoint does not host ``queue_info`` at all —
        capability detection is best effort by design.
        """

        try:
            data = self.request('GET', '/%s/queue_info/job_allocation'
                                      % endpoint)
        except ClientError:
            return None

        alloc = (data or {}).get('allocation')

        return alloc if isinstance(alloc, dict) else None

    # ---------------------------------------------------------- federation
    def fed_join(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /broker/federation/join/default`` → full resource record.

        The record may carry a ``members`` list -- one entry per pilot
        shape the resource is willing to run, each of which joins its
        capability class pool.  Without one the federation derives a
        single member from the flat ``pool`` block, which is what a join
        written before class pools sends.
        """

        return self.request('POST', self._fed('join/%s' % DEFAULT_SID),
                            json=record)

    def fed_leave(self, name: str,
                  cancel_tasks: bool = False) -> Dict[str, Any]:
        """``POST /broker/federation/leave/default/<name>``.

        With class pools a queued task of a leaving resource can still run
        on another member, so nothing is cancelled by default;
        ``cancel_tasks=True`` is the full-teardown behaviour.
        """

        return self.request('POST', self._fed('leave/%s/%s'
                                             % (DEFAULT_SID, name)),
                            json={'cancel_tasks': bool(cancel_tasks)})

    def fed_resources(self) -> List[Dict[str, Any]]:
        """``GET /broker/federation/resources/default`` → resources list.

        Each record carries one entry per pilot shape (with its own
        ``usage``) next to the resource-wide aggregate;
        :func:`pilots_of` renders the one from the other for a broker
        that predates class pools.
        """

        data = self.request('GET', self._fed('resources/%s' % DEFAULT_SID))

        return list((data or {}).get('resources') or [])

    def fed_resource(self, name: str) -> Dict[str, Any]:

        return self.request('GET', self._fed('resource/%s/%s'
                                            % (DEFAULT_SID, name)))

    def fed_pick(self, requirements: Dict[str, Any]) -> Dict[str, Any]:

        return self.request('POST', self._fed('pick/%s' % DEFAULT_SID),
                            json={'requirements': requirements})

    def fed_submit(self, task: Dict[str, Any],
                   requirements: Dict[str, Any]) -> Dict[str, Any]:

        return self.request('POST', self._fed('submit/%s' % DEFAULT_SID),
                            json={'task': task, 'requirements': requirements})

    def fed_task(self, task_id: str) -> Dict[str, Any]:

        return self.request('GET', self._fed('task/%s/%s'
                                            % (DEFAULT_SID, task_id)))

    # ------------------------------------------------------------ campaign
    def campaign_submit(self, workflow: Dict[str, Any],
                        sweep: Dict[str, List[Any]]) -> Dict[str, Any]:
        """``POST /broker/atomic_campaign/campaigns/default``."""

        return self.request('POST', self._cmp('campaigns/%s' % DEFAULT_SID),
                            json={'workflow': workflow, 'sweep': sweep})

    def campaigns(self) -> List[Dict[str, Any]]:

        data = self.request('GET', self._cmp('campaigns/%s' % DEFAULT_SID))

        if isinstance(data, list):
            return data

        return list((data or {}).get('campaigns') or [])

    def campaign(self, cid: str) -> Dict[str, Any]:

        return self.request('GET', self._cmp('campaign/%s/%s'
                                            % (DEFAULT_SID, cid)))

    def campaign_results(self, cid: str) -> Dict[str, Any]:

        return self.request('GET', self._cmp('results/%s/%s'
                                            % (DEFAULT_SID, cid)))

    def campaign_cancel(self, cid: str) -> Dict[str, Any]:

        return self.request('POST', self._cmp('cancel/%s/%s'
                                             % (DEFAULT_SID, cid)))

    # --------------------------------------------------------------- paths
    @staticmethod
    def _fed(route: str) -> str:
        return '/%s/federation/%s' % (BROKER_EP, route)

    @staticmethod
    def _cmp(route: str) -> str:
        return '/%s/atomic_campaign/%s' % (BROKER_EP, route)


# ---------------------------------------------------------------------------
# response helpers
# ---------------------------------------------------------------------------

# the member name a single-member resource is given (allocation mode, and a
# login-mode join written before class pools)
DEFAULT_MEMBER = 'default'


def member_id(resource: str, member: str) -> str:
    """``'<resource>.<member>'`` -- the id the dispatcher knows a member by."""

    return '%s.%s' % (resource, member)


def _int(value: Any, default: int) -> int:
    """An int from whatever a record carried; *default* for anything else."""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> Optional[float]:
    """A float from whatever a record carried; ``None`` for anything else."""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pilot_kind(record: Dict[str, Any], member: Dict[str, Any]) -> str:
    """``'endpoint'`` or ``'submit'`` -- how this pilot comes into being.

    A federation that says so per row is believed; an older one only says
    it per resource, where ``allocation`` mode means the endpoint runs
    inside the allocation and *is* the pilot.
    """

    kind = str(member.get('pilot') or '')

    if kind in ('endpoint', 'submit'):
        return kind

    return 'endpoint' if str(record.get('mode') or '') == 'allocation' \
                      else 'submit'


def _pilot_name(record: Dict[str, Any], member: Dict[str, Any],
                kind: str) -> str:
    """What a pilot row is called.

    An allocation is one pilot and carries the endpoint's name; a
    login-mode resource submits one pilot shape per class, and each shape
    is named ``<endpoint>/<shape>`` so two shapes of the same endpoint
    stay apart.
    """

    ep   = str(member.get('endpoint') or record.get('endpoint') or '')
    name = str(member.get('member') or '')

    if kind == 'endpoint':
        return ep or name or str(record.get('name') or '')

    if ep and name:
        return '%s/%s' % (ep, name)

    return ep or name or str(record.get('name') or '')


def _pilot_failing(usage: Dict[str, Any]) -> bool:
    """Whether a row that holds no pilot is failing rather than idle.

    The federation itself calls a row ``failing`` after three failures in
    a row, or while it is still backing off.  This mirrors that, and
    diverges in one spot on purpose: a reported ``pilot_error`` counts on
    its own, because a broker that surfaces the reason without a counter
    is exactly the case this line was written for.
    """

    if usage.get('pilot_error'):
        return True

    if _int(usage.get('pilot_failures'), 0) >= 3:
        return True

    until = usage.get('paused_until')

    return isinstance(until, (int, float)) and not isinstance(until, bool) \
        and until > time.time()


def _pilot_state(record: Dict[str, Any], member: Dict[str, Any]) -> str:
    """The state word of one pilot row.

    A federation that derives one per row (Orbit 122) is believed --
    including its ``idle``.  An older one reports only liveness, so the
    word is read here: an endpoint that is not answering is that, whatever
    it holds; a shape on a healthy endpoint that holds no pilot is
    ``failing`` when what it started died, and ``idle`` otherwise -- it is
    declared, it is simply not running anything.
    """

    word = member.get('state')

    if word:
        return str(word)

    # liveness first: `idle` would be a comfortable lie about a lost site
    live = str(member.get('liveness') or record.get('liveness') or '')

    if live and live != 'ok':
        return live

    usage  = member.get('usage') or {}
    active = usage.get('pilots_active')

    if active == 0 and not isinstance(active, bool):
        return 'failing' if _pilot_failing(usage) else 'idle'

    return live or str(record.get('state') or '')


def pilots_of(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The pilot rows of one resource record, deriving one where needed.

    A resource declares one shape of pilot per capability class it serves,
    and the federation reports one row per shape (the wire calls a row a
    ``member``).  Three payloads have to render the same table:

    * a federation that names the endpoint, the pilot kind and the time
      left per row -- everything is taken as it comes;
    * one that reports rows without those fields -- the kind comes from
      the record's mode, ``left`` is unknown, and a row without a live
      pilot is ``idle``;
    * one that predates class pools and reports no rows at all -- exactly
      one row is derived from the resource-wide fields, flagged
      ``derived: True``.

    Every row carries the keys the table renders (``pilot_name``,
    ``pilot``, ``mode``, ``remaining_sec``, ``mem_gb_per_node``,
    ``state``) next to whatever the federation sent.
    """

    record = record or {}
    rows   = record.get('members')

    if isinstance(rows, list) and rows:
        rows = [dict(m) for m in rows if isinstance(m, dict)]
    else:
        rows = _derived_pilots(record)

    out = []
    for member in rows:
        kind  = _pilot_kind(record, member)
        attrs = member.get('attributes') or {}
        # the memory lives under `attributes` on every shape so far; a
        # row that carries it at the top level is not overruled by a
        # `None` sitting in the attributes
        mem   = attrs.get('mem_gb_per_node')
        if mem is None:
            mem = member.get('mem_gb_per_node')

        member.update({
            'endpoint'       : member.get('endpoint')
                               or record.get('endpoint') or '',
            'pilot'          : kind,
            'pilot_name'     : _pilot_name(record, member, kind),
            'mode'           : 'alloc' if kind == 'endpoint' else 'login',
            'remaining_sec'  : _float(member.get('remaining_sec')),
            'mem_gb_per_node': mem,
            'state'          : _pilot_state(record, member),
        })
        out.append(member)

    return out


# the name `pilots_of` took over from; kept as an alias for one release,
# so an older caller (and the odd script) keeps working
members_of = pilots_of


def _derived_pilots(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The single pilot row of a record that reports none of its own."""

    caps = record.get('capabilities') or {}
    pool = record.get('pool') or {}
    name = str(record.get('name') or '')

    nodes = _int(pool.get('nodes'), 1)
    cpus  = _int(pool.get('cpus_per_node') or caps.get('cores'), 0)
    # a login-mode pool declares its GPUs per node; an allocation is one
    # pilot, so the resource's own GPU count is that pilot's
    gpus  = _int(pool.get('gpus_per_node') if 'gpus_per_node' in pool
                 else caps.get('gpus'), 0)

    return [{
        'member'          : DEFAULT_MEMBER,
        'member_id'       : member_id(name, DEFAULT_MEMBER) if name else '',
        'class'           : 'gpu' if gpus else 'cpu',
        'pool_name'       : record.get('pool_name') or '',
        # a derived member has no queue of its own: an allocation-mode
        # resource never declared one, and inventing the join mode here
        # would put a word that is not a queue in a queue column
        'queue'           : pool.get('queue') or '',
        'account'         : pool.get('account'),
        'nodes'           : nodes,
        'cpus_per_node'   : cpus,
        'gpus_per_node'   : gpus,
        'walltime_sec'    : pool.get('walltime_sec'),
        'min_pilots'      : pool.get('min_pilots', 0),
        'max_pilots'      : pool.get('max_pilots', 1),
        'rhapsody_backend': pool.get('rhapsody_backend'),
        'scratch_base'    : record.get('scratch_base'),
        # a resource that declared `shared_fs=false` at join time says so
        # at the top level of its record -- the implicit member inherits it
        'shared_fs'       : bool(record.get('shared_fs', True)),
        'software'        : list(caps.get('software') or []),
        'attributes'      : {'site'           : record.get('site') or '',
                             'kind'           : record.get('kind') or '',
                             'mem_gb_per_node': caps.get('mem_gb')},
        'budget'          : dict(record.get('budget') or {}),
        'usage'           : dict(record.get('usage') or {}),
        'liveness'        : record.get('liveness') or '',
        # the derived state word ('failing' when the resource's pilots die
        # at submit); an older federation sends none, hence the fallback
        'state'           : record.get('state')
                            or record.get('liveness') or '',
        'derived'         : True,
    }]


def _detail(resp: Any) -> str:
    """Best available error message from a failed response.

    FastAPI reports ``{"detail": …}``; the gateway proxy may pass a plugin
    error through verbatim, so fall back to the raw body.
    """

    try:
        body = resp.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        detail = body.get('detail', body.get('error'))
        if detail:
            return detail if isinstance(detail, str) else repr(detail)

    text = (resp.text or '').strip()

    return text[:400] if text else resp.reason or 'request failed'


def _body(resp: Any) -> Any:
    """Parsed JSON body; ``None`` for an empty body, raw text if not JSON."""

    if not resp.content:
        return None

    try:
        return resp.json()
    except ValueError:
        return resp.text


def add_connection_args(parser) -> None:
    """Add ``--broker/--token/--cert`` to an argparse parser (shared by CLIs)."""
    parser.add_argument('--broker', default=None,
                        help='gateway URL (default $RADICAL_ORBIT_BROKER_URL)')
    parser.add_argument('--token', default=None,
                        help='bearer token (default $RADICAL_ORBIT_TOKEN, '
                             'then $RADICAL_ORBIT_BROKER_TOKEN, then '
                             '~/.radical/orbit/broker.token; may be empty '
                             'for a --no-auth broker)')
    parser.add_argument('--cert', default=None,
                        help='broker TLS cert (default '
                             '$RADICAL_ORBIT_BROKER_CERT, then '
                             '~/.radical/orbit/broker_cert.pem)')


def client_from_args(args) -> Client:
    return Client(broker=args.broker, token=args.token, cert=args.cert)

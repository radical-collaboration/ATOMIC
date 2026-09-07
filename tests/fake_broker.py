"""A fake orbit gateway for the CLI tests.

The CLIs talk to the broker with ``requests``; these tests replace
``requests.Session.request`` with :class:`FakeBroker`, which implements
just enough of the gateway + federation contract (see
``plans/00-overview.md``) to drive the join / leave / resources flows,
and records every call so a test can assert on the exact request that
went out.

Kept out of the test modules themselves because both ``test_client.py``
and ``test_cli_join.py`` use it.
"""

import json as _json
import threading

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit


# ---------------------------------------------------------------------------
def error_body(status: int, detail: str) -> Dict[str, Any]:
    """The gateway's error envelope, as the real broker sends it."""

    return {'error': True, 'status_code': status, 'detail': detail}


class FakeResponse:
    """The bits of ``requests.Response`` the client actually uses."""

    def __init__(self, status: int = 200, body: Any = None,
                 text: Optional[str] = None):

        self.status_code = status
        self.reason      = 'fake'
        self._body       = body

        if text is not None:
            self.text = text
        elif body is None:
            self.text = ''
        else:
            self.text = _json.dumps(body)

        self.content = self.text.encode('utf-8')

    def json(self) -> Any:

        if not self.content:
            raise ValueError('no content')

        return _json.loads(self.text)


# ---------------------------------------------------------------------------
class FakeBroker:
    """An in-memory gateway; install it over ``requests.Session.request``."""

    def __init__(self):

        # topology: endpoint name -> {'connected': bool, 'plugins': [...]}
        self.endpoints: Dict[str, Dict[str, Any]] = {}

        # what `sysinfo` and `queue_info` report
        self.metrics: Dict[str, Any] = {
            'cpu'   : {'cores_logical': 16, 'cores_physical': 8},
            'memory': {'total': 32 * 1024 ** 3},
            'gpus'  : [{'vendor': 'NVIDIA'}, {'vendor': 'NVIDIA'}],
        }
        self.allocation: Optional[Dict[str, Any]] = None
        self.has_queue_info = True

        # what the federation fills in when a join carries no budget
        self.derived_budget = 2.0

        # federation state
        self.resources: List[Dict[str, Any]] = []
        self.joined:    List[Dict[str, Any]] = []
        self.left:      List[str]            = []

        # observability / steering
        self.calls: List[Dict[str, Any]] = []
        self.sysinfo_sessions: List[str] = []
        self.errors: Dict[str, Tuple[int, str]] = {}   # route -> (status, msg)
        self.joined_event = threading.Event()

    # ----------------------------------------------------------- transport
    def install(self, monkeypatch) -> 'FakeBroker':
        """Route every ``requests`` call in this process to this fake."""

        import requests

        broker = self

        def _request(session, method, url, **kw):
            return broker.handle(method, url, **kw)

        monkeypatch.setattr(requests.Session, 'request', _request)

        return self

    def handle(self, method: str, url: str, **kw) -> FakeResponse:
        """Dispatch one request."""

        path = urlsplit(url).path
        self.calls.append({'method' : method,
                           'url'    : url,
                           'path'   : path,
                           'json'   : kw.get('json'),
                           'headers': dict(kw.get('headers') or {}),
                           'verify' : kw.get('verify'),
                           'timeout': kw.get('timeout')})

        for route, (status, msg) in self.errors.items():
            if route in path:
                return FakeResponse(status, error_body(status, msg))

        parts = [p for p in path.split('/') if p]

        return self._route(method, parts)

    # -------------------------------------------------------------- routes
    def _route(self, method: str, parts: List[str]) -> FakeResponse:

        if parts == ['endpoints']:
            return FakeResponse(200, {'endpoints': self._endpoint_list(),
                                      'total': len(self.endpoints)})

        if len(parts) >= 2 and parts[0] == 'broker':
            return self._broker_route(method, parts[1], parts[2:])

        if len(parts) >= 2:
            return self._endpoint_route(method, parts[0], parts[1],
                                        parts[2:])

        return FakeResponse(404, error_body(404, 'no such route: %s' % parts))

    def _endpoint_list(self) -> List[Dict[str, Any]]:

        out = []
        for name, info in self.endpoints.items():
            plugins = info.get('plugins') or ['sysinfo', 'queue_info']
            out.append({'name'        : name,
                        'plugins'     : plugins,
                        'connected'   : bool(info.get('connected')),
                        'plugin_count': len(plugins)})
        return out

    def _endpoint_route(self, method: str, endpoint: str, plugin: str,
                        rest: List[str]) -> FakeResponse:

        if endpoint not in self.endpoints:
            return FakeResponse(404, error_body(404, 'endpoint %r not connected'
                                                % endpoint))

        if plugin == 'sysinfo':
            if rest[:1] == ['register_session']:
                sid = 'sys-%d' % (len(self.sysinfo_sessions) + 1)
                self.sysinfo_sessions.append(sid)
                return FakeResponse(200, {'sid': sid})
            if rest[:1] == ['metrics']:
                if rest[1:] and rest[1] not in self.sysinfo_sessions:
                    return FakeResponse(404, error_body(404, 'unknown session'))
                return FakeResponse(200, self.metrics)
            if rest[:1] == ['unregister_session']:
                if rest[1:] and rest[1] in self.sysinfo_sessions:
                    self.sysinfo_sessions.remove(rest[1])
                return FakeResponse(200, {'ok': True})

        if plugin == 'queue_info' and rest == ['job_allocation']:
            if not self.has_queue_info:
                return FakeResponse(404, error_body(404, 'plugin not loaded'))
            return FakeResponse(200, {'allocation': self.allocation})

        return FakeResponse(404, error_body(404, 'no such plugin route'))

    def _broker_route(self, method: str, plugin: str,
                      rest: List[str]) -> FakeResponse:

        if plugin != 'federation':
            return FakeResponse(503, error_body(503, 'plugin %r not hosted'
                                                % plugin))

        if rest[:2] == ['join', 'default']:
            return self._join()

        if rest[:2] == ['leave', 'default'] and len(rest) == 3:
            return self._leave(rest[2])

        if rest == ['resources', 'default']:
            return FakeResponse(200, {'resources': self.resources})

        if rest[:2] == ['resource', 'default'] and len(rest) == 3:
            for rec in self.resources:
                if rec.get('name') == rest[2]:
                    return FakeResponse(200, rec)
            return FakeResponse(404, error_body(404, 'unknown resource'))

        return FakeResponse(404, error_body(404, 'no such federation route'))

    def _join(self) -> FakeResponse:

        record = dict(self.calls[-1]['json'] or {})
        name   = record.get('name')

        if any(r.get('name') == name for r in self.resources):
            return FakeResponse(409, error_body(409, 'name %r exists' % name))

        # a record without a budget is one the federation derives itself
        # (allocation mode: nodes x walltime) -- it comes back filled in
        budget = record.get('budget') or {'node_hours': self.derived_budget}

        full = dict(record)

        # the federation fills a member's id, class and pool server-side
        members = []
        for member in record.get('members') or []:
            cls = member.get('class') or (
                  'gpu' if member.get('gpus_per_node') else 'cpu')
            members.append(dict(member,
                                member_id='%s.%s' % (name, member['member']),
                                **{'class': cls},
                                pool_name='fed-%s' % cls))
        if members:
            full['members'] = members

        full.update({'joined_at'     : 1757100000.0,
                     'dispatcher_sid': 'fed' if members else 'fed-%s' % name,
                     'pool_name'     : '' if members else 'fed-%s' % name,
                     'liveness'      : 'ok',
                     'budget'        : budget,
                     'usage'         : {'node_hours_used'     : 0.0,
                                        'node_hours_remaining':
                                            budget['node_hours'],
                                        'pilots_active': 0,
                                        'tasks_running': 0,
                                        'tasks_done'   : 0}})

        self.joined.append(record)
        self.resources.append(full)
        self.joined_event.set()

        return FakeResponse(200, full)

    def _leave(self, name: str) -> FakeResponse:

        self.left.append(name)

        for rec in list(self.resources):
            if rec.get('name') == name:
                self.resources.remove(rec)
                return FakeResponse(200, {
                    'resource'       : name,
                    'ok'             : True,
                    'members_removed': len(rec.get('members') or []) or 1,
                    'tasks_requeued' : 0,
                    'tasks_failed'   : 0})

        return FakeResponse(404, error_body(404, 'unknown resource %r' % name))

    # ------------------------------------------------------------- helpers
    def connect(self, endpoint: str, plugins: Optional[List[str]] = None
                ) -> None:
        """Make `endpoint` show up as a connected participant."""

        self.endpoints[endpoint] = {
            'connected': True,
            'plugins'  : plugins if plugins is not None
                         else ['sysinfo', 'queue_info', 'rhapsody']}

    def paths(self, method: Optional[str] = None) -> List[str]:
        """The request paths seen so far (optionally filtered by method)."""

        return [c['path'] for c in self.calls
                if method is None or c['method'] == method]

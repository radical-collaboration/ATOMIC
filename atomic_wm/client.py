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
from typing import Any, Dict, List, Optional

DEFAULT_SID = 'default'


class ClientError(RuntimeError):
    """Raised for non-2xx responses; carries ``status`` and ``detail``."""

    def __init__(self, status: int, detail: str, url: str = ''):
        super().__init__(f'HTTP {status} — {detail}' + (f' ({url})' if url else ''))
        self.status = status
        self.detail = detail
        self.url = url


class Client:
    """Thin synchronous HTTP client (``requests``) for the demo CLIs."""

    def __init__(self,
                 broker: Optional[str] = None,
                 token: Optional[str] = None,
                 cert: Optional[str] = None,
                 timeout: float = 30.0):
        self.broker = (broker or os.environ.get('RADICAL_ORBIT_BROKER_URL')
                       or '').rstrip('/')
        self.token = token if token is not None else os.environ.get(
            'RADICAL_ORBIT_TOKEN', '')
        self.cert = cert or os.environ.get('RADICAL_ORBIT_BROKER_CERT')
        self.timeout = timeout
        if not self.broker:
            raise ValueError('broker URL required (--broker or '
                             '$RADICAL_ORBIT_BROKER_URL)')

    # ---------------------------------------------------------------- core
    def request(self, method: str, path: str,
                json: Any = None) -> Any:
        """Issue one request; return parsed JSON; raise ClientError on error."""
        raise NotImplementedError('P2 implements this')

    # ------------------------------------------------------------- gateway
    def endpoints(self) -> List[Dict[str, Any]]:
        """``GET /endpoints`` → list of participants (name, plugins, connected…)."""
        raise NotImplementedError('P2 implements this')

    def endpoint_connected(self, name: str) -> bool:
        raise NotImplementedError('P2 implements this')

    # ---------------------------------------------------- endpoint plugins
    def sysinfo_metrics(self, endpoint: str) -> Dict[str, Any]:
        """Register a sysinfo session on ``endpoint``, fetch metrics, unregister."""
        raise NotImplementedError('P2 implements this')

    def job_allocation(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """``GET /<endpoint>/queue_info/job_allocation`` or None if not in one."""
        raise NotImplementedError('P2 implements this')

    # ---------------------------------------------------------- federation
    def fed_join(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /broker/federation/join/default`` → full resource record."""
        raise NotImplementedError('P2 implements this')

    def fed_leave(self, name: str) -> Dict[str, Any]:
        raise NotImplementedError('P2 implements this')

    def fed_resources(self) -> List[Dict[str, Any]]:
        """``GET /broker/federation/resources/default`` → resources list."""
        raise NotImplementedError('P2 implements this')

    def fed_resource(self, name: str) -> Dict[str, Any]:
        raise NotImplementedError('P2 implements this')

    def fed_pick(self, requirements: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError('P2 implements this')

    def fed_submit(self, task: Dict[str, Any],
                   requirements: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError('P2 implements this')

    def fed_task(self, task_id: str) -> Dict[str, Any]:
        raise NotImplementedError('P2 implements this')

    # ------------------------------------------------------------ campaign
    def campaign_submit(self, workflow: Dict[str, Any],
                        sweep: Dict[str, List[Any]]) -> Dict[str, Any]:
        """``POST /broker/atomic_campaign/campaigns/default``."""
        raise NotImplementedError('P2 implements this')

    def campaigns(self) -> List[Dict[str, Any]]:
        raise NotImplementedError('P2 implements this')

    def campaign(self, cid: str) -> Dict[str, Any]:
        raise NotImplementedError('P2 implements this')

    def campaign_results(self, cid: str) -> Dict[str, Any]:
        raise NotImplementedError('P2 implements this')

    def campaign_cancel(self, cid: str) -> Dict[str, Any]:
        raise NotImplementedError('P2 implements this')


def add_connection_args(parser) -> None:
    """Add ``--broker/--token/--cert`` to an argparse parser (shared by CLIs)."""
    parser.add_argument('--broker', default=None,
                        help='gateway URL (default $RADICAL_ORBIT_BROKER_URL)')
    parser.add_argument('--token', default=None,
                        help='bearer token (default $RADICAL_ORBIT_TOKEN)')
    parser.add_argument('--cert', default=None,
                        help='broker TLS cert (default $RADICAL_ORBIT_BROKER_CERT)')


def client_from_args(args) -> Client:
    return Client(broker=args.broker, token=args.token, cert=args.cert)

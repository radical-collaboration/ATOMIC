"""``atomic_campaign`` -- the campaign plugin (broker or service endpoint).

One POST creates a campaign: a workflow spec plus a parameter sweep is
expanded into N workflow instances, each of which runs its stages
sequentially through the ``federation`` plugin, with its outputs collected
into the central store on the broker host.  The plugin owns the driver task,
the campaign state, and the REST surface the CLI and the demo UI use.

Everything ATOMIC-specific about *how* a campaign is planned and executed
lives in :mod:`atomic_wm.campaign`; this module is the orbit shell around it:
plugin lifetime, routes, sessions, persistence, and the calls to the
``federation`` / ``task_dispatcher`` plugins on the broker.

Two placements, chosen by the host role (plans/11 §3.1):

- **on the broker** (``app.state.is_broker``): the federation is reached
  in-process (:class:`_FederationAPI`; ``plans/00-overview.md`` §Facts --
  broker plugins call each other through
  ``app.state.endpoint_service.handle_request``, never through the broker
  caller, which cannot address the broker itself);
- **on a service endpoint** (``ep_cm``, started with ``--plugins
  atomic_campaign``): over the endpoint runtime's WebSocket to
  ``dst='broker'`` (:class:`_RemoteFederationAPI`).  A broker restart then
  does not touch the campaign state: the runner waits the outage out
  (``unreachable_budget_sec``).
"""

from __future__ import annotations

import asyncio
import math
import base64
import json
import logging
import os
import tempfile
import time

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# radical.orbit FIRST, deliberately: in an environment without orbit (a
# login node, a pilot's python) the entry-point guard in
# ``atomic_wm/plugins/__init__.py`` only swallows a missing ``radical*``.
# fastapi ships with orbit, so it is present wherever orbit is -- but the
# import that fails first must be the one the guard recognises.
from radical.orbit.plugin_base         import DEFAULT_SID, Plugin
from radical.orbit.plugin_session_base import PluginSession

from fastapi import FastAPI, HTTPException, Request

from atomic_wm.campaign.planner import PlannerError, SweepPlanner
from atomic_wm.campaign.runner  import (CampaignRunner, FederationAPI,
                                        FederationCallError,
                                        FederationUnavailable,
                                        DEFAULT_UNREACHABLE_BUDGET,
                                        call_error, describe,
                                        drain_pending_cancels,
                                        is_transient, is_transient_status,
                                        REASON_INCOMPLETE, REASON_NOT_STARTED,
                                        REASON_NO_RESOURCE,
                                        REASON_NO_RESOURCES_JOINED,
                                        REASON_NO_STATUS, REASON_STOPPED)
from atomic_wm.campaign.state   import (Campaign, CANCELED, FAILED,
                                        STATE_FILE,
                                        TERMINAL_STATES, default_state_root,
                                        default_store_root, load_campaigns,
                                        mark_interrupted, new_campaign_id,
                                        prune_campaigns, save_campaigns)
from atomic_wm.campaign.store   import ResultStore

log = logging.getLogger('radical.orbit')

FEDERATION_PLUGIN = 'federation'
DISPATCHER_PLUGIN = 'task_dispatcher'

# the participant name the remote API addresses the federation on
BROKER_DST = 'broker'

# per-call bound of a remote federation call.  ``EndpointRuntime.call``
# waits 600 s by default, and a pending call survives a reconnect by
# design -- one poll could hang for ten minutes without this.
REMOTE_CALL_TIMEOUT = 15.0
# a staging get moves a whole output file: a longer bound
REMOTE_STAGING_TIMEOUT = 120.0

# how often the plugin retries cancels the federation has not confirmed
DRAIN_INTERVAL = 10.0

# how long a stop waits for the shared AuCaM backend to shut down (the whole
# stop must stay within ORBIT's host-stop bounds: 5 s broker, 15 s endpoint)
BACKEND_STOP_TIMEOUT = 2.0

# $ATOMIC_CAMPAIGN_UNREACHABLE_SEC overrides the unreachable budget
_BUDGET_ENV = 'ATOMIC_CAMPAIGN_UNREACHABLE_SEC'

_JSON_HEADERS = {'content-type': 'application/json'}

# how many per-member texts a 409's on-screen reason still spells out (past
# that only the count fits on a card; the full list stays in `detail`)
MAX_REASON_MEMBERS = 3


# --------------------------------------------------------------------------
def _ui_module_path() -> Optional[str]:
    """Absolute path of the packaged Explorer module, or ``None``.

    The file (``atomic_wm/ui/atomic_campaign.js``, build piece 05) is served
    at ``/plugins/atomic_campaign.js``.  ``BrokerPluginHost.get_ui_modules()``
    reads it off the *class*, so this is resolved once at import time -- the
    gateway caches the JS anyway, so a changed module needs a broker restart.
    """

    try:
        from atomic_wm.ui import ui_module_path
        path = Path(ui_module_path())
        if path.is_file():
            return str(path)
        log.warning('[atomic_campaign] UI module missing at %s -- the '
                    'Explorer will show the generic plugin page', path)
    except Exception as exc:                                  # noqa: BLE001
        log.warning('[atomic_campaign] no UI module (%s) -- the Explorer '
                    'will show the generic plugin page', exc)
    return None


# --------------------------------------------------------------------------
class AtomicCampaignSession(PluginSession):
    """Sessions carry no state here -- campaigns are plugin-wide.

    The class exists because ``Plugin._ensure_default_session`` needs a
    ``session_class`` to instantiate the reserved ``default`` session, which
    is the only session these routes ever use.
    """


# --------------------------------------------------------------------------
class _FederationAPI(FederationAPI):
    """The runner's view of the broker: federation, dispatcher, pilots.

    - ``federation`` and ``task_dispatcher`` are broker-hosted siblings and
      are reached **in-process** through the plugin host's
      ``handle_request(method, path, headers, body_bytes)`` (endpoint-relative
      path, no ``/broker`` prefix; returns a starlette response, raises
      ``HTTPException``).  They are looked up lazily on every call: plugins
      load in filter order, and a missing one must answer, not crash.
    - a pilot's ``staging`` plugin lives on a *remote* participant, so it is
      reached over the broker caller, using orbit's own ``StagingClient`` on
      the dispatcher's caller-backed sync transport (blocking calls, hence
      ``asyncio.to_thread``).
    """

    def __init__(self, app: FastAPI, *, call_timeout: float = 120.0) -> None:
        self._app     = app
        self._timeout = call_timeout

    # -- in-process plugin calls ------------------------------------------
    def _host(self) -> Any:
        return getattr(self._app.state, 'endpoint_service', None)

    def has_plugin(self, name: str) -> bool:
        host = self._host()
        return bool(host is not None and name in getattr(host, 'plugins', {}))

    async def _call(self, plugin: str, method: str, path: str,
                    body: Optional[Dict[str, Any]] = None
                    ) -> Tuple[int, Any]:
        """One in-process call; returns ``(status, parsed body)``."""

        host = self._host()
        if host is None or not self.has_plugin(plugin):
            # the message is shown on screen -- keep it in the demo's
            # vocabulary; the plugin name goes to the log, not the UI
            log.warning('[atomic_campaign] %s plugin is not hosted on this '
                        'broker', plugin)
            raise FederationUnavailable(FederationUnavailable.DEFAULT)

        payload = json.dumps(body).encode() if body is not None else b''
        try:
            resp = await host.handle_request(method, path, dict(_JSON_HEADERS),
                                             payload)
        except HTTPException as exc:
            return exc.status_code, {'detail': exc.detail}

        raw = bytes(getattr(resp, 'body', b'') or b'')
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {'detail': raw.decode('utf-8', 'replace')}
        return int(getattr(resp, 'status_code', 200)), data

    @staticmethod
    def _detail(data: Any) -> str:
        if isinstance(data, dict):
            detail = data.get('detail') or data.get('error') or ''
            if isinstance(detail, dict):
                detail = detail.get('message') or json.dumps(detail)
            return str(detail)
        return str(data)

    # -- FederationAPI ----------------------------------------------------
    async def submit(self, task: Dict[str, Any],
                     requirements: Dict[str, Any]) -> Dict[str, Any]:

        status, data = await self._call(
            FEDERATION_PLUGIN, 'POST',
            '/%s/submit/%s' % (FEDERATION_PLUGIN, DEFAULT_SID),
            {'task': task, 'requirements': requirements})
        if status >= 400:
            detail = self._detail(data)
            if is_transient_status(status, detail):
                raise call_error(status, detail, REASON_NOT_STARTED)
            reason = self._submit_reason(status, detail, data)
            if status == 409:
                detail = self._submit_detail(detail, data)
            raise FederationCallError(reason, detail or 'HTTP %d' % status,
                                      status=status)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _member_reasons(data: Any) -> Dict[str, Any]:
        """The 409 body's ``reasons`` map: member id -> why it lost.

        Empty when the federation has no members at all -- which is the
        one case a 409 does not blame the requirements for.
        """

        reasons = data.get('reasons') if isinstance(data, dict) else None
        return reasons if isinstance(reasons, dict) else {}

    @classmethod
    def _member_parts(cls, data: Any) -> List[str]:
        """``['a.cpu: gpus 0 < 1', 'b.gpu: node_hours exhausted']``.

        Sorted by member id so the same refusal always reads the same way;
        a value that is not a string is JSON-dumped rather than dropped.
        """

        parts = []
        reasons = cls._member_reasons(data)
        for member_id in sorted(reasons):
            text = reasons[member_id]
            if not isinstance(text, str):
                text = json.dumps(text, default=str)
            parts.append('%s: %s' % (member_id, text))
        return parts

    @classmethod
    def _submit_reason(cls, status: int, detail: str,
                       data: Any = None) -> str:
        """Which on-screen phrase a failed submit deserves.

        409 is the federation's own "no class has an eligible member", and
        its ``reasons`` map tells the two stories apart: an empty one means
        nothing has joined yet (nobody to satisfy anything), a filled one
        means the resources that did join do not match.  Saying the second
        when the first is true sends whoever watches the demo hunting a
        requirement instead of joining a resource.

        In the second case the phrase alone is not enough on screen -- "no
        resource satisfies the stage requirements" leaves the audience
        guessing *what* was missing -- so the per-member texts ride along
        in brackets while the federation is small enough for that to fit
        (they are the federation's own words about resources: ``gpus
        0 < 1``, ``software missing: pytorch``).  Past MAX_REASON_MEMBERS
        only the count is named and the list stays in ``detail``.

        A 400 is ambiguous -- it is also what a malformed body earns --
        so only the dispatcher's two placement refusals are reported as a
        missing resource; every other 400 is a broken call, and saying
        "no resource satisfies the requirements" about one would send
        whoever reads it looking in the wrong place.
        """

        if status == 409:
            parts = cls._member_parts(data)
            if not parts:
                return REASON_NO_RESOURCES_JOINED
            if len(parts) <= MAX_REASON_MEMBERS:
                return '%s (%s)' % (REASON_NO_RESOURCE, '; '.join(parts))
            return '%s (%d resources checked)' % (REASON_NO_RESOURCE,
                                                  len(parts))

        if status == 400:
            text = (detail or '').lower()
            if 'no member satisfies' in text or 'exceed every pilot_size' \
                    in text:
                return REASON_NO_RESOURCE

        return REASON_NOT_STARTED

    @classmethod
    def _submit_detail(cls, detail: str, data: Any) -> str:
        """Fold the 409's per-member reasons into the technical detail.

        ``no resource satisfies requirements: a.cpu: gpus 0 < 1;
        b.gpu: node_hours exhausted`` -- the phrase on screen says *that*
        no resource matched, this says which one failed on what.  Sorted
        by member id so the same refusal always reads the same way.
        """

        parts = cls._member_parts(data)
        if not parts:
            return detail
        listed = '; '.join(parts)
        return '%s: %s' % (detail, listed) if detail else listed

    async def task(self, task_id: str) -> Dict[str, Any]:

        status, data = await self._call(
            FEDERATION_PLUGIN, 'GET',
            '/%s/task/%s/%s' % (FEDERATION_PLUGIN, DEFAULT_SID, task_id))
        if status >= 400:
            raise call_error(status, self._detail(data), REASON_NO_STATUS)
        return data if isinstance(data, dict) else {}

    async def resources(self) -> List[Dict[str, Any]]:

        status, data = await self._call(
            FEDERATION_PLUGIN, 'GET',
            '/%s/resources/%s' % (FEDERATION_PLUGIN, DEFAULT_SID))
        if status >= 400:
            return []
        return list((data or {}).get('resources') or [])

    async def stage_out(self, dispatcher_sid: str, task_id: str,
                        filename: str) -> Optional[bytes]:

        status, data = await self._call(
            DISPATCHER_PLUGIN, 'GET',
            '/%s/stage_out/%s/%s/%s' % (DISPATCHER_PLUGIN, dispatcher_sid,
                                        task_id, filename))
        if status >= 400 and is_transient_status(status, self._detail(data)):
            # an outage, not a missing file: the runner retries it
            raise call_error(status, self._detail(data), REASON_INCOMPLETE)
        if status >= 400 or not isinstance(data, dict):
            return None
        content = data.get('content_b64')
        if not content:
            return None
        try:
            return base64.b64decode(content)
        except (ValueError, TypeError):
            return None

    async def cancel_task(self, dispatcher_sid: str, task_id: str) -> bool:

        status, data = await self._call(
            DISPATCHER_PLUGIN, 'POST',
            '/%s/cancel/%s/%s' % (DISPATCHER_PLUGIN, dispatcher_sid, task_id))
        if status < 400:
            return True
        detail = self._detail(data)
        if is_transient_status(status, detail):
            raise call_error(status, detail, REASON_STOPPED)
        # an unknown task (404) has nothing left to cancel; anything else
        # is a refusal that a retry does not change
        return status == 404

    # -- pilot-side staging (remote participant, broker caller) -----------
    def _staging_client(self, endpoint: str) -> Any:
        """Build + register a caller-backed ``StagingClient`` (blocking)."""

        from radical.orbit.plugin_staging         import StagingClient
        from radical.orbit.plugin_task_dispatcher import _CallerSyncHTTP

        caller = getattr(self._app.state, 'broker_caller', None)
        if caller is None:
            return None
        http   = _CallerSyncHTTP(caller, endpoint, timeout=self._timeout)
        client = StagingClient(http, '/staging', endpoint_id=endpoint,
                               plugin_name='staging')
        client.register_session()
        return client

    async def staging_get(self, endpoint: str,
                          path: str) -> Optional[bytes]:

        if getattr(self._app.state, 'broker_caller', None) is None:
            return None

        def _work() -> Optional[bytes]:
            client = self._staging_client(endpoint)
            if client is None:
                return None
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    tgt = os.path.join(tmp, os.path.basename(path))
                    client.get(path, tgt)
                    with open(tgt, 'rb') as fd:
                        return fd.read()
            finally:
                client.close()

        try:
            return await asyncio.to_thread(_work)
        except Exception as exc:                              # noqa: BLE001
            log.info('[atomic_campaign] staging get %s:%s failed: %s',
                     endpoint, path, exc)
            return None


# --------------------------------------------------------------------------
class _TimedRuntime:
    """An ``EndpointRuntime`` whose ``call`` has a bounded default timeout.

    ``_RuntimeHTTP`` (what ``StagingClient`` rides on) calls the runtime
    without a timeout, i.e. with the runtime's 600 s default.
    """

    def __init__(self, runtime: Any, timeout: float) -> None:
        self._runtime = runtime
        self._timeout = timeout

    def call(self, dst: str, method: str, path: str, *,
             body: bytes = b'', headers: Optional[Dict[str, str]] = None,
             timeout: Optional[float] = None) -> Any:
        return self._runtime.call(dst, method, path, body=body,
                                  headers=headers,
                                  timeout=timeout or self._timeout)


# --------------------------------------------------------------------------
class _RemoteFederationAPI(_FederationAPI):
    """The runner's view of the broker from a service endpoint (``ep_cm``).

    Same routes, bodies and error mapping as the in-process API; only the
    transport differs: ``EndpointRuntime.call(dst='broker', ...)``.

    - ``call`` is **synchronous** and blocks on a future that is scheduled
      on the endpoint's own work loop -- the loop this plugin's coroutines
      run on.  Called from a coroutine it would deadlock, so every call
      runs in a thread (``asyncio.to_thread``).
    - every call carries an explicit :data:`REMOTE_CALL_TIMEOUT`; a timeout
      or a lost connection surfaces as ``TimeoutError`` / ``ConnectionError``
      (transient for the runner).
    - the federation and dispatcher routes are called with the reserved
      ``default`` session (federation) and the dispatcher sid the submit
      answered with, both reachable from any participant.
    - a pilot's ``staging`` plugin is reached the same way
      (``StagingClient`` over ``_RuntimeHTTP(runtime, child_endpoint)``);
      the broker caller that the in-process API uses does not exist here.
    """

    def __init__(self, app: FastAPI, *, runtime: Any = None,
                 dst: str = BROKER_DST,
                 call_timeout: float = REMOTE_CALL_TIMEOUT,
                 staging_timeout: float = REMOTE_STAGING_TIMEOUT) -> None:
        super().__init__(app, call_timeout=call_timeout)
        self._rt              = runtime
        self._dst             = dst
        self._staging_timeout = staging_timeout

    def _runtime(self) -> Any:
        if self._rt is not None:
            return self._rt
        rt = getattr(self._app.state, 'endpoint_service', None)
        return rt if callable(getattr(rt, 'call', None)) else None

    def has_plugin(self, name: str) -> bool:
        # what the broker hosts is the broker's to say (a 404 / 503)
        return self._runtime() is not None

    async def _call(self, plugin: str, method: str, path: str,
                    body: Optional[Dict[str, Any]] = None
                    ) -> Tuple[int, Any]:

        rt = self._runtime()
        if rt is None:
            log.warning('[atomic_campaign] no endpoint runtime to reach the '
                        'broker through')
            raise FederationUnavailable(FederationUnavailable.DEFAULT)

        payload = json.dumps(body).encode() if body is not None else b''
        resp = await asyncio.to_thread(rt.call, self._dst, method, path,
                                       body=payload,
                                       headers=dict(_JSON_HEADERS),
                                       timeout=self._timeout)
        raw = bytes(getattr(resp, 'content', b'') or b'')
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {'detail': raw.decode('utf-8', 'replace')}
        return int(getattr(resp, 'status_code', 502)), data

    async def staging_get(self, endpoint: str,
                          path: str) -> Optional[bytes]:

        rt = self._runtime()
        if rt is None:
            return None

        def _work() -> Optional[bytes]:
            from radical.orbit.plugin_staging import StagingClient
            from radical.orbit.runtime_client import _RuntimeHTTP

            http   = _RuntimeHTTP(_TimedRuntime(rt, self._staging_timeout),
                                  endpoint)
            client = StagingClient(http, '/staging', endpoint_id=endpoint,
                                   plugin_name='staging')
            client.register_session()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    tgt = os.path.join(tmp, os.path.basename(path))
                    client.get(path, tgt)
                    with open(tgt, 'rb') as fd:
                        return fd.read()
            finally:
                client.close()

        try:
            return await asyncio.to_thread(_work)
        except Exception as exc:                              # noqa: BLE001
            log.info('[atomic_campaign] staging get %s:%s failed: %s',
                     endpoint, path, exc)
            if is_transient(exc):
                raise                     # the broker is away: retried
            return None


# --------------------------------------------------------------------------
def make_federation_api(app: FastAPI) -> FederationAPI:
    """The federation API for this host: in-process on the broker, over
    the endpoint runtime everywhere else."""

    if getattr(app.state, 'is_broker', False):
        return _FederationAPI(app)
    return _RemoteFederationAPI(app)


# --------------------------------------------------------------------------
class PluginAtomicCampaign(Plugin):
    """Campaign manager for the ATOMIC demo (broker or service endpoint)."""

    plugin_name   = 'atomic_campaign'
    session_class = AtomicCampaignSession
    version       = '0.1.0'

    # read off the CLASS by BrokerPluginHost.get_ui_modules() -> resolve once
    ui_module = _ui_module_path()

    ui_config = {
        'icon'          : '🧪',
        'title'         : 'ATOMIC Campaigns',
        'description'   : 'Parameter-sweep campaigns across the resource '
                          'federation.',
        'refresh_button': True,
    }

    # ----------------------------------------------------------------------
    @classmethod
    def is_enabled(cls, app: FastAPI) -> bool:
        """The broker or a service endpoint -- never a compute node.

        It is not in any role's default plugin set: an endpoint hosts it
        only when started with ``--plugins atomic_campaign`` (``ep_cm``).
        A pilot (role ``compute``) has no business steering campaigns.
        """

        from radical.orbit.utils import host_role
        return host_role(app)['role'] in ('broker', 'login', 'standalone')

    # ----------------------------------------------------------------------
    def __init__(self, app: FastAPI,
                 instance_name: str = 'atomic_campaign',
                 state_root: Optional[Any] = None,
                 store_root: Optional[Any] = None,
                 fed_api: Optional[FederationAPI] = None,
                 max_concurrent_workflows: int = 8,
                 stage_timeout_sec: float = 900.0,
                 poll_interval_sec: float = 1.0,
                 poll_max_interval_sec: float = 3.0,
                 unreachable_budget_sec: Optional[float] = None,
                 drain_interval_sec: float = DRAIN_INTERVAL) -> None:

        super().__init__(app, instance_name)

        self._state_root = Path(state_root) if state_root \
                           else default_state_root()
        self._state_file = self._state_root / STATE_FILE
        self._store      = ResultStore(Path(store_root) if store_root
                                       else default_store_root())
        self._fed        = fed_api if fed_api is not None \
                           else make_federation_api(app)
        self._planner    = SweepPlanner()
        self._max_wf     = int(max_concurrent_workflows)
        # not `_timeout`: that is Plugin's session-expiry hook (orbit #157)
        self._stage_timeout = float(stage_timeout_sec)
        self._poll       = float(poll_interval_sec)
        self._poll_max   = float(poll_max_interval_sec)
        self._budget     = self._budget_from(unreachable_budget_sec)
        self._drain_every = float(drain_interval_sec)
        self._drainer: Optional[asyncio.Task] = None
        # set to make the drainer try now instead of after `_drain_every`
        self._drain_wake = asyncio.Event()
        # set by a stop: interrupted tasks wait for the next start
        self._stopping   = False

        self._campaigns: Dict[str, Campaign]       = {}
        # CampaignRunner (sweep) or AucamCampaignDriver (aucam): both offer
        # run(campaign) / cancel()
        self._runners:   Dict[str, Any] = {}
        self._aucam_backend: Any = None     # shared by every AuCaM driver
        # two first AuCaM submits at once must not build two backends
        self._aucam_lock = asyncio.Lock()
        self._drivers:   Dict[str, asyncio.Task]   = {}

        for camp in load_campaigns(self._state_file):
            self._campaigns[camp.campaign_id] = camp
        if self._campaigns:
            log.info('[%s] recovered %d campaign(s) from %s',
                     self.instance_name, len(self._campaigns),
                     self._state_file)
            self._persist()

        self.add_route_post('campaigns/{sid}',        self._route_submit)
        self.add_route_get ('campaigns/{sid}',        self._route_list)
        self.add_route_get ('campaign/{sid}/{cid}',   self._route_campaign)
        self.add_route_get ('results/{sid}/{cid}',    self._route_results)
        self.add_route_post('cancel/{sid}/{cid}',     self._route_cancel)
        self.add_route_get ('store/{sid}',            self._route_store)

    # -- helpers -----------------------------------------------------------
    @property
    def store(self) -> ResultStore:
        return self._store

    @staticmethod
    def _budget_from(value: Optional[float]) -> float:
        """The unreachable budget: argument, then the env, then 300 s."""

        if value is None:
            env = os.environ.get(_BUDGET_ENV)
            try:
                value = float(env) if env else DEFAULT_UNREACHABLE_BUDGET
            except ValueError:
                log.warning('[atomic_campaign] ignoring $%s=%r', _BUDGET_ENV,
                            env)
                value = DEFAULT_UNREACHABLE_BUDGET
        return max(0.0, float(value))

    # -- pending cancels ---------------------------------------------------
    def _ensure_drainer(self) -> None:
        """Start the pending-cancel drainer if there is work for it.

        The drainer is the one place that settles ``pending_cancels``.
        Called whenever the plugin gets a chance to run on its loop (a
        route, a topology change -- i.e. after a reconnect --, a finished
        driver); a no-op while nothing is pending or a drainer runs.
        """

        if self._stopping:
            return
        if self._drainer is not None and not self._drainer.done():
            return
        if not any(c.pending_cancels for c in self._campaigns.values()):
            return
        try:
            self._drainer = asyncio.get_running_loop().create_task(
                self._drain_loop())
        except RuntimeError:                       # no loop (yet)
            self._drainer = None

    def _wake_drainer(self) -> None:
        """A runner saw the federation answer again (or left a cancel
        pending): try the pending cancels now."""

        self._ensure_drainer()
        self._drain_wake.set()

    async def _drain_loop(self) -> None:
        """Retry the unconfirmed cancels until none is left."""

        while True:
            self._drain_wake.clear()
            pending = [c for c in list(self._campaigns.values())
                       if c.pending_cancels]
            if not pending:
                return
            settled = 0
            for camp in pending:
                try:
                    settled += await drain_pending_cancels(self._fed, camp)
                except Exception as exc:                      # noqa: BLE001
                    log.info('[%s] cancel drain failed: %s',
                             self.instance_name, exc)
            if settled:
                log.info('[%s] %d pending cancel(s) settled',
                         self.instance_name, settled)
                self._persist()
            try:
                await asyncio.wait_for(self._drain_wake.wait(),
                                       self._drain_every)
            except asyncio.TimeoutError:
                pass

    async def on_topology_change(self, participants: dict) -> None:
        """A (re)connect is the moment to retry the pending cancels."""

        await super().on_topology_change(participants)
        self._ensure_drainer()
        # a (re)connect is rare and is exactly when the federation may answer
        # again: wake a sleeping drainer now, not after DRAIN_INTERVAL
        self._wake_drainer()

    def _persist(self) -> None:
        """Write the campaign set out (small, atomic, after every change).

        Finished campaigns are capped (``prune_campaigns``) so a broker that
        is up all day keeps neither an unbounded state file nor an unbounded
        ``GET campaigns`` listing.
        """

        keep = prune_campaigns(list(self._campaigns.values()))
        if len(keep) != len(self._campaigns):
            kept = {c.campaign_id for c in keep}
            for cid in [c for c in self._campaigns if c not in kept]:
                self._campaigns.pop(cid, None)
                self._runners.pop(cid, None)
        try:
            save_campaigns(self._state_file, keep)
        except OSError as exc:
            log.warning('[%s] could not persist state to %s: %s',
                        self.instance_name, self._state_file, exc)

    async def _session(self, request: Request) -> str:
        """Resolve the route's ``{sid}`` (always ``default`` in practice)."""

        sid = request.path_params['sid']
        # a route starts a drainer that is not running, but never wakes a
        # sleeping one: with the federation down, UI polls every few
        # seconds would turn `drain_interval_sec` into their rate, each
        # pass a failed cancel (up to REMOTE_CALL_TIMEOUT) and a log line
        self._ensure_drainer()
        await self._ensure_default_session()
        if sid not in self._sessions:
            raise HTTPException(status_code=404,
                                detail='unknown session id: %s' % sid)
        self._touch(sid)
        return sid

    def _campaign(self, request: Request) -> Campaign:
        cid  = request.path_params['cid']
        camp = self._campaigns.get(cid)
        if camp is None:
            raise HTTPException(status_code=404,
                                detail='unknown campaign: %s' % cid)
        return camp

    @staticmethod
    async def _body(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:                                     # noqa: BLE001
            body = {}
        return body if isinstance(body, dict) else {}

    # -- routes ------------------------------------------------------------
    async def _route_submit(self, request: Request) -> dict:
        """``POST campaigns/{sid}`` -- plan a campaign and start driving it.

        Body: ``{"workflow": <spec>, "sweep": {"param": [values]},
        "name"?, "max_concurrent_workflows"?, "stage_timeout_sec"?,
        "planner"?: "sweep"|"aucam", "aucam"?: {...}}``.  With
        ``planner: aucam`` the Campaign Manager launches the frames itself
        (see :mod:`atomic_wm.campaign.aucam_adapter` for the ``aucam`` keys);
        503 when AuCaM is not installed, and while the service stops.

        The spec is validated synchronously (400 on a bad one); everything
        that can only fail later -- no federation, no resource, a failing
        task -- shows up as campaign/workflow state, not as an HTTP error.
        """

        await self._session(request)
        body = await self._body(request)

        workflow = body.get('workflow')
        if workflow is None:
            raise HTTPException(status_code=400,
                                detail="body needs a 'workflow' spec")

        # imported here: the adapter is only needed for 'aucam' campaigns,
        # and it imports without AuCaM installed
        from atomic_wm.campaign import aucam_adapter
        try:
            planner = aucam_adapter.planner_of(body)
        except PlannerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        max_wf  = self._positive(body, 'max_concurrent_workflows',
                                 self._max_wf)
        timeout = self._positive(body, 'stage_timeout_sec',
                                 self._stage_timeout)

        stage_kwargs = dict(stage_timeout=float(timeout),
                            unreachable_budget=self._budget,
                            poll_interval=self._poll,
                            poll_max_interval=self._poll_max,
                            wake_drainer=self._wake_drainer)
        name = str(body.get('name') or workflow.get('name') or 'campaign')

        if planner == 'aucam':
            camp, runner = await self._aucam_campaign(body, name,
                                                      stage_kwargs)
        else:
            try:
                instances = self._planner.expand(workflow, body.get('sweep'))
            except PlannerError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            camp = Campaign(campaign_id=new_campaign_id(), name=name,
                            sweep=dict(body.get('sweep') or {}),
                            workflows=instances)
            runner = CampaignRunner(
                self._fed, self._store,
                max_concurrent_workflows=int(max_wf),
                on_change=self._persist, **stage_kwargs)

        # after the route's last await: a stop that landed meanwhile has
        # already stopped every driver, and nobody would stop this one
        self._refuse_when_stopping()
        self._campaigns[camp.campaign_id] = camp
        self._persist()
        self._runners[camp.campaign_id] = runner
        self._drivers[camp.campaign_id] = asyncio.create_task(
            self._drive(camp, runner))

        log.info('[%s] campaign %s: %d workflow(s), sweep %s',
                 self.instance_name, camp.campaign_id, len(camp.workflows),
                 camp.sweep)
        return camp.brief()

    def _refuse_when_stopping(self) -> None:
        """503 for a submit while the service stops (RULE A)."""

        if self._stopping:
            raise HTTPException(status_code=503,
                                detail='the campaign service is stopping')

    async def _aucam_campaign(self, body: Dict[str, Any], name: str,
                              stage_kwargs: Dict[str, Any]
                              ) -> Tuple[Campaign, Any]:
        """Validate an ``aucam`` submit; return (campaign, driver)."""

        from atomic_wm.campaign import aucam_adapter
        try:
            plan = aucam_adapter.parse_aucam_request(body)
        except aucam_adapter.AucamUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PlannerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        async with self._aucam_lock:
            if self._aucam_backend is None:
                self._aucam_backend = await aucam_adapter.make_backend()
                if self._stopping:
                    # built after the stop shut the shared one down
                    await self._stop_aucam_backend()
        camp = Campaign(campaign_id=new_campaign_id(), name=name,
                        sweep=dict(body.get('sweep') or {}),
                        planner='aucam', workflows=[])
        driver = aucam_adapter.AucamCampaignDriver(
            self._fed, self._store, plan, on_change=self._persist,
            backend=self._aucam_backend, **stage_kwargs)
        return camp, driver

    async def _route_list(self, request: Request) -> dict:
        """``GET campaigns/{sid}`` -- one summary per campaign, newest first."""

        await self._session(request)
        camps = sorted(self._campaigns.values(),
                       key=lambda c: c.created_at, reverse=True)
        return {'campaigns': [c.summary() for c in camps]}

    async def _route_campaign(self, request: Request) -> dict:
        """``GET campaign/{sid}/{cid}`` -- the full state of one campaign."""

        await self._session(request)
        return self._campaign(request).to_dict()

    async def _route_results(self, request: Request) -> dict:
        """``GET results/{sid}/{cid}`` -- parsed metrics, what the UI plots."""

        await self._session(request)
        return self._store.results(self._campaign(request))

    async def _route_cancel(self, request: Request) -> dict:
        """``POST cancel/{sid}/{cid}`` -- stop driving; running stages end."""

        await self._session(request)
        camp   = self._campaign(request)
        runner = self._runners.get(camp.campaign_id)
        if camp.is_terminal():
            return {'campaign_id': camp.campaign_id, 'state': camp.state,
                    'canceled': False, 'reason': 'already terminal'}
        if runner is not None:
            runner.cancel()
        # deliberately no campaign.reason here: a cancel that arrives after
        # the last stage finished must still read DONE, not "stopped"
        self._persist()
        return {'campaign_id': camp.campaign_id, 'state': camp.state,
                'canceled': True}

    async def _route_store(self, request: Request) -> dict:
        """``GET store/{sid}`` -- the central store layout, for UI links."""

        await self._session(request)
        cid = request.query_params.get('cid') if request.query_params else None
        try:
            return self._store.layout(cid or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # -- driver ------------------------------------------------------------
    async def _drive(self, camp: Campaign, runner: Any) -> None:
        """Run one campaign to its end; never let an error escape the task."""

        try:
            await runner.run(camp)
        except asyncio.CancelledError:
            # shutdown() has already stamped INTERRUPTED; a cancel from
            # anywhere else means the campaign was stopped
            self._terminate(camp, CANCELED, REASON_STOPPED)
            raise
        except FederationUnavailable as exc:
            self._terminate(camp, FAILED,
                            str(exc) or FederationUnavailable.DEFAULT)
        except FederationCallError as exc:
            log.warning('[%s] campaign %s: %s (%s)', self.instance_name,
                        camp.campaign_id, exc.reason, exc.detail)
            self._terminate(camp, FAILED, exc.reason, exc.detail)
        except Exception as exc:                              # noqa: BLE001
            log.exception('[%s] campaign %s crashed', self.instance_name,
                          camp.campaign_id)
            self._terminate(camp, FAILED, REASON_INCOMPLETE,
                            '%s: %s' % (type(exc).__name__, exc))
        finally:
            # an AuCaM driver builds its outcome on every way out -- a
            # shutdown or a crash included -- and it must not be lost
            outcome = getattr(runner, 'outcome', None)
            if outcome is not None:
                camp.outcome = outcome
            self._drivers.pop(camp.campaign_id, None)
            self._runners.pop(camp.campaign_id, None)
            self._persist()
            self._ensure_drainer()
            log.info('[%s] campaign %s -> %s (%s)', self.instance_name,
                     camp.campaign_id, camp.state, camp.reason or 'ok')
            try:
                await self.send_notification('campaign_state', camp.summary())
            except Exception:                                 # noqa: BLE001
                pass

    # ----------------------------------------------------------------------
    @staticmethod
    def _terminate(camp: Campaign, state: str, reason: str,
                   detail: str = '') -> None:
        """Stamp a final state -- unless the campaign already has one.

        ``shutdown`` marks INTERRUPTED *before* cancelling the driver, so the
        driver's own CancelledError branch must not overwrite it.
        """

        if camp.state in TERMINAL_STATES:
            return
        camp.state       = state
        camp.reason      = camp.reason or reason
        camp.detail      = camp.detail or (detail or None)
        camp.finished_at = camp.finished_at or time.time()

    # ----------------------------------------------------------------------
    @staticmethod
    def _positive(body: Dict[str, Any], key: str, default: float) -> float:
        """Read a positive number from the request body (400 if it is not)."""

        value = body.get(key)
        if value is None or value == '':
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float('nan')
        if not number > 0 or not math.isfinite(number):  # NaN, inf
            raise HTTPException(status_code=400,
                                detail='%s must be a positive number' % key)
        return number

    # -- lifetime ----------------------------------------------------------
    async def shutdown(self) -> None:
        """Stop every driver, persist, then let the base close the sessions.

        Order matters: a campaign that is still running when the service goes
        down is INTERRUPTED, not CANCELED -- nobody asked for it to stop.  So
        every unfinished campaign is stamped *before* its driver task is
        cancelled, and ``_terminate`` refuses to overwrite that stamp.

        Shutdown does **not** cancel ORBIT tasks.  Only an explicit campaign
        cancel does that at once.  The tasks of interrupted stages are
        queued (``pending_cancels``, persisted) and cancelled once the
        federation answers after the next start: without a resume their
        outputs are never collected, but a shutdown must not depend on a
        federation that may be going down with it.
        """

        await self._stop_drivers()
        await super().shutdown()

    async def on_host_stop(self) -> None:
        """The endpoint (``ep_cm``) stops: same as :meth:`shutdown`, but
        sessions are the runtime's to keep (it does not call ``shutdown``
        on an endpoint)."""

        await self._stop_drivers()

    async def _stop_drivers(self) -> None:
        """Mark unfinished campaigns INTERRUPTED, cancel drivers, persist."""

        self._stopping = True

        for cid, camp in self._campaigns.items():
            if not camp.is_terminal():
                mark_interrupted(camp)
                log.info('[%s] campaign %s interrupted by shutdown',
                         self.instance_name, cid)

        # cancel every driver before awaiting any: awaiting one at a time
        # let the others keep polling and submit new stages meanwhile
        pending = [task for task in self._drivers.values() if not task.done()]
        for task in pending:
            task.cancel()
        if self._drainer is not None:
            self._drainer.cancel()
            pending.append(self._drainer)
            self._drainer = None
        await asyncio.gather(*pending, return_exceptions=True)
        self._drivers.clear()
        self._runners.clear()
        self._persist()
        await self._stop_aucam_backend()

    async def _stop_aucam_backend(self) -> None:
        """Shut the shared AuCaM backend down -- after every driver that
        used it has stopped; bounded, a stop must not hang on it."""

        backend, self._aucam_backend = self._aucam_backend, None
        if backend is None:
            return
        try:
            await asyncio.wait_for(backend.shutdown(), BACKEND_STOP_TIMEOUT)
        except Exception as exc:                              # noqa: BLE001
            log.warning('[%s] AuCaM backend shutdown: %s',
                        self.instance_name, describe(exc))

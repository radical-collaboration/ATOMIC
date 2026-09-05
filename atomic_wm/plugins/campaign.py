"""``atomic_campaign`` -- the broker-hosted campaign plugin.

One POST creates a campaign: a workflow spec plus a parameter sweep is
expanded into N workflow instances, each of which runs its stages
sequentially through the ``federation`` plugin, with its outputs collected
into the central store on the broker host.  The plugin owns the driver task,
the campaign state, and the REST surface the CLI and the demo UI use.

Everything ATOMIC-specific about *how* a campaign is planned and executed
lives in :mod:`atomic_wm.campaign`; this module is the orbit shell around it:
plugin lifetime, routes, sessions, persistence, and the in-process calls to
the sibling broker plugins (see ``plans/00-overview.md`` §Facts -- broker
plugins call each other through ``app.state.endpoint_service.handle_request``,
never through the broker caller, which cannot address the broker itself).
"""

from __future__ import annotations

import asyncio
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
                                        FederationUnavailable)
from atomic_wm.campaign.state   import (Campaign, STATE_FILE, TERMINAL_STATES,
                                        default_state_root,
                                        default_store_root, load_campaigns,
                                        new_campaign_id, save_campaigns)
from atomic_wm.campaign.store   import ResultStore

log = logging.getLogger('radical.orbit')

FEDERATION_PLUGIN = 'federation'
DISPATCHER_PLUGIN = 'task_dispatcher'

_JSON_HEADERS = {'content-type': 'application/json'}


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
            log.warning('[%s] %s plugin is not hosted on this broker',
                        FEDERATION_PLUGIN, plugin)
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
            raise RuntimeError('could not place the stage on a resource (%s)'
                               % (self._detail(data) or 'HTTP %d' % status))
        return data if isinstance(data, dict) else {}

    async def task(self, task_id: str) -> Dict[str, Any]:

        status, data = await self._call(
            FEDERATION_PLUGIN, 'GET',
            '/%s/task/%s/%s' % (FEDERATION_PLUGIN, DEFAULT_SID, task_id))
        if status >= 400:
            raise RuntimeError('could not read the stage status (%s)'
                               % (self._detail(data) or 'HTTP %d' % status))
        return data if isinstance(data, dict) else {}

    async def resources(self) -> List[Dict[str, Any]]:

        status, data = await self._call(
            FEDERATION_PLUGIN, 'GET',
            '/%s/resources/%s' % (FEDERATION_PLUGIN, DEFAULT_SID))
        if status >= 400:
            return []
        return list((data or {}).get('resources') or [])

    async def stage_in(self, dispatcher_sid: str, pool: str, task_id: str,
                       filename: str, data: bytes) -> Dict[str, Any]:

        status, out = await self._call(
            DISPATCHER_PLUGIN, 'POST',
            '/%s/stage_in/%s/%s' % (DISPATCHER_PLUGIN, dispatcher_sid,
                                    task_id),
            {'pool'       : pool,
             'filename'   : filename,
             'content_b64': base64.b64encode(data).decode('ascii'),
             'overwrite'  : True})
        if status >= 400:
            raise RuntimeError('could not place the input file (%s)'
                               % (self._detail(out) or 'HTTP %d' % status))
        return out if isinstance(out, dict) else {}

    async def stage_out(self, dispatcher_sid: str, task_id: str,
                        filename: str) -> Optional[bytes]:

        status, data = await self._call(
            DISPATCHER_PLUGIN, 'GET',
            '/%s/stage_out/%s/%s/%s' % (DISPATCHER_PLUGIN, dispatcher_sid,
                                        task_id, filename))
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

        status, _ = await self._call(
            DISPATCHER_PLUGIN, 'POST',
            '/%s/cancel/%s/%s' % (DISPATCHER_PLUGIN, dispatcher_sid, task_id))
        return status < 400

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

    async def staging_put(self, endpoint: str, path: str,
                          data: bytes) -> bool:

        if getattr(self._app.state, 'broker_caller', None) is None:
            return False

        def _work() -> bool:
            client = self._staging_client(endpoint)
            if client is None:
                return False
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    src = os.path.join(tmp, os.path.basename(path))
                    with open(src, 'wb') as fd:
                        fd.write(data)
                    client.put(src, path, overwrite=True)
                return True
            finally:
                client.close()

        try:
            return await asyncio.to_thread(_work)
        except Exception as exc:                              # noqa: BLE001
            log.info('[atomic_campaign] staging put %s:%s failed: %s',
                     endpoint, path, exc)
            return False


# --------------------------------------------------------------------------
class PluginAtomicCampaign(Plugin):
    """Broker-hosted campaign manager for the ATOMIC demo."""

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
        """Broker hosts only: the campaign owns broker-wide state."""

        from radical.orbit.utils import host_role
        return host_role(app)['role'] == 'broker'

    # ----------------------------------------------------------------------
    def __init__(self, app: FastAPI,
                 instance_name: str = 'atomic_campaign',
                 state_root: Optional[Any] = None,
                 store_root: Optional[Any] = None,
                 fed_api: Optional[FederationAPI] = None,
                 max_concurrent_workflows: int = 8,
                 stage_timeout_sec: float = 900.0,
                 poll_interval_sec: float = 1.0,
                 poll_max_interval_sec: float = 3.0) -> None:

        super().__init__(app, instance_name)

        self._state_root = Path(state_root) if state_root \
                           else default_state_root()
        self._state_file = self._state_root / STATE_FILE
        self._store      = ResultStore(Path(store_root) if store_root
                                       else default_store_root())
        self._fed        = fed_api if fed_api is not None \
                           else _FederationAPI(app)
        self._planner    = SweepPlanner()
        self._max_wf     = int(max_concurrent_workflows)
        self._timeout    = float(stage_timeout_sec)
        self._poll       = float(poll_interval_sec)
        self._poll_max   = float(poll_max_interval_sec)

        self._campaigns: Dict[str, Campaign]       = {}
        self._runners:   Dict[str, CampaignRunner] = {}
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

    def _persist(self) -> None:
        """Write the whole campaign set out (small, atomic, after changes)."""

        try:
            save_campaigns(self._state_file, list(self._campaigns.values()))
        except OSError as exc:
            log.warning('[%s] could not persist state to %s: %s',
                        self.instance_name, self._state_file, exc)

    async def _session(self, request: Request) -> str:
        """Resolve the route's ``{sid}`` (always ``default`` in practice)."""

        sid = request.path_params['sid']
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
        "name"?, "max_concurrent_workflows"?, "stage_timeout_sec"?}``.

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
        try:
            instances = self._planner.expand(workflow, body.get('sweep'))
        except PlannerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        camp = Campaign(campaign_id=new_campaign_id(),
                        name=str(body.get('name')
                                 or workflow.get('name') or 'campaign'),
                        sweep=dict(body.get('sweep') or {}),
                        workflows=instances)
        self._campaigns[camp.campaign_id] = camp
        self._persist()

        runner = CampaignRunner(
            self._fed, self._store,
            max_concurrent_workflows=int(body.get('max_concurrent_workflows')
                                         or self._max_wf),
            on_change=self._persist,
            stage_timeout=float(body.get('stage_timeout_sec')
                                or self._timeout),
            poll_interval=self._poll, poll_max_interval=self._poll_max)
        self._runners[camp.campaign_id] = runner
        self._drivers[camp.campaign_id] = asyncio.create_task(
            self._drive(camp, runner))

        log.info('[%s] campaign %s: %d workflow(s), sweep %s',
                 self.instance_name, camp.campaign_id, len(camp.workflows),
                 camp.sweep)
        return camp.brief()

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
        camp.reason = camp.reason or 'canceled by request'
        self._persist()
        return {'campaign_id': camp.campaign_id, 'state': camp.state,
                'canceled': True}

    async def _route_store(self, request: Request) -> dict:
        """``GET store/{sid}`` -- the central store layout, for UI links."""

        await self._session(request)
        cid = request.query_params.get('cid') if request.query_params else None
        return self._store.layout(cid or None)

    # -- driver ------------------------------------------------------------
    async def _drive(self, camp: Campaign, runner: CampaignRunner) -> None:
        """Run one campaign to its end; never let an error escape the task."""

        try:
            await runner.run(camp)
        except asyncio.CancelledError:
            camp.state  = 'CANCELED'
            camp.reason = camp.reason or 'the campaign was stopped'
            camp.finished_at = camp.finished_at or time.time()
            raise
        except FederationUnavailable as exc:
            camp.state  = 'FAILED'
            camp.reason = str(exc) or FederationUnavailable.DEFAULT
            camp.finished_at = time.time()
        except Exception as exc:                              # noqa: BLE001
            log.exception('[%s] campaign %s crashed', self.instance_name,
                          camp.campaign_id)
            camp.state  = 'FAILED'
            camp.reason = '%s: %s' % (type(exc).__name__, exc)
            camp.finished_at = time.time()
        finally:
            self._drivers.pop(camp.campaign_id, None)
            self._persist()
            log.info('[%s] campaign %s -> %s (%s)', self.instance_name,
                     camp.campaign_id, camp.state, camp.reason or 'ok')
            try:
                await self.send_notification('campaign_state', camp.summary())
            except Exception:                                 # noqa: BLE001
                pass

    # -- lifetime ----------------------------------------------------------
    async def shutdown(self) -> None:
        """Stop every driver, persist, then let the base close the sessions."""

        for cid, task in list(self._drivers.items()):
            if task.done():
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):       # noqa: BLE001
                pass
            camp = self._campaigns.get(cid)
            if camp is not None and camp.state not in TERMINAL_STATES:
                camp.state  = 'INTERRUPTED'
                camp.reason = camp.reason or 'the service was restarted'
        self._drivers.clear()
        self._persist()
        await super().shutdown()

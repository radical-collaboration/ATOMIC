"""Executor seam: run workflow instances through the federation.

``StageRunner`` drives ONE stage: submit the task through the federation
**with the previous stage's outputs in the submit body**, poll the task
until it is terminal, collect the declared outputs immediately, write a
manifest.  ``CampaignRunner`` drives a whole campaign: stages sequentially
within a workflow, workflows concurrently (bounded by a semaphore), with
failure isolated to the workflow it happened in.

Placement is what the **poll** says.  A federation pool is a capability
class (``fed-cpu``, ``fed-gpu``) with several members, and the member --
i.e. the actual resource -- is only chosen when the task is dispatched.
The submit response therefore names an advisory resource, and every poll
may correct it; ``resource: null`` is a legitimate answer for a task that
is not placed (yet, or any more) and never fails a stage.

Everything the runner needs from the outside world is behind
:class:`FederationAPI` -- the plugin implements it with in-process calls to
the ``federation`` / ``task_dispatcher`` plugins and broker-caller calls to a
pilot's ``staging`` plugin; the tests implement it with a scripted fake.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, \
                   Tuple

from .state import Campaign, StageRun, WorkflowInstance
from .state import (CANCELED, DONE, FAILED, PENDING, RUNNING, SKIPPED,
                    STAGING, SUBMITTED, TERMINAL_STATES)
from .store import ResultStore, Source, stage_manifest

log = logging.getLogger('radical.orbit')

# dispatcher task states we treat as terminal (00-overview "Facts")
TASK_TERMINAL = ('DONE', 'FAILED', 'CANCELED')

# tools that only exist on PATH inside a pilot whose venv has atomic-wm
# installed; $ATOMIC_TOOL_PREFIX gives them an explicit home (see docs).
_TOOL_PREFIX_ENV = 'ATOMIC_TOOL_PREFIX'

# consecutive failed status lookups before a stage gives up
MAX_POLL_FAILURES = 10

# Every ``reason`` below is shown on screen verbatim, so the set is small,
# fixed and written in the demo's vocabulary.  Whatever ORBIT said goes to
# ``StageRun.detail`` and to the log.
REASON_NO_RESOURCE = 'no resource satisfies the stage requirements'
REASON_NOT_STARTED = 'the stage could not be started'
REASON_NO_STATUS   = 'the stage status could not be read'
REASON_STOPPED     = 'the campaign was stopped'
REASON_INCOMPLETE  = 'the stage did not complete'
REASON_STAGE_IN    = 'the input file could not be placed on the resource'


# --------------------------------------------------------------------------
def reason_failed(stage: 'StageRun') -> str:
    """The on-screen text for a stage whose task did not succeed.

    The resource is named only once a poll **confirmed** the placement
    (``member_id`` is set).  Before that the resource on the record is
    the advisory one the submit answered with, and blaming a resource
    that may never have seen the task is worse than saying nothing.
    """

    if stage.member_id and stage.resource:
        return 'the stage failed on resource %s' % stage.resource
    return 'the stage failed'


# --------------------------------------------------------------------------
class FederationUnavailable(RuntimeError):
    """The resource federation is not reachable on this service.

    The message ends up on screen verbatim (campaign/stage ``reason``), so
    it is phrased in the demo's vocabulary -- resources, campaigns,
    workflows, stages -- and never in ORBIT's.
    """

    DEFAULT = 'the resource federation is not available'


# --------------------------------------------------------------------------
class StageFailed(RuntimeError):
    """A stage could not be run to a successful, collected end."""


# --------------------------------------------------------------------------
class FederationCallError(RuntimeError):
    """A federation/dispatcher call failed.

    Carries both halves of the story: ``reason`` is the on-screen text (demo
    vocabulary, one of the fixed phrases above) and ``detail`` is whatever
    the other plugin actually said.
    """

    def __init__(self, reason: str, detail: str = '') -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
class TaskNotFound(FederationCallError):
    """The federation does not know this task (404) -- fail fast, do not
    keep polling for the whole stage timeout."""

    def __init__(self, detail: str = '') -> None:
        super().__init__(REASON_NO_STATUS, detail)


# --------------------------------------------------------------------------
class FederationAPI:
    """Everything the runner needs from the outside world.

    The plugin's implementation talks to the ``federation`` and
    ``task_dispatcher`` plugins in-process and to a pilot's ``staging``
    plugin over the broker caller; tests substitute a fake.
    """

    async def submit(self, task: Dict[str, Any],
                     requirements: Dict[str, Any]) -> Dict[str, Any]:
        """``POST federation/submit/default`` -> ``{task, pool, class,
        dispatcher_sid, resource, member, members_eligible}``.

        The task body carries this stage's inputs as ``inputs_b64``; it
        never carries a ``cwd`` -- the dispatcher assigns one when it
        places the task.  ``resource`` in the response is **advisory** and
        ``member`` is ``None`` until dispatch.  Raises
        :class:`FederationUnavailable`.
        """
        raise NotImplementedError

    async def task(self, task_id: str) -> Dict[str, Any]:
        """``GET federation/task/default/<task_id>`` -> dispatcher task dict
        plus ``resource`` / ``member`` / ``member_id`` (and
        ``child_endpoint`` while the pilot lives).  ``resource`` may be
        ``None`` for a task which is not placed."""
        raise NotImplementedError

    async def resources(self) -> List[Dict[str, Any]]:
        """``GET federation/resources/default`` -> the resource records."""
        raise NotImplementedError

    async def stage_out(self, dispatcher_sid: str, task_id: str,
                        filename: str) -> Optional[bytes]:
        """Dispatcher ``stage_out`` -- read a file from the task scratch dir
        on the broker host; ``None`` when it is not there."""
        raise NotImplementedError

    async def staging_get(self, endpoint: str,
                          path: str) -> Optional[bytes]:
        """Pilot-side ``staging get`` (no shared filesystem needed)."""
        raise NotImplementedError

    async def cancel_task(self, dispatcher_sid: str, task_id: str) -> bool:
        """Best-effort dispatcher task cancel."""
        raise NotImplementedError


# --------------------------------------------------------------------------
class StageRunner:
    """Run the stages of one workflow instance, sequentially."""

    def __init__(self, fed: FederationAPI, store: ResultStore, *,
                 poll_interval:     float = 1.0,
                 poll_max_interval: float = 3.0,
                 stage_timeout:     float = 900.0,
                 tool_prefix:       Optional[str] = None,
                 on_change:         Optional[Callable[[], None]] = None,
                 sleep:             Callable[[float], Any] = asyncio.sleep,
                 clock:             Callable[[], float] = time.time) -> None:

        self._fed          = fed
        self._store        = store
        self._poll         = float(poll_interval)
        self._poll_max     = float(poll_max_interval)
        self._timeout      = float(stage_timeout)
        self._tool_prefix  = tool_prefix if tool_prefix is not None \
                             else os.environ.get(_TOOL_PREFIX_ENV)
        self._on_change    = on_change
        self._sleep        = sleep
        self._clock        = clock

    # ----------------------------------------------------------------------
    def _changed(self) -> None:
        """Tell the owner something moved (it persists the state)."""

        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception as exc:                              # noqa: BLE001
            log.warning('[atomic_campaign] state persist failed: %s', exc)

    # ----------------------------------------------------------------------
    def resolve_cmd(self, cmd: Sequence[str]) -> List[str]:
        """Prefix a bare ``atomic-fake-*`` tool with ``$ATOMIC_TOOL_PREFIX``.

        The synthetic workload is on ``PATH`` inside a pilot only when the
        pilot's venv has ``atomic-wm`` installed.  Where it is not, the
        operator points ``ATOMIC_TOOL_PREFIX`` at a bin directory and the
        runner rewrites ``argv[0]`` to an absolute path.
        """

        out = list(cmd)
        if not out or not self._tool_prefix:
            return out
        argv0 = out[0]
        if '/' in argv0 or not argv0.startswith('atomic-fake-'):
            return out
        out[0] = str(Path(self._tool_prefix).expanduser() / argv0)
        return out

    # ----------------------------------------------------------------------
    async def run_workflow(self, campaign: Campaign, wf: WorkflowInstance,
                           cancel: Optional[asyncio.Event] = None) -> None:
        """Run every stage of *wf* in order; stop at the first failure.

        Never raises for a stage failure -- the workflow records it and the
        campaign carries on with the other workflows.  A missing federation
        is the one exception: it is re-raised so the campaign as a whole can
        report it.
        """

        wf.state  = RUNNING
        wf.reason = None
        self._changed()

        produced: Dict[str, bytes] = {}
        failure: Optional[StageRun] = None

        for stage in wf.stages:
            if failure is not None:
                stage.state  = SKIPPED
                stage.reason = 'stage %r did not succeed' % failure.name
                continue
            if cancel is not None and cancel.is_set():
                stage.state  = CANCELED
                stage.reason = REASON_STOPPED
                failure      = stage
                continue
            await self._guarded_stage(campaign, wf, stage, produced, cancel)
            if stage.state != DONE:
                failure = stage

        self._finish_workflow(wf, failure)

    # ----------------------------------------------------------------------
    async def _guarded_stage(self, campaign: Campaign, wf: WorkflowInstance,
                             stage: StageRun, produced: Dict[str, bytes],
                             cancel: Optional[asyncio.Event]) -> None:
        """Run one stage, turning every failure into stage state.

        Only a missing federation and a cancelled driver escape -- the first
        because the whole campaign must report it, the second because the
        asyncio contract says so.
        """

        try:
            await self._run_stage(campaign, wf, stage, produced, cancel)
        except FederationUnavailable as exc:
            stage.state  = FAILED
            stage.reason = str(exc) or FederationUnavailable.DEFAULT
            wf.state     = FAILED
            wf.reason    = stage.reason
            wf.finished_at = self._clock()
            self._changed()
            raise
        except StageFailed as exc:
            if stage.state != CANCELED:
                stage.state = FAILED
            stage.reason = stage.reason or str(exc)
        except FederationCallError as exc:
            log.warning('[atomic_campaign] stage %s/%s: %s (%s)',
                        wf.id, stage.name, exc.reason, exc.detail)
            stage.state  = FAILED
            stage.reason = exc.reason
            stage.detail = exc.detail or None
        except asyncio.CancelledError:
            # an orderly shutdown stamps INTERRUPTED *before* cancelling the
            # driver -- never overwrite a state that is already final
            if stage.state not in TERMINAL_STATES:
                stage.state  = CANCELED
                stage.reason = REASON_STOPPED
            if wf.state not in TERMINAL_STATES:
                wf.state  = CANCELED
                wf.reason = wf.reason or stage.reason
            self._changed()
            raise
        except Exception as exc:                              # noqa: BLE001
            log.exception('[atomic_campaign] stage %s/%s crashed',
                          wf.id, stage.name)
            stage.state  = FAILED
            stage.reason = REASON_INCOMPLETE
            stage.detail = '%s: %s' % (type(exc).__name__, exc)
        finally:
            self._changed()

    # ----------------------------------------------------------------------
    def _finish_workflow(self, wf: WorkflowInstance,
                         failure: Optional[StageRun]) -> None:
        """Roll the stage outcomes up into the workflow's final state."""

        if failure is None:
            wf.state, wf.reason = DONE, None
        elif failure.state == CANCELED:
            wf.state  = CANCELED
            wf.reason = failure.reason
        else:
            wf.state  = FAILED
            wf.reason = 'stage %r: %s' % (failure.name,
                                          failure.reason or REASON_INCOMPLETE)
            wf.detail = failure.detail
        wf.finished_at = self._clock()
        self._changed()

    # ----------------------------------------------------------------------
    async def _run_stage(self, campaign: Campaign, wf: WorkflowInstance,
                         stage: StageRun, produced: Dict[str, bytes],
                         cancel: Optional[asyncio.Event]) -> None:
        """Submit (inputs included), poll, collect, manifest.

        The inputs ride **in** the submit body: with capability class pools
        nobody knows where the task will run until the dispatcher places
        it, so there is no directory to stage into beforehand.  Raises on
        failure.
        """

        cid          = campaign.campaign_id
        stage.task_id = '%s-%s-%s' % (cid, wf.id, stage.name)
        stage.state   = STAGING if stage.inputs else SUBMITTED
        stage.submitted_at = self._clock()
        self._changed()

        inputs = self._encode_inputs(stage, produced)

        task = {'task_id' : stage.task_id,
                'cmd'     : self.resolve_cmd(stage.cmd),
                'inputs'  : list(stage.inputs),
                'outputs' : list(stage.declared_outputs),
                'priority': 0}
        # deliberately no `cwd`: the dispatcher assigns one when it places
        # the task, and reports it back on the first poll
        if inputs:
            task['inputs_b64'] = inputs

        stage.state = SUBMITTED
        resp = await self._fed.submit(task, dict(stage.requirements))
        self._record_submit(stage, resp)
        self._changed()

        try:
            task_dict = await self._poll_task(stage, cancel)
        finally:
            self._changed()

        # collect FIRST -- the pilot may vanish moments after the task ends
        collected = await self._collect(campaign, wf, stage)
        extra: Optional[Dict[str, Any]] = None
        if inputs:
            extra = {'inputs_staged':
                     [{'name': name, 'via': 'submit',
                       'size': len(produced.get(name) or b'')}
                      for name in stage.inputs]}
        self._finish_stage(stage, task_dict)
        try:
            self._store.write_manifest(
                cid, wf.id, stage.name,
                stage_manifest(cid, wf, stage, collected, extra=extra))
        except OSError as exc:
            log.warning('[atomic_campaign] manifest write failed for %s/%s: %s',
                        wf.id, stage.name, exc)

        missing = [c['name'] for c in collected if c['via'] is None]
        if stage.state != DONE:
            raise StageFailed(stage.reason or 'task did not succeed')
        if missing:
            stage.state  = FAILED
            stage.reason = 'output(s) not collected: %s' % ', '.join(missing)
            raise StageFailed(stage.reason)

        # feed the next stage
        for entry in collected:
            data = self._store.read_output(cid, wf.id, stage.name,
                                           entry['name'])
            if data is not None:
                produced[entry['name']] = data

    # ----------------------------------------------------------------------
    @staticmethod
    def _record_submit(stage: StageRun, resp: Dict[str, Any]) -> None:
        """Copy the federation submit response onto the stage record.

        ``resource`` is **advisory** here -- the class pool has several
        members and the binding choice is made at dispatch -- so it is
        recorded to have something on screen straight away and overwritten
        by the first poll that names a member.
        """

        resp = resp or {}
        task = resp.get('task') or {}
        stage.resource       = resp.get('resource')
        stage.member         = resp.get('member')
        stage.cls            = resp.get('class')
        stage.pool           = resp.get('pool')
        stage.dispatcher_sid = resp.get('dispatcher_sid')
        if task.get('cwd'):
            stage.cwd = task['cwd']
        if task.get('task_id'):
            stage.task_id = task['task_id']

    # ----------------------------------------------------------------------
    def _encode_inputs(self, stage: StageRun,
                       produced: Dict[str, bytes]) -> Dict[str, str]:
        """This stage's inputs, base64 encoded, for the submit body.

        The dispatcher spools them and places them wherever the task lands
        (there is exactly one path, executed before the task exists,
        instead of two racing ones executed after it does).  Failing to
        *read* an input is a stage failure with the same on-screen text a
        failed stage-in had; failing to *place* it is the dispatcher's and
        surfaces as a task failure.
        """

        out: Dict[str, str] = {}

        for name in stage.inputs:
            data = produced.get(name)
            if data is None:
                stage.state  = FAILED
                stage.reason = REASON_STAGE_IN
                stage.detail = ('input %r was not produced by an earlier '
                                'stage' % name)
                raise StageFailed(stage.reason)
            try:
                out[name] = base64.b64encode(data).decode('ascii')
            except (TypeError, ValueError) as exc:
                stage.state  = FAILED
                stage.reason = REASON_STAGE_IN
                stage.detail = 'input %r: %s' % (name, exc)
                raise StageFailed(stage.reason) from exc

        return out

    # ----------------------------------------------------------------------
    async def _poll_task(self, stage: StageRun,
                         cancel: Optional[asyncio.Event]) -> Dict[str, Any]:
        """Poll until the task is terminal; 1 s, backing off to 3 s.

        The timeout is counted on the TASK state only -- a pilot that takes a
        minute to boot is normal and must not fail a stage.
        """

        deadline = self._clock() + self._timeout
        interval = self._poll
        task_dict: Dict[str, Any] = {}
        failures = 0

        while True:
            await self._sleep(interval)
            interval = min(interval * 1.5, self._poll_max)

            if cancel is not None and cancel.is_set():
                await self._cancel_task(stage)
                stage.state  = CANCELED
                stage.reason = REASON_STOPPED
                raise StageFailed(stage.reason)

            task_dict, failures = await self._poll_once(stage, failures)

            self._observe(stage, task_dict)

            state = str(task_dict.get('state') or '')
            if state in TASK_TERMINAL:
                return task_dict

            if self._clock() > deadline:
                await self._cancel_task(stage)
                stage.state  = FAILED
                stage.reason = ('stage timed out after %.0f s (last '
                                'state: %s)'
                                % (self._timeout, state or 'unknown'))
                raise StageFailed(stage.reason)

    # ----------------------------------------------------------------------
    async def _poll_once(self, stage: StageRun,
                         failures: int) -> Tuple[Dict[str, Any], int]:
        """One status lookup; returns ``(task dict, consecutive failures)``.

        A 404 fails the stage immediately -- the task is gone and no amount
        of waiting brings it back.  Any other error is transient *until* it
        has happened :data:`MAX_POLL_FAILURES` times in a row; that stops a
        broken federation from burning the whole stage timeout.
        """

        try:
            return await self._fed.task(stage.task_id or '') or {}, 0
        except FederationUnavailable:
            raise
        except TaskNotFound as exc:
            stage.state  = FAILED
            stage.reason = exc.reason
            stage.detail = exc.detail or None
            raise StageFailed(stage.reason) from exc
        except Exception as exc:                              # noqa: BLE001
            failures += 1
            log.info('[atomic_campaign] status of %s unreadable (%d/%d): %s',
                     stage.task_id, failures, MAX_POLL_FAILURES, exc)
            if failures >= MAX_POLL_FAILURES:
                stage.state  = FAILED
                stage.reason = REASON_NO_STATUS
                stage.detail = str(exc)
                raise StageFailed(stage.reason) from exc
            return {}, failures

    # ----------------------------------------------------------------------
    def _observe(self, stage: StageRun, task_dict: Dict[str, Any]) -> None:
        """Fold one task-dict poll into the stage record.

        This is where placement comes from: the class pool's member is
        chosen at dispatch, so the poll -- not the submit -- says where the
        stage runs.  A poll that names no resource leaves the last known
        placement alone: an unplaced task is not a failed one.
        """

        state = str(task_dict.get('state') or '')

        self._observe_placement(stage, task_dict)

        child = task_dict.get('child_endpoint')
        if not child and task_dict.get('pilot_id') and stage.pool:
            # the dispatcher names a pilot's child endpoint
            # '<pool>_<pid>', and '<pool>_<member_id>_<pid>' in a class
            # pool -- this is the fallback for a pilot that finished
            # between two polls, the reported field is the primary path
            if stage.member_id:
                child = '%s_%s_%s' % (stage.pool, stage.member_id,
                                      task_dict['pilot_id'])
            else:
                child = '%s_%s' % (stage.pool, task_dict['pilot_id'])
        if child and child != stage.child_endpoint:
            stage.child_endpoint = child
            self._changed()
        if task_dict.get('cwd'):
            stage.cwd = task_dict['cwd']
        if state == 'RUNNING' and stage.state != RUNNING:
            stage.state = RUNNING
            if stage.started_at is None:
                stage.started_at = self._clock()
            self._changed()

    # ----------------------------------------------------------------------
    def _observe_placement(self, stage: StageRun,
                           task_dict: Dict[str, Any]) -> None:
        """Take resource / member / class off one poll.

        ``member_id`` is authoritative: it is ``'<resource>.<member>'`` and
        the resource name may itself contain dots, so it splits on the
        **last** one.  Anything the poll does not name is left as it was --
        ``resource: null`` happens for a task that has not been dispatched
        (or whose resource left the federation) and must not wipe a
        placement we already knew.
        """

        changed = False
        mid     = task_dict.get('member_id')

        resource = task_dict.get('resource')
        member   = task_dict.get('member')

        if mid:
            # the member id is the authoritative placement: it is what the
            # dispatcher stamped on the task, so its two halves win over
            # any `resource`/`member` the answer also carried
            split_res, _, split_mem = str(mid).rpartition('.')
            resource = split_res or resource or None
            member   = split_mem or member   or None
            if mid != stage.member_id:
                stage.member_id = str(mid)
                changed = True

        for attr, value in (('resource', resource), ('member', member),
                            ('cls', task_dict.get('class'))):
            if value and value != getattr(stage, attr):
                setattr(stage, attr, value)
                changed = True

        if changed:
            self._changed()

    # ----------------------------------------------------------------------
    def _finish_stage(self, stage: StageRun,
                      task_dict: Dict[str, Any]) -> None:
        """Map the terminal task dict onto the stage's outcome."""

        state = str(task_dict.get('state') or '')
        stage.exit_code   = task_dict.get('exit_code')
        stage.finished_at = task_dict.get('finished_at') or self._clock()
        # success == DONE with a zero (or absent) exit code -- 00-overview
        if state == 'DONE' and stage.exit_code in (0, None):
            stage.state  = DONE
            stage.reason = None
        elif state == 'CANCELED':
            stage.state  = CANCELED
            stage.reason = REASON_STOPPED
            stage.detail = task_dict.get('error') or None
        else:
            stage.state  = FAILED
            stage.reason = reason_failed(stage)
            # whatever ORBIT said stays out of the UI, but not out of reach
            stage.detail = (task_dict.get('error')
                            or 'task state %s, exit code %s'
                               % (state or 'unknown', stage.exit_code))

    # ----------------------------------------------------------------------
    async def _cancel_task(self, stage: StageRun) -> None:
        if not stage.dispatcher_sid or not stage.task_id:
            return
        try:
            await self._fed.cancel_task(stage.dispatcher_sid, stage.task_id)
        except Exception as exc:                              # noqa: BLE001
            log.info('[atomic_campaign] cancel of %s failed: %s',
                     stage.task_id, exc)

    # ----------------------------------------------------------------------
    async def _collect(self, campaign: Campaign, wf: WorkflowInstance,
                       stage: StageRun) -> List[Dict[str, Any]]:
        """Collect the declared outputs, immediately, in the documented order.

        1. the pilot's own ``staging`` plugin (works without a shared FS),
        2. the dispatcher's ``stage_out`` (shared FS),
        3. a direct read on the broker host (localhost).
        """

        collected = await self._store.collect(
            campaign.campaign_id, wf.id, stage.name,
            list(stage.declared_outputs), self._sources(stage))
        stage.outputs = collected
        self._changed()
        return collected

    # ----------------------------------------------------------------------
    def _sources(self, stage: StageRun) -> List[Source]:

        async def _pilot(name: str) -> Optional[bytes]:
            if not stage.child_endpoint or not stage.cwd:
                return None
            return await self._fed.staging_get(
                stage.child_endpoint, str(Path(stage.cwd) / name))

        async def _stage_out(name: str) -> Optional[bytes]:
            if not stage.dispatcher_sid or not stage.task_id:
                return None
            return await self._fed.stage_out(stage.dispatcher_sid,
                                             stage.task_id, name)

        async def _local(name: str) -> Optional[bytes]:
            if not stage.cwd:
                return None
            path = Path(stage.cwd) / name
            if not path.is_file():
                return None
            return path.read_bytes()

        return [('pilot_staging', _pilot),
                ('dispatcher_stage_out', _stage_out),
                ('broker_local', _local)]


# --------------------------------------------------------------------------
class CampaignRunner:
    """Drive a whole campaign: workflows concurrently, stages sequentially."""

    def __init__(self, fed: FederationAPI, store: ResultStore, *,
                 max_concurrent_workflows: int = 8,
                 on_change: Optional[Callable[[], None]] = None,
                 **stage_kwargs: Any) -> None:

        self._fed       = fed
        self._store     = store
        self._max       = max(1, int(max_concurrent_workflows))
        self._on_change = on_change
        self._kwargs    = stage_kwargs
        self.cancel_event = asyncio.Event()

    # ----------------------------------------------------------------------
    def cancel(self) -> None:
        """Ask the driver to stop; running stages end as CANCELED."""

        self.cancel_event.set()

    # ----------------------------------------------------------------------
    def make_stage_runner(self) -> StageRunner:
        return StageRunner(self._fed, self._store,
                           on_change=self._on_change, **self._kwargs)

    # ----------------------------------------------------------------------
    async def run(self, campaign: Campaign) -> Campaign:
        """Run every workflow of *campaign*; return it in its final state."""

        sem = asyncio.Semaphore(self._max)

        async def _one(wf: WorkflowInstance) -> None:
            async with sem:
                if self.cancel_event.is_set():
                    self._mark_canceled(wf)
                    return
                await self.make_stage_runner().run_workflow(
                    campaign, wf, self.cancel_event)

        results = await asyncio.gather(
            *[_one(wf) for wf in campaign.workflows], return_exceptions=True)

        for wf, res in zip(campaign.workflows, results):
            if isinstance(res, asyncio.CancelledError):
                self._mark_canceled(wf)
            elif isinstance(res, FederationUnavailable):
                reason = str(res) or FederationUnavailable.DEFAULT
                campaign.reason = campaign.reason or reason
                self._mark_failed(wf, reason)
            elif isinstance(res, BaseException):
                log.exception('[atomic_campaign] workflow %s crashed: %s',
                              wf.id, res)
                self._mark_failed(wf, REASON_INCOMPLETE,
                                  '%s: %s' % (type(res).__name__, res))

        campaign.refresh_state()
        self._changed()
        return campaign

    # ----------------------------------------------------------------------
    def _changed(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception as exc:                              # noqa: BLE001
            log.warning('[atomic_campaign] state persist failed: %s', exc)

    # ----------------------------------------------------------------------
    @staticmethod
    def _mark_failed(wf: WorkflowInstance, reason: str,
                     detail: str = '') -> None:
        if wf.state not in TERMINAL_STATES:
            wf.state = FAILED
        wf.reason = wf.reason or reason
        wf.detail = wf.detail or (detail or None)
        for stage in wf.stages:
            if stage.state == PENDING:
                stage.state  = SKIPPED
                stage.reason = reason

    # ----------------------------------------------------------------------
    @staticmethod
    def _mark_canceled(wf: WorkflowInstance) -> None:
        if wf.state not in TERMINAL_STATES:
            wf.state  = CANCELED
            wf.reason = wf.reason or REASON_STOPPED
        for stage in wf.stages:
            if stage.state not in TERMINAL_STATES:
                stage.state  = CANCELED
                stage.reason = REASON_STOPPED

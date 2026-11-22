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
import concurrent.futures as futures
import logging
import os
import time

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, \
                   Tuple

from .state import Campaign, StageRun, WorkflowInstance
from .state import (CANCELED, DONE, FAILED, INTERRUPTED, PENDING, RUNNING,
                    SKIPPED, STAGING, SUBMITTED, TERMINAL_STATES,
                    FED_DISPATCHER_SID, REASON_INTERRUPTED)
from .store import ResultStore, Source, stage_manifest

log = logging.getLogger('radical.orbit')

# dispatcher task states we treat as terminal (00-overview "Facts")
TASK_TERMINAL = ('DONE', 'FAILED', 'CANCELED')
# dispatcher task states of a task waiting for a pilot
TASK_QUEUED   = ('NEW', 'PENDING', 'QUEUED')

# tools that only exist on PATH inside a pilot whose venv has atomic-wm
# installed; $ATOMIC_TOOL_PREFIX gives them an explicit home (see docs).
_TOOL_PREFIX_ENV = 'ATOMIC_TOOL_PREFIX'

# How long the federation may stay unreachable (transient errors only, see
# :func:`is_transient`) before a stage gives up -- counted from the first
# failed call of an uninterrupted run of failures, reset by every success.
# It must cover a broker restart: the broker comes back, the federation
# replays its state, the pilots reconnect (plans/11 §3.3).
DEFAULT_UNREACHABLE_BUDGET = 300.0

# back-off between two retries of a failed call while within the budget
RETRY_MIN_INTERVAL = 1.0
RETRY_MAX_INTERVAL = 10.0

# how often one cancel is tried in place before it is left to the
# persisted pending-cancel list (drained after the federation is back)
CANCEL_ATTEMPTS = 3


# Every ``reason`` below is shown on screen verbatim, so the set is small,
# fixed and written in the demo's vocabulary.  Whatever ORBIT said goes to
# ``StageRun.detail`` and to the log.
REASON_NO_RESOURCE = 'no resource satisfies the stage requirements'
REASON_NOT_STARTED = 'the stage could not be started'
REASON_NO_STATUS   = 'the stage status could not be read'
REASON_STOPPED     = 'the campaign was stopped'
REASON_CANCELED_RS = 'the stage was cancelled on the resource'
REASON_INCOMPLETE  = 'the stage did not complete'
REASON_STAGE_IN    = 'the input file could not be placed on the resource'
# an empty federation is a different story from one whose resources do not
# match the stage -- and on stage it calls for a different fix
REASON_NO_RESOURCES_JOINED = 'no resources have joined the federation yet'


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


# shielded cancels still running after their caller went away (a strong
# reference, so the loop does not drop them)
_BACKGROUND: 'set[asyncio.Future]' = set()


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

    def __init__(self, reason: str, detail: str = '',
                 status: Optional[int] = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.status = status


# --------------------------------------------------------------------------
class FederationTransient(FederationCallError):
    """A federation call failed for a reason that passes (see
    :func:`is_transient_status`): retried within the unreachable budget."""


# --------------------------------------------------------------------------
class TaskNotFound(FederationCallError):
    """The federation does not know this task (404) -- fail fast, do not
    keep polling for the whole stage timeout."""

    def __init__(self, detail: str = '') -> None:
        super().__init__(REASON_NO_STATUS, detail, status=404)


# --------------------------------------------------------------------------
def is_transient_status(status: int, detail: str = '') -> bool:
    """Whether an HTTP answer from the federation is worth waiting out.

    The ORBIT contract (plans/11 §3.3, §4):

    - 503, with or without Retry-After: the broker's fast-fail for a
      suspect endpoint or a full call table, a replay that is not done;
    - 410 "shutting down": the plugin answers that while the broker stops;
    - 404 "unknown session": the federation's dispatcher session is not
      re-registered yet after a broker restart;
    - any other 5xx: the far side broke, not the request.

    Every other 4xx is an answer, not an outage: a 409 (a known task whose
    class has no members now), any other 404 (the task is gone), a 400.
    Submit, poll, collection and cancel all classify by this one function
    (with :func:`is_transient` on top for transport errors).
    """

    text = str(detail or '').lower()
    if 500 <= status <= 599:
        return True
    if status == 410 and 'shutting down' in text:
        return True
    if status == 404 and 'unknown session' in text:
        return True
    return False


# --------------------------------------------------------------------------
def call_error(status: int, detail: str, reason: str) -> FederationCallError:
    """The exception a failed federation call raises, by its class."""

    detail = detail or 'HTTP %d' % status
    if is_transient_status(status, detail):
        return FederationTransient(reason, detail, status=status)
    if status == 404:
        return TaskNotFound(detail)
    return FederationCallError(reason, detail, status=status)


# --------------------------------------------------------------------------
def describe(exc: BaseException) -> str:
    """``'TimeoutError: ...'`` -- a bare ``TimeoutError()`` has no text."""

    text = str(exc)
    return '%s: %s' % (type(exc).__name__, text) if text \
           else type(exc).__name__


# --------------------------------------------------------------------------
def is_transient(exc: BaseException) -> bool:
    """Transport errors and the transient HTTP answers above.

    A timeout is one whichever module raised it: before Python 3.11
    ``asyncio.TimeoutError`` and ``concurrent.futures.TimeoutError`` are
    not the builtin ``TimeoutError``, so all three are named.
    """

    return isinstance(exc, (FederationTransient, ConnectionError,
                            TimeoutError, asyncio.TimeoutError,
                            futures.TimeoutError))


# --------------------------------------------------------------------------
async def try_cancel(fed: 'FederationAPI', campaign: Campaign,
                     task_id: str, dispatcher_sid: str) -> bool:
    """One cancel call; drop the task from the pending list unless the
    federation is unreachable.  Returns whether the entry is settled.

    A transient failure (:func:`is_transient`) keeps the entry; anything
    else settles it -- a refused or unknown task is nothing left to stop.
    """

    try:
        ok = await fed.cancel_task(dispatcher_sid, task_id)
    except FederationUnavailable:
        return False
    except Exception as exc:                                  # noqa: BLE001
        if is_transient(exc):
            log.info('[atomic_campaign] cancel of %s deferred: %s',
                     task_id, describe(exc))
            return False
        log.info('[atomic_campaign] cancel of %s refused: %s', task_id, exc)
        ok = False
    if not ok:
        log.info('[atomic_campaign] cancel of %s not confirmed -- dropped',
                 task_id)
    campaign.unqueue_cancel(task_id)
    return True


# --------------------------------------------------------------------------
async def drain_pending_cancels(fed: 'FederationAPI',
                                campaign: Campaign) -> int:
    """Try every pending cancel of *campaign* once; return how many settled.

    Stops at the first entry the federation could not take -- it is down
    again, the rest would only fail the same way.
    """

    settled = 0
    for entry in list(campaign.pending_cancels):
        task_id = str(entry.get('task_id') or '')
        if not task_id:
            campaign.unqueue_cancel(task_id)
            continue
        if not await try_cancel(fed, campaign, task_id,
                                str(entry.get('dispatcher_sid')
                                    or FED_DISPATCHER_SID)):
            break
        settled += 1
    return settled


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
        """Dispatcher task cancel.

        ``True`` once the task is cancelled or there is nothing left to
        cancel, ``False`` for a refusal that retrying does not change.
        Raises a transient error (:func:`is_transient`) when the federation
        could not be reached -- the caller then keeps the cancel pending.
        """
        raise NotImplementedError


# --------------------------------------------------------------------------
class StageRunner:
    """Run the stages of one workflow instance, sequentially."""

    def __init__(self, fed: FederationAPI, store: ResultStore, *,
                 poll_interval:     float = 1.0,
                 poll_max_interval: float = 3.0,
                 stage_timeout:     float = 900.0,
                 queue_timeout:     float = 4 * 3600.0,
                 unreachable_budget: float = DEFAULT_UNREACHABLE_BUDGET,
                 tool_prefix:       Optional[str] = None,
                 on_change:         Optional[Callable[[], None]] = None,
                 wake_drainer:      Optional[Callable[[], None]] = None,
                 sleep:             Callable[[float], Any] = asyncio.sleep,
                 clock:             Callable[[], float] = time.time) -> None:

        self._fed          = fed
        self._store        = store
        self._poll         = float(poll_interval)
        self._poll_max     = float(poll_max_interval)
        self._timeout      = float(stage_timeout)
        self._queue_timeout = float(queue_timeout)
        self._budget       = float(unreachable_budget)
        self._tool_prefix  = tool_prefix if tool_prefix is not None \
                             else os.environ.get(_TOOL_PREFIX_ENV)
        self._on_change    = on_change
        # the owner's pending-cancel drainer: the ONE place that settles
        # the list, so N runners never retry the same entry at once
        self._wake_drainer = wake_drainer
        self._sleep        = sleep
        self._clock        = clock
        # task ids whose ORBIT task may exist and is not settled -- not seen
        # terminal, not cancelled (RULE B: such a task must not be left)
        self._live: 'set[str]' = set()

    # ----------------------------------------------------------------------
    def _drain_cancels(self) -> None:
        """Tell the owner's drainer to try the pending cancels now.

        Called when the federation answers again after a failure, and when
        a cancel is left pending.  The runner never drains the list itself.
        """

        if self._wake_drainer is None:
            return
        try:
            self._wake_drainer()
        except Exception as exc:                              # noqa: BLE001
            log.warning('[atomic_campaign] drainer wake failed: %s', exc)

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
                if not stage.is_terminal():           # INTERRUPTED stays
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
            if stage.state not in (CANCELED, INTERRUPTED):
                stage.state = FAILED
            stage.reason = stage.reason or str(exc)
        except FederationCallError as exc:
            log.warning('[atomic_campaign] stage %s/%s: %s (%s)',
                        wf.id, stage.name, exc.reason, exc.detail)
            stage.state  = FAILED
            stage.reason = exc.reason
            stage.detail = exc.detail or None
        except asyncio.CancelledError:
            await self._stage_cancelled(campaign, wf, stage)
            raise
        except Exception as exc:                              # noqa: BLE001
            log.exception('[atomic_campaign] stage %s/%s crashed',
                          wf.id, stage.name)
            stage.state  = FAILED
            stage.reason = REASON_INCOMPLETE
            stage.detail = '%s: %s' % (type(exc).__name__, exc)
        finally:
            self._queue_if_live(campaign, stage)
            self._changed()

    # ----------------------------------------------------------------------
    def _queue_if_live(self, campaign: Campaign, stage: StageRun) -> None:
        """A stage ended FAILED/CANCELED while its task may still run:
        queue a cancel for it.

        Covers every end that did not cancel the task already -- the
        unreachable budget ran out during the submit (it may have landed)
        or the poll, a fatal poll answer, a crash.  The federation is
        likely down then, so nothing is tried here: the owner's drainer
        settles the entry once it answers.  An orderly shutdown never gets
        here -- its stages are INTERRUPTED, not FAILED or CANCELED.
        """

        task_id = stage.task_id
        if not task_id or task_id not in self._live \
                or stage.state not in (FAILED, CANCELED):
            return
        self._live.discard(task_id)
        if campaign.queue_cancel(task_id, stage.dispatcher_sid):
            log.info('[atomic_campaign] cancel of %s queued: stage %s',
                     task_id, stage.state)
        self._drain_cancels()

    # ----------------------------------------------------------------------
    async def _stage_cancelled(self, campaign: Campaign,
                               wf: WorkflowInstance, stage: StageRun) -> None:
        """The driver (or whoever runs this workflow) was cancelled.

        An orderly shutdown stamps INTERRUPTED *before* it cancels the
        driver: then a final state is never overwritten and the task is not
        cancelled here -- a service restart must not kill the work on the
        resource (``mark_interrupted`` queued it for later).  Any other
        cancel (a stopped campaign, a caller cancelling ``run_workflow``)
        must reach the task, or it keeps running unobserved.
        """

        if stage.state not in TERMINAL_STATES:
            if stage.state in (SUBMITTED, RUNNING):
                await self._cancel_task(campaign, stage)
            stage.state  = CANCELED
            stage.reason = REASON_STOPPED
        if wf.state not in TERMINAL_STATES:
            wf.state  = CANCELED
            wf.reason = wf.reason or stage.reason
        self._changed()

    # ----------------------------------------------------------------------
    def _finish_workflow(self, wf: WorkflowInstance,
                         failure: Optional[StageRun]) -> None:
        """Roll the stage outcomes up into the workflow's final state."""

        if failure is None:
            wf.state, wf.reason = DONE, None
        elif failure.state in (CANCELED, INTERRUPTED):
            wf.state  = failure.state
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

        self._gate(campaign, stage)

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
        resp = await self._submit(campaign, stage, task, cancel)
        self._record_submit(stage, resp)
        self._changed()

        try:
            task_dict = await self._poll_task(campaign, stage, cancel)
        finally:
            self._changed()

        # collect FIRST -- the pilot may vanish moments after the task ends
        collected = await self._collect(campaign, wf, stage, cancel)
        extra: Optional[Dict[str, Any]] = None
        if inputs:
            extra = {'inputs_staged':
                     [{'name': name, 'via': 'submit',
                       'size': len(produced.get(name) or b'')}
                      for name in stage.inputs]}
        self._finish_stage(stage, task_dict)

        # the outcome is final BEFORE the manifest records it
        missing = [c['name'] for c in collected if c['via'] is None]
        if stage.state == DONE and missing:
            self._incomplete(stage, missing, cancel)

        try:
            self._store.write_manifest(
                cid, wf.id, stage.name,
                stage_manifest(cid, wf, stage, collected, extra=extra))
        except OSError as exc:
            log.warning('[atomic_campaign] manifest write failed for %s/%s: %s',
                        wf.id, stage.name, exc)

        if stage.state != DONE:
            raise StageFailed(stage.reason or 'task did not succeed')

        # feed the next stage
        for entry in collected:
            data = self._store.read_output(cid, wf.id, stage.name,
                                           entry['name'])
            if data is not None:
                produced[entry['name']] = data

    # ----------------------------------------------------------------------
    async def _submit(self, campaign: Campaign, stage: StageRun,
                      task: Dict[str, Any],
                      cancel: Optional[asyncio.Event]) -> Dict[str, Any]:
        """Submit, retrying transient errors within the unreachable budget.

        Every retry sends the same task id.  By the ORBIT contract (plans/11
        §4) a known task id answers with the existing record, so a submit
        whose answer got lost is not run twice -- but only once ORBIT's
        ``fix/federation-submit-ledger`` is merged.  On ORBIT devel without
        it, ``plugin_federation._route_submit`` picks a class pool again
        without consulting its ledger: a retry after a 5xx that came back
        once the task had landed may land in another pool and create a
        second task with the same id.
        """

        down_since: Optional[float] = None
        interval = RETRY_MIN_INTERVAL
        self._live.add(str(stage.task_id))
        while True:
            # a retry included: the first try may have landed, and
            # mark_interrupted has queued it for the next start
            self._gate(campaign, stage)
            try:
                resp = await self._fed.submit(task, dict(stage.requirements))
            except Exception as exc:                          # noqa: BLE001
                if not is_transient(exc):
                    if down_since is None:
                        # refused at the first try: nothing landed
                        self._live.discard(str(stage.task_id))
                    raise
                now = self._clock()
                if down_since is None:
                    down_since = now
                if now - down_since >= self._budget:
                    raise FederationCallError(
                        REASON_NOT_STARTED,
                        'federation unreachable for %.0f s: %s'
                        % (now - down_since, describe(exc))) from exc
                log.info('[atomic_campaign] submit of %s deferred: %s',
                         stage.task_id, describe(exc))
                await self._sleep(interval)
                interval = min(interval * 2, RETRY_MAX_INTERVAL)
                # the submit may have landed: a stop must cancel it
                await self._stop_if_canceled(campaign, stage, cancel)
                continue
            if down_since is not None:
                self._drain_cancels()             # it answers again
            return resp

    # ----------------------------------------------------------------------
    @staticmethod
    def _gate(campaign: Campaign, stage: StageRun) -> None:
        """RULE A: no stage is ever submitted for an INTERRUPTED campaign.

        The service is stopping (or stopped): a new ORBIT task now would
        be one nobody collects, and cancelling it would be the shutdown
        cancelling ORBIT work.  Checked before every submit, retries
        included, so it holds whoever drives the runner and however late
        the stop lands.
        """

        if campaign.state != INTERRUPTED:
            return
        stage.state  = INTERRUPTED
        stage.reason = stage.reason or REASON_INTERRUPTED
        raise StageFailed(stage.reason)

    # ----------------------------------------------------------------------
    async def _stop_if_canceled(self, campaign: Campaign, stage: StageRun,
                                cancel: Optional[asyncio.Event]) -> None:
        """The campaign was stopped: cancel the task, end the stage."""

        if cancel is None or not cancel.is_set():
            return
        await self._cancel_task(campaign, stage)
        stage.state  = CANCELED
        stage.reason = REASON_STOPPED
        raise StageFailed(stage.reason)

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
    async def _poll_task(self, campaign: Campaign, stage: StageRun,
                         cancel: Optional[asyncio.Event]) -> Dict[str, Any]:
        """Poll until the task is terminal; 1 s, backing off to 3 s.

        The timeout is counted on the TASK state only -- a pilot that takes a
        minute to boot, or waits in the batch queue, is normal and must not
        fail a stage.  The stage deadline therefore starts at the first
        RUNNING poll; until then a separate, much longer queue-wait bound
        applies, so a task that never starts cannot hang its campaign.
        """

        started        = self._clock()
        queue_deadline = started + self._queue_timeout
        # a task flapping between RUNNING and re-queued resets both bounds
        # below; this one it cannot reset
        hard_deadline  = started + self._queue_timeout + self._timeout
        deadline: Optional[float] = None
        interval = self._poll
        task_dict: Dict[str, Any] = {}
        down_since: Optional[float] = None

        while True:
            await self._sleep(interval)
            interval = min(interval * 1.5, self._poll_max)

            await self._stop_if_canceled(campaign, stage, cancel)

            task_dict, down_since = await self._poll_once(stage, down_since)

            self._observe(stage, task_dict)

            state = str(task_dict.get('state') or '')
            if state in TASK_TERMINAL:
                self._live.discard(str(stage.task_id))
                return task_dict

            if self._clock() > hard_deadline:
                await self._cancel_task(campaign, stage)
                stage.state  = FAILED
                stage.reason = ('stage did not complete within %.0f s '
                                '(last state: %s)'
                                % (hard_deadline - started,
                                   state or 'unknown'))
                raise StageFailed(stage.reason)

            if deadline is None and state == 'RUNNING':
                deadline = self._clock() + self._timeout
            elif deadline is not None and state in TASK_QUEUED:
                # back in the queue (its member left, the task was
                # re-queued): waiting again is not running time.  Only an
                # explicit queue state counts -- a failed poll ({}) does not
                deadline       = None
                queue_deadline = self._clock() + self._queue_timeout
                if stage.state == RUNNING:
                    stage.state = SUBMITTED
                    self._changed()

            if deadline is None:
                if self._clock() > queue_deadline:
                    await self._cancel_task(campaign, stage)
                    stage.state  = FAILED
                    stage.reason = ('stage did not start within %.0f s '
                                    '(last state: %s)'
                                    % (self._queue_timeout,
                                       state or 'unknown'))
                    raise StageFailed(stage.reason)
                continue

            if self._clock() > deadline:
                await self._cancel_task(campaign, stage)
                stage.state  = FAILED
                stage.reason = ('stage timed out after %.0f s (last '
                                'state: %s)'
                                % (self._timeout, state or 'unknown'))
                raise StageFailed(stage.reason)

    # ----------------------------------------------------------------------
    async def _poll_once(self, stage: StageRun,
                         down_since: Optional[float]
                         ) -> Tuple[Dict[str, Any], Optional[float]]:
        """One status lookup; returns ``(task dict, unreachable since)``.

        Errors are classified as for the submit (:func:`is_transient`): a
        transport error, a 5xx, a 410 "shutting down" or a 404 "unknown
        session" while the broker replays is waited out for up to the
        unreachable budget, counted from the first failure of an
        uninterrupted run; any success resets it.  Anything else -- a 409
        (the class has no members any more), any other 4xx (a 404: the task
        is gone), an unexpected error -- fails the stage at once, because
        no amount of waiting changes it.  A missing federation is re-raised
        for the campaign to report.
        """

        try:
            task_dict = await self._fed.task(stage.task_id or '') or {}
        except FederationUnavailable:
            raise
        except Exception as exc:                              # noqa: BLE001
            if is_transient(exc):
                return self._poll_failed(stage, down_since, exc)
            if isinstance(exc, TaskNotFound):
                self._live.discard(str(stage.task_id))     # it is gone
            stage.state  = FAILED
            if isinstance(exc, FederationCallError):
                stage.reason = exc.reason
                stage.detail = exc.detail or None
            else:
                stage.reason = REASON_NO_STATUS
                stage.detail = describe(exc)
            raise StageFailed(stage.reason) from exc
        if down_since is not None:
            self._drain_cancels()                 # it answers again
        return task_dict, None

    # ----------------------------------------------------------------------
    def _poll_failed(self, stage: StageRun, down_since: Optional[float],
                     exc: BaseException
                     ) -> Tuple[Dict[str, Any], Optional[float]]:

        now = self._clock()
        if down_since is None:
            down_since = now
        log.info('[atomic_campaign] status of %s unreadable for %.0f s: %s',
                 stage.task_id, now - down_since, describe(exc))
        if now - down_since >= self._budget:
            stage.state  = FAILED
            stage.reason = REASON_NO_STATUS
            stage.detail = ('federation unreachable for %.0f s: %s'
                            % (now - down_since, describe(exc)))
            raise StageFailed(stage.reason) from exc
        return {}, down_since

    # ----------------------------------------------------------------------
    def _observe(self, stage: StageRun, task_dict: Dict[str, Any]) -> None:
        """Fold one task-dict poll into the stage record.

        This is where placement comes from: the class pool's member is
        chosen at dispatch, so the poll -- not the submit -- says where the
        stage runs.  A poll that names no resource leaves the last known
        placement alone: an unplaced task is not a failed one.

        A stage with a final state keeps it: a shutdown stamps INTERRUPTED
        while the runner may still poll, and a stage turned RUNNING again
        would have its ORBIT task cancelled when the runner is cancelled
        (RULE A).
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
        if state == 'RUNNING' and stage.state != RUNNING \
                and stage.state not in TERMINAL_STATES:
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
        """Map the terminal task dict onto the stage's outcome.

        An INTERRUPTED stage keeps that state (RULE A): the stop stamped it
        while the task ran, and its workflow must read INTERRUPTED too.
        """

        state = str(task_dict.get('state') or '')
        stage.exit_code   = task_dict.get('exit_code')
        stage.finished_at = task_dict.get('finished_at') or self._clock()
        if stage.state == INTERRUPTED:
            return
        # success == DONE with a zero (or absent) exit code -- 00-overview
        if state == 'DONE' and stage.exit_code in (0, None):
            stage.state  = DONE
            stage.reason = None
        elif state == 'CANCELED':
            # our own stops never get here (the poll loop raises first):
            # this cancel came from the resource side -- leave, operator
            stage.state  = CANCELED
            stage.reason = REASON_CANCELED_RS
            stage.detail = task_dict.get('error') or None
        else:
            stage.state  = FAILED
            stage.reason = reason_failed(stage)
            # whatever ORBIT said stays out of the UI, but not out of reach
            stage.detail = (task_dict.get('error')
                            or 'task state %s, exit code %s'
                               % (state or 'unknown', stage.exit_code))

    # ----------------------------------------------------------------------
    async def _cancel_task(self, campaign: Campaign,
                           stage: StageRun) -> None:
        """Cancel the stage's task on the federation -- shielded, retried.

        The cancel is put on the campaign's persisted pending-cancel list
        *first*, then tried up to :data:`CANCEL_ATTEMPTS` times.  If the
        federation stays unreachable the entry remains, and the owner's
        drainer settles it once the federation answers again.  The shield
        lets a cancel that is under way finish even when the caller is
        cancelled again.
        """

        if not stage.task_id:
            return
        task_id = stage.task_id
        sid     = stage.dispatcher_sid or FED_DISPATCHER_SID
        self._live.discard(task_id)
        campaign.queue_cancel(task_id, sid)
        self._changed()

        async def _attempts() -> None:
            for attempt in range(CANCEL_ATTEMPTS):
                if not campaign.cancel_pending(task_id):
                    return                        # someone else settled it
                if await try_cancel(self._fed, campaign, task_id, sid):
                    self._changed()
                    return
                if attempt + 1 < CANCEL_ATTEMPTS:
                    await self._sleep(min(self._poll_max * (attempt + 1),
                                          RETRY_MAX_INTERVAL))
            log.info('[atomic_campaign] cancel of %s left pending', task_id)
            self._drain_cancels()

        job = asyncio.ensure_future(_attempts())
        _BACKGROUND.add(job)
        job.add_done_callback(_BACKGROUND.discard)
        try:
            # cancelled again while cancelling: the job carries on, the
            # caller's cancel is honoured
            await asyncio.shield(job)
        except Exception as exc:                              # noqa: BLE001
            log.info('[atomic_campaign] cancel of %s failed: %s',
                     task_id, exc)

    # ----------------------------------------------------------------------
    async def _collect(self, campaign: Campaign, wf: WorkflowInstance,
                       stage: StageRun,
                       cancel: Optional[asyncio.Event] = None
                       ) -> List[Dict[str, Any]]:
        """Collect the declared outputs, immediately, in the documented order.

        1. the pilot's own ``staging`` plugin (works without a shared FS),
        2. the dispatcher's ``stage_out`` (shared FS),
        3. a direct read on the broker host (localhost).

        An output that no source delivered while one of them failed with a
        transient error (:func:`is_transient` -- the broker went away right
        after the task ended) is tried again within the unreachable budget,
        like a poll; any round that collects something resets it.  A
        stopped campaign ends that wait: the task is over, there is nothing
        to cancel, and the campaign must not stay RUNNING for the budget.
        """

        def _stopped() -> bool:
            return cancel is not None and cancel.is_set()

        entries: Dict[str, Dict[str, Any]] = {}
        names    = list(stage.declared_outputs)
        interval = RETRY_MIN_INTERVAL
        down_since: Optional[float] = None
        while names:
            transient: List[BaseException] = []
            for entry in await self._store.collect(
                    campaign.campaign_id, wf.id, stage.name, names,
                    self._sources(stage, transient)):
                entries[entry['name']] = entry
            missing = [n for n in names if entries[n]['via'] is None]
            if not missing or not transient or _stopped():
                break
            now = self._clock()
            if down_since is None or len(missing) < len(names):
                down_since = now
            if now - down_since >= self._budget:
                break
            log.info('[atomic_campaign] outputs of %s deferred: %s',
                     stage.task_id, describe(transient[-1]))
            await self._sleep(interval)
            interval = min(interval * 2, RETRY_MAX_INTERVAL)
            names = missing
            if _stopped():
                break

        stage.outputs = [entries[n] for n in stage.declared_outputs
                         if n in entries]
        self._changed()
        return stage.outputs

    # ----------------------------------------------------------------------
    @staticmethod
    def _incomplete(stage: StageRun, missing: List[str],
                    cancel: Optional[asyncio.Event]) -> None:
        """A DONE task whose outputs were not all collected: the stage
        failed -- or was stopped, when the campaign stopped meanwhile."""

        if cancel is not None and cancel.is_set():
            stage.state  = CANCELED
            stage.reason = REASON_STOPPED
            stage.detail = 'output(s) not collected: %s' % ', '.join(missing)
            return
        stage.state  = FAILED
        stage.reason = 'output(s) not collected: %s' % ', '.join(missing)

    # ----------------------------------------------------------------------
    def _sources(self, stage: StageRun,
                 transient: List[BaseException]) -> List[Source]:
        """The collection sources; *transient* gathers their outages."""

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

        return [_noting(('pilot_staging', _pilot), transient),
                _noting(('dispatcher_stage_out', _stage_out), transient),
                ('broker_local', _local)]


# --------------------------------------------------------------------------
def _noting(source: Source, transient: List[BaseException]) -> Source:
    """*source*, recording its transient errors in *transient* (it still
    raises them -- the store records every error per source)."""

    label, call = source

    async def _fetch(name: str) -> Optional[bytes]:
        try:
            return await call(name)
        except Exception as exc:                              # noqa: BLE001
            if is_transient(exc):
                transient.append(exc)
            raise
    return label, _fetch


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

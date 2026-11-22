"""AuCaM adapter: plan and steer an ATOMIC campaign with AuCaM (plan 11, 3.2).

AuCaM (Masha Goliyad's Campaign Manager, ``src.aucam``, pinned to devel @
6842888) decides *what* runs: which frames start, which are cancelled, when
the campaign has reached its goal or spent its budget.  ATOMIC's
:class:`~atomic_wm.campaign.runner.StageRunner` still decides *how* a frame
runs: stage by stage through the federation.  The seam between the two:

- **One** ``CampaignManager`` **per ATOMIC campaign** (AuCaM runs one
  campaign per ``run()``).  Several drivers may run at once in one process;
  they can share one RHAPSODY backend (:func:`make_backend`).  The adapter
  never relies on AuCaM's signal handlers -- :meth:`AucamCampaignDriver.cancel`
  calls ``CampaignManager.stop()``.
- **A frame is one AuCaM Python workflow** (type ``frame``).  Its block
  creates an ATOMIC :class:`~atomic_wm.campaign.state.WorkflowInstance`
  whose id is unique per AuCaM *launch* -- ``f<frame>-<8 hex of the AuCaM
  wf_id>`` -- so a relaunched frame never reuses an ORBIT task id
  (``<cid>-<wf.id>-<stage>``: the dispatcher would answer a known DONE id
  with the cached result).  The instance is appended to
  ``campaign.workflows``, so the Explorer shows every launch like a sweep
  instance, and the block awaits ``StageRunner.run_workflow``.
- **Reports**: after every state change of its stages (and at least every
  ``report_interval_s``) the block emits ``score`` (``None`` until the
  score stage is collected) and running totals ``node_hours`` /
  ``cpu_hours``, counted from the first RUNNING poll of each stage to its
  end.  Batch-queue wait before that poll is not charged.  Charged: a
  broker outage while the stage runs (the stage stays RUNNING, as the
  resource would bill it), and the time a stage spends re-queued after it
  ran (``started_at`` is kept, so ``spent`` runs on).  Resetting
  ``started_at`` on a re-queue would drop the time already run instead --
  charging the wait is the lesser error, and ``frame_node_hours`` /
  ``max_wall_time_s`` should leave room for it.
- **Cancel**: when AuCaM cancels a frame, the block first sets the
  runner's own stop event (the runner cancels the ORBIT task at its next
  poll -- that works with today's runner) and waits up to
  ``cancel_wait_s``; a runner that does not come back by then (a hung
  submit or poll) is cancelled as a task, which relies on the track-A
  contract "a cancelled ``run_workflow`` cancels its ORBIT task".
- :class:`BigsimController` puts the per-frame scores into ``obs``
  (``good_frames``, ``best_score``, ``frame_scores``: AuCaM's goals read
  top-level ``obs`` keys), cancels frames whose score is poor, relaunches a
  frame AuCaM cancelled at its own node-hour limit (or that failed) up to
  ``max_attempts``, stops at the goal (N good frames) and lets AuCaM stop
  at the budget.  Its summary lands in the outcome.

Plugin-facing interface
-----------------------

The campaign submit body selects the planner::

    {"name": ..., "planner": "sweep" | "aucam",       # default "sweep"
     "workflow": <frame template, as for the sweep>,
     "sweep": {...},                                  # frames, if no list
     "aucam": {<parameters below>}}

``workflow`` is the same spec the sweep planner takes; every frame is one
copy of it with the frame's parameters substituted.  Frames come from
``aucam.frames`` (a list of parameter dicts) or, without it, from the sweep
points.  ``aucam`` keys (all optional except where noted):

=====================  =====================================================
``frames``             list of ``{param: value}``; ``frame`` = index if absent
``score``              ``{"stage", "file", "key"}``: where the score is read
                       (a JSON output of a stage; dotted key).  Default: the
                       last stage, its first output, ``summary.score``
``direction``          ``maximize`` (default) or ``minimize``
``good_score``         a completed frame at least this good is *good*
                       (required when ``goal_good_frames`` is set)
``poor_score``         a running frame worse than this is cancelled
``goal_good_frames``   end the campaign after this many good frames
``budget_node_hours``  campaign spending limit (AuCaM ``spending``)
``frame_node_hours``   per-frame starting limit (default 1.0); AuCaM
                       learns it after 3 successful frames (``learn``)
``learn``              default true
``resources``          per frame, e.g. ``{"cpus": 4}``; default: the largest
                       stage ``cores``
``max_concurrent``     frames running at once (default 4)
``capacity``           default ``max_concurrent`` x ``resources``
``max_attempts``       launches per frame (default 2: one relaunch)
``nodes_per_stage``    nodes one stage task holds (default 1)
``node_hour_scale``    multiply measured node-hours (default 1; demo knob)
``policy_interval_s``  AuCaM policy cycle (default 5)
``report_interval_s``  longest gap between two reports (default 10)
``cancel_wait_s``      how long a cancelled frame waits for its runner (10)
``max_wall_time_s``    AuCaM wall-time cancel (default none: queue waits and
                       broker outages must not cancel a frame)
``stale_timeout_s``    AuCaM stale-reporter cancel (default none)
``idle_exit_s``        AuCaM idle exit, seconds or ``auto`` (the default)
=====================  =====================================================

The plugin calls, in this order::

    plan   = parse_aucam_request(body)            # PlannerError -> 400
    camp   = Campaign(campaign_id=..., name=..., workflows=[])
    driver = AucamCampaignDriver(fed, store, plan, on_change=persist,
                                 backend=shared_backend, **stage_kwargs)
    await driver.run(camp)    # returns camp in its final state
    driver.outcome            # the summary dict
    driver.cancel()           # from the cancel route; CampaignManager.stop()

``driver`` has the same ``run(campaign)`` / ``cancel()`` shape as
:class:`~atomic_wm.campaign.runner.CampaignRunner`, so the plugin's
``_drive`` / ``_route_cancel`` work unchanged.

Shutdown never cancels an ORBIT task: a broker or CM restart must not kill
HPC work.  The plugin stamps the campaign INTERRUPTED, calls
:meth:`AucamCampaignDriver.interrupt` (every frame's runner task is
cancelled at once), then cancels the task that awaits ``run()``, and AuCaM
stops.  Three things keep that stop away from ORBIT, each pinned by a test:

- the runner never moves a stage with a final state (INTERRUPTED) on a
  poll, and its cancellation leaves such a stage's task alone;
- ``interrupt()`` right after the stamp: a runner whose stage ends in the
  window before AuCaM's shutdown reaches its frame would otherwise start
  the next stage -- a new ORBIT task during a shutdown;
- a frame stopped on an INTERRUPTED campaign does not set the runner's
  stop event (the runner would cancel at its next poll) and waits only
  ``SHUTDOWN_WAIT_S`` for it, as does the final drain of the runners, so
  a shutdown stays within ORBIT's host-stop bounds.

ORBIT tasks are cancelled by :meth:`AucamCampaignDriver.cancel`, by
AuCaM's own cancels of a frame (a poor score, the frame's node-hour limit,
a wall-time or stale-report limit), by a stage timeout and by a definitive
stage failure -- never by a shutdown.

This module imports without AuCaM; :data:`AUCAM_ERROR` says why it is not
usable, and :func:`parse_aucam_request` / the driver raise
:class:`AucamUnavailable` then.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from .planner import PlannerError, SweepPlanner, validate_sweep, \
                     validate_workflow
from .runner import FederationAPI, FederationUnavailable, StageRunner
from .state import (CANCELED, DONE, FAILED, INTERRUPTED, PENDING, RUNNING,
                    TERMINAL_STATES, Campaign, WorkflowInstance)
from .store import ResultStore

try:                                                      # pragma: no cover
    from src.aucam import (CampaignBudget, CampaignController,
                           CampaignDecision, CampaignManager, CampaignSpec,
                           FIFOSchedulingPolicy, Goal, LaunchPriority,
                           LaunchRequest, PolicyResult, WorkflowConfig,
                           WorkflowResult)
    AUCAM_ERROR: Optional[BaseException] = None
except ImportError as _exc:                               # pragma: no cover
    AUCAM_ERROR = _exc
    CampaignController = object                           # type: ignore

log = logging.getLogger('radical.orbit')

PLANNERS      = ('sweep', 'aucam')
WORKFLOW_TYPE = 'frame'

# how long a shutdown waits for a cancelled runner task to unwind (it only
# has to persist; nothing is called on the federation)
SHUTDOWN_WAIT_S = 1.0

# on-screen reasons (demo vocabulary, see runner.py)
REASON_POOR      = 'cancelled by the campaign manager: score %s is worse ' \
                   'than %s'
REASON_LIMIT     = 'cancelled by the campaign manager: over its node-hour ' \
                   'limit'
REASON_CM        = 'cancelled by the campaign manager'
REASON_GOAL      = 'the campaign reached its goal'
REASON_BUDGET    = 'the campaign budget is used up'
REASON_STOPPED   = 'the campaign was stopped'
REASON_ALL_RAN   = 'every frame has run'

# AuCaM exit reason -> what a frame still running at the end was told
_END_REASONS = {'goals_satisfied'     : REASON_GOAL,
                'spending_limit'      : REASON_BUDGET,
                'controller_terminate': REASON_ALL_RAN,
                'campaign_idle'       : REASON_ALL_RAN,
                'signal'              : REASON_STOPPED}


# --------------------------------------------------------------------------
class AucamUnavailable(PlannerError):
    """AuCaM is not importable here (``src.aucam``, Python >= 3.11)."""


def require_aucam() -> None:
    if AUCAM_ERROR is not None:
        raise AucamUnavailable('the AuCaM planner is not available on this '
                               'service (%s)' % AUCAM_ERROR)


# --------------------------------------------------------------------------
def planner_of(body: Dict[str, Any]) -> str:
    """``sweep`` or ``aucam`` -- the submit body's ``planner`` (default
    ``sweep``); anything else is a :class:`PlannerError` (-> 400)."""

    name = (body or {}).get('planner') or 'sweep'
    if name not in PLANNERS:
        raise PlannerError('unknown planner %r (known: %s)'
                           % (name, ', '.join(PLANNERS)))
    return str(name)


# --------------------------------------------------------------------------
@dataclass
class AucamPlan:
    """A validated ``planner: "aucam"`` request (see the module docstring)."""

    workflow:          Dict[str, Any]
    frames:            List[Dict[str, Any]]
    score_stage:       str
    score_file:        str
    score_key:         str             = 'summary.score'
    direction:         str             = 'maximize'
    good_score:        Optional[float] = None
    poor_score:        Optional[float] = None
    goal_good_frames:  Optional[int]   = None
    budget_node_hours: Optional[float] = None
    frame_node_hours:  float           = 1.0
    learn:             bool            = True
    resources:         Dict[str, float] = field(default_factory=dict)
    max_concurrent:    int             = 4
    capacity:          Dict[str, float] = field(default_factory=dict)
    max_attempts:      int             = 2
    nodes_per_stage:   float           = 1.0
    node_hour_scale:   float           = 1.0
    policy_interval_s: float           = 5.0
    report_interval_s: float           = 10.0
    cancel_wait_s:     float           = 10.0
    max_wall_time_s:   Optional[float] = None
    stale_timeout_s:   Optional[float] = None
    idle_exit_s:       Any             = 'auto'

    # ----------------------------------------------------------------------
    def better(self, a: float, b: float) -> bool:
        """``a`` at least as good as ``b`` in this plan's direction."""

        return a >= b if self.direction == 'maximize' else a <= b

    def is_good(self, score: Any) -> bool:
        return _is_num(score) and self.good_score is not None \
            and self.better(float(score), self.good_score)

    def is_poor(self, score: Any) -> bool:
        return _is_num(score) and self.poor_score is not None \
            and not self.better(float(score), self.poor_score)

    # ----------------------------------------------------------------------
    def instance(self, frame: Dict[str, Any], wf_id: str,
                 attempt: int = 1) -> WorkflowInstance:
        """The ATOMIC instance of one AuCaM launch of *frame*."""

        params = dict(frame)
        sweep  = {k: [v] for k, v in params.items()}
        inst   = SweepPlanner().expand(self.workflow, sweep)[0]
        inst.id = launch_id(int(frame['frame']), wf_id)
        inst.params['attempt'] = attempt
        return inst


# --------------------------------------------------------------------------
def launch_id(frame: int, aucam_wf_id: str) -> str:
    """``f<frame>-<8 hex>`` -- unique per AuCaM launch, safe in a task id.

    AuCaM's wf_id is ``<campaign>.<type>.<uuid4 hex[:8]>``; its last 8
    alphanumerics make the launch unique without dragging the campaign
    name (already the task id's prefix) into the id twice.
    """

    tail = re.sub(r'[^A-Za-z0-9]', '', aucam_wf_id or '')[-8:] or 'x'
    return 'f%03d-%s' % (frame, tail)


# --------------------------------------------------------------------------
def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _number(raw: Dict[str, Any], key: str, default: Any, *,
            minimum: float = 0.0, strict: bool = True,
            integer: bool = False) -> Any:
    """Read one number of the ``aucam`` section (400 on a bad one)."""

    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if not _is_num(value) or (integer and int(value) != value):
        raise PlannerError('aucam.%s must be a finite %s'
                           % (key, 'integer' if integer else 'number'))
    if value < minimum or (strict and value == minimum):
        raise PlannerError('aucam.%s must be %s %g'
                           % (key, '>' if strict else '>=', minimum))
    return int(value) if integer else float(value)


def _resources(raw: Any, what: str) -> Dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise PlannerError('aucam.%s must be an object like {"cpus": 4}'
                           % what)
    out = {}
    for key, value in raw.items():
        if key not in ('cpus', 'gpus'):
            raise PlannerError('aucam.%s: unknown resource %r (cpus, gpus)'
                               % (what, key))
        if not _is_num(value) or value < 0:
            raise PlannerError('aucam.%s.%s must be a number >= 0'
                               % (what, key))
        out[key] = float(value)
    if not any(v > 0 for v in out.values()):
        raise PlannerError('aucam.%s needs cpus or gpus > 0' % what)
    return out


# --------------------------------------------------------------------------
def parse_aucam_request(body: Dict[str, Any]) -> AucamPlan:
    """Validate a ``planner: "aucam"`` submit body; raise on a bad one.

    Everything that can be checked before a campaign exists is checked
    here, so a bad request is a 400 and never a half-run campaign: the
    workflow template, every frame's substitution, the score location and
    the numbers.  Raises :class:`AucamUnavailable` (a
    :class:`PlannerError`) when AuCaM is not importable.
    """

    require_aucam()
    body     = body or {}
    workflow = body.get('workflow')
    validate_workflow(workflow)
    raw = body.get('aucam') or {}
    if not isinstance(raw, dict):
        raise PlannerError('"aucam" must be an object')

    frames = _frames(raw.get('frames'), body.get('sweep'))

    stages = workflow['stages']
    s_stage, s_file, s_key = _score_location(raw.get('score'), stages)

    direction = raw.get('direction') or 'maximize'
    if direction not in ('maximize', 'minimize'):
        raise PlannerError('aucam.direction must be maximize or minimize')

    def _score(key: str) -> Optional[float]:
        val = raw.get(key)
        if val is None:
            return None
        if not _is_num(val):
            raise PlannerError('aucam.%s must be a finite number' % key)
        return float(val)

    cores = max(int((s.get('requirements') or {}).get('cores') or 1)
                for s in stages)
    resources = _resources(raw['resources'], 'resources') \
        if raw.get('resources') is not None else {'cpus': float(cores)}
    max_conc  = _number(raw, 'max_concurrent', 4, minimum=1, strict=False,
                        integer=True)
    capacity  = _resources(raw['capacity'], 'capacity') \
        if raw.get('capacity') is not None \
        else {k: v * max_conc for k, v in resources.items()}

    plan = AucamPlan(
        workflow          = workflow,
        frames            = frames,
        score_stage       = s_stage,
        score_file        = s_file,
        score_key         = s_key,
        direction         = direction,
        good_score        = _score('good_score'),
        poor_score        = _score('poor_score'),
        goal_good_frames  = _number(raw, 'goal_good_frames', None,
                                    minimum=1, strict=False, integer=True),
        budget_node_hours = _number(raw, 'budget_node_hours', None),
        frame_node_hours  = _number(raw, 'frame_node_hours', 1.0),
        learn             = bool(raw.get('learn', True)),
        resources         = resources,
        max_concurrent    = max_conc,
        capacity          = capacity,
        max_attempts      = _number(raw, 'max_attempts', 2, minimum=1,
                                    strict=False, integer=True),
        nodes_per_stage   = _number(raw, 'nodes_per_stage', 1.0),
        node_hour_scale   = _number(raw, 'node_hour_scale', 1.0),
        policy_interval_s = _number(raw, 'policy_interval_s', 5.0),
        report_interval_s = _number(raw, 'report_interval_s', 10.0),
        cancel_wait_s     = _number(raw, 'cancel_wait_s', 10.0),
        max_wall_time_s   = _number(raw, 'max_wall_time_s', None),
        stale_timeout_s   = _number(raw, 'stale_timeout_s', None),
        idle_exit_s       = 'auto' if raw.get('idle_exit_s') == 'auto'
                            else _number(raw, 'idle_exit_s', 'auto',
                                         strict=False))

    if plan.goal_good_frames is not None and plan.good_score is None:
        raise PlannerError('aucam.goal_good_frames needs aucam.good_score')

    # every frame must expand -- a missing parameter is a 400 now, not a
    # failed frame later
    for frame in plan.frames:
        plan.instance(frame, 'check')
    return plan


# --------------------------------------------------------------------------
def _score_location(score: Any, stages: List[Dict[str, Any]]
                    ) -> Tuple[str, str, str]:
    """``(stage, file, key)`` of the score; default: the last stage's
    first output, key ``summary.score``."""

    score = score or {}
    if not isinstance(score, dict):
        raise PlannerError('aucam.score must be {"stage", "file", "key"}')
    names   = [s['name'] for s in stages]
    s_stage = score.get('stage') or names[-1]
    if s_stage not in names:
        raise PlannerError('aucam.score.stage %r is not a stage (%s)'
                           % (s_stage, ', '.join(names)))
    outputs = list(stages[names.index(s_stage)].get('outputs') or [])
    s_file  = score.get('file') or (outputs[0] if outputs else None)
    if not s_file or s_file not in outputs:
        raise PlannerError('aucam.score.file must be an output of stage %r'
                           % s_stage)
    return s_stage, s_file, str(score.get('key') or 'summary.score')


# --------------------------------------------------------------------------
def _frames(raw: Any, sweep: Any) -> List[Dict[str, Any]]:
    """The frames' parameter dicts, each with an int ``frame`` index."""

    if raw is None:
        points = validate_sweep(sweep)
        raw = SweepPlanner._points(points) if points else None
    if not isinstance(raw, (list, tuple)) or not raw:
        raise PlannerError('the aucam planner needs frames: aucam.frames '
                           '(a list of parameter objects) or a sweep')
    out:  List[Dict[str, Any]] = []
    seen: Set[int] = set()
    for idx, frame in enumerate(raw):
        if not isinstance(frame, dict):
            raise PlannerError('aucam.frames[%d] must be an object' % idx)
        frame = dict(frame)
        frame.setdefault('frame', idx)
        if not _is_num(frame['frame']) or int(frame['frame']) != \
                frame['frame'] or frame['frame'] < 0:
            raise PlannerError('aucam.frames[%d].frame must be an integer '
                               '>= 0' % idx)
        frame['frame'] = int(frame['frame'])
        if frame['frame'] in seen:
            raise PlannerError('duplicate frame %d' % frame['frame'])
        seen.add(frame['frame'])
        out.append(frame)
    return out


# --------------------------------------------------------------------------
class BigsimController(CampaignController):               # type: ignore
    """Steer frames: launch them, cancel poor ones, stop at the goal.

    Bookkeeping is per **frame**, not per AuCaM launch: a frame may be
    launched again (``max_attempts``) after AuCaM cancelled it at its own
    node-hour limit, or after it failed.  A frame cancelled for a poor
    score is never relaunched -- the score is a property of the frame.
    """

    def __init__(self, plan: AucamPlan, *,
                 stop_requested: Callable[[], bool] = lambda: False) -> None:

        require_aucam()
        super().__init__()
        self._plan   = plan
        self._stop   = stop_requested
        self._type   = WORKFLOW_TYPE
        self._todo:  Deque[Tuple[int, int]] = deque(
            (int(f['frame']), 1) for f in plan.frames)
        self._requested: Dict[int, int] = {}     # frame -> attempt, queued
        self._live:      Dict[str, int] = {}     # AuCaM wf_id -> frame
        # AuCaM wf_id -> on-screen reason the CONTROLLER cancelled it for
        self.why:        Dict[str, str] = {}
        self.frames: Dict[int, Dict[str, Any]] = {
            int(f['frame']): {'frame': int(f['frame']), 'state': PENDING,
                              'score': None, 'attempts': 0,
                              'launches': []}
            for f in plan.frames}

    # -- bookkeeping, called by the driver --------------------------------
    def launched(self, wf_id: str, frame: int, atomic_id: str) -> None:
        """A block of *frame* started (AuCaM launched the request)."""

        self._requested.pop(frame, None)
        self._live[wf_id] = frame
        rec = self.frames[frame]
        rec['state']     = RUNNING
        rec['attempts'] += 1
        rec['launches'].append(atomic_id)

    def good_frames(self) -> List[int]:
        return [f for f, r in self.frames.items() if r['state'] == 'GOOD']

    # -- AuCaM hooks -------------------------------------------------------
    def goals(self) -> list:
        if self._plan.goal_good_frames is None:
            return []
        return [Goal(name='good_frames', metric='good_frames',
                     threshold=float(self._plan.goal_good_frames),
                     direction='maximize')]

    def is_goal_reached(self) -> bool:
        n = self._plan.goal_good_frames
        return n is not None and len(self.good_frames()) >= n

    def observe(self, queue_snapshot, budget, running=None, **kwargs):
        obs   = super().observe(queue_snapshot, budget, running, **kwargs)
        live  = obs.get('live_reports') or {}
        scores = {wf_id: rep.score for wf_id, rep in live.items()
                  if _is_num(rep.score)}
        known = [r['score'] for r in self.frames.values()
                 if _is_num(r['score'])] + list(scores.values())
        best  = None
        for s in known:
            if best is None or self._plan.better(s, best):
                best = s
        # top-level keys: AuCaM's goals read obs[goal.metric]
        obs['good_frames']  = len(self.good_frames())
        obs['best_score']   = best
        obs['frame_scores'] = scores
        return obs

    def decide(self, obs: dict) -> PolicyResult:
        plan  = self._plan
        types = obs.get('wf_types') or {}
        decisions: Dict[str, Any] = {}

        for wf_id, rep in (obs.get('live_reports') or {}).items():
            if types.get(wf_id) != self._type or not plan.is_poor(rep.score):
                continue
            decisions[wf_id] = CampaignDecision.CANCEL
            self.why[wf_id]  = REASON_POOR % (_fmt(rep.score),
                                              _fmt(plan.poor_score))
            frame = self._live.get(wf_id)
            if frame is not None:
                self.frames[frame]['score'] = float(rep.score)

        if self._stop():
            return PolicyResult(decisions=decisions, terminate=True)

        running = int((obs.get('running') or {}).get(self._type, 0))
        queued  = int(obs['queue_snapshot'].by_type.get(self._type, 0))
        self._forget_dropped(queued, set(types))

        requests = []
        room = plan.max_concurrent - running - queued
        while room > 0 and self._todo and not self.is_goal_reached():
            frame, attempt = self._todo.popleft()
            self._requested[frame] = attempt
            self.frames[frame]['state'] = 'QUEUED'
            requests.append(LaunchRequest.make(
                self._type,
                LaunchPriority.HIGH if attempt > 1 else LaunchPriority.NORMAL,
                context={'frame': frame, 'attempt': attempt}))
            room -= 1

        finished = not self._todo and not self._requested and not requests \
            and running == 0 and queued == 0
        return PolicyResult(decisions=decisions, launch_requests=requests,
                            terminate=finished or self.is_goal_reached())

    def _forget_dropped(self, queued: int, active: Set[str]) -> None:
        """Requests AuCaM dropped (they did not fit the budget) never start.

        Only decidable when nothing is queued and every active workflow has
        claimed its frame (a launched block claims it as it starts).
        """

        if queued or not self._requested or (active - set(self._live)):
            return
        for frame in list(self._requested):
            self._requested.pop(frame)
            self.frames[frame]['state'] = 'DROPPED'

    def on_complete(self, result: WorkflowResult) -> None:
        super().on_complete(result)
        frame = self._live.pop(result.workflow_id, None)
        if frame is None:
            return
        rec   = self.frames[frame]
        score = result.final_score
        if _is_num(score):
            rec['score'] = float(score)
        if result.returncode == 0:
            rec['state'] = 'GOOD' if self._plan.is_good(score) else 'DONE'
        elif result.returncode == -1 and result.workflow_id in self.why:
            rec['state'] = 'POOR'
        elif result.returncode == -1 and not (result.over_budget
                                              or result.over_resources):
            rec['state'] = CANCELED
        else:
            if result.returncode == -1:
                self.why[result.workflow_id] = REASON_LIMIT
            rec['state'] = 'LIMIT' if result.returncode == -1 else FAILED
            if rec['attempts'] < self._plan.max_attempts:
                rec['state'] = 'RETRY'
                self._todo.appendleft((frame, rec['attempts'] + 1))

    def get_summary(self) -> dict:
        counts: Dict[str, int] = {}
        for rec in self.frames.values():
            counts[rec['state']] = counts.get(rec['state'], 0) + 1
        best = None
        for rec in self.frames.values():
            if _is_num(rec['score']) and rec['state'] in ('GOOD', 'DONE') \
                    and (best is None
                         or self._plan.better(rec['score'], best['score'])):
                best = {'frame': rec['frame'], 'score': rec['score'],
                        'workflow': rec['launches'][-1]
                        if rec['launches'] else None}
        return {'frames'     : len(self.frames),
                'good'       : sorted(self.good_frames()),
                'states'     : counts,
                'best'       : best,
                'launches'   : sum(len(r['launches'])
                                   for r in self.frames.values()),
                'relaunches' : sum(max(0, len(r['launches']) - 1)
                                   for r in self.frames.values()),
                'per_frame'  : [dict(r, launches=list(r['launches']))
                                for _, r in sorted(self.frames.items())]}


def _fmt(value: Any) -> str:
    return '%.3g' % value if _is_num(value) else str(value)


# --------------------------------------------------------------------------
async def make_backend() -> Any:
    """One local RHAPSODY backend for every driver of this process.

    A frame's block runs in the CM's event loop and submits no RHAPSODY
    task (its work goes through the federation), so the backend only
    satisfies AsyncFlow's engine.  Note: a RHAPSODY backend keeps **one**
    task callback, which each new ``WorkflowEngine`` overwrites -- harmless
    here, but a block that did submit ``@flow`` tasks must not share it.
    """

    require_aucam()
    from rhapsody.backends.execution.concurrent import \
        ConcurrentExecutionBackend
    return await ConcurrentExecutionBackend()


# --------------------------------------------------------------------------
class AucamCampaignDriver:
    """Run ONE ATOMIC campaign with ONE AuCaM ``CampaignManager``."""

    def __init__(self, fed: FederationAPI, store: ResultStore,
                 plan: AucamPlan, *,
                 on_change: Optional[Callable[[], None]] = None,
                 backend: Any = None,
                 **stage_kwargs: Any) -> None:

        require_aucam()
        self._fed        = fed
        self._store      = store
        self._plan       = plan
        self._on_change  = on_change
        self._backend    = backend
        self._stage_kw   = stage_kwargs
        self._clock: Callable[[], float] = stage_kwargs.get('clock',
                                                            time.time)
        self._frames     = {int(f['frame']): f for f in plan.frames}
        self._stop_req   = False
        self._fatal: Optional[BaseException] = None
        self._inner: Set[asyncio.Future] = set()
        self._scores: Dict[str, Optional[float]] = {}

        self.controller  = BigsimController(
            plan, stop_requested=lambda: self._stop_req)
        self.manager     = CampaignManager(backend=backend)
        self.launches: Dict[str, WorkflowInstance] = {}   # AuCaM id -> inst
        self.outcome: Optional[Dict[str, Any]] = None

    # ----------------------------------------------------------------------
    def cancel(self) -> None:
        """Stop the campaign: AuCaM cancels every running frame."""

        self._stop_req = True
        self.manager.stop()

    # ----------------------------------------------------------------------
    def interrupt(self) -> None:
        """The service stops (the campaign is stamped INTERRUPTED): cancel
        every frame's runner task NOW.

        The runners are tasks of their own, outside the task that awaits
        :meth:`run`.  Left alone until AuCaM's shutdown reaches their frame
        they keep polling, and a stage that ends meanwhile starts the next
        one -- a new ORBIT task during a shutdown.  Cancelled here,
        synchronously right after the stamp, they see the stamp and leave
        ORBIT alone.  The owner calls this; :meth:`run` does not, since by
        the time it sees its cancellation AuCaM has stopped every frame.
        """

        for task in self._inner:
            task.cancel()

    # ----------------------------------------------------------------------
    def _changed(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception as exc:                              # noqa: BLE001
            log.warning('[atomic_campaign] state persist failed: %s', exc)

    # ----------------------------------------------------------------------
    def spec(self, campaign: Campaign) -> Any:
        """The AuCaM ``CampaignSpec`` of *campaign*."""

        plan = self._plan

        async def frame(reporter: Any, context: dict) -> None:
            await self._run_frame(campaign, reporter, context)

        wfc = WorkflowConfig(
            name            = WORKFLOW_TYPE,
            block           = frame,
            spending_limit  = {'node_hours': plan.frame_node_hours},
            resources       = dict(plan.resources),
            learn           = plan.learn,
            launch_on_start = False,          # the controller names frames
            max_wall_time_s = plan.max_wall_time_s,
            stale_timeout_s = plan.stale_timeout_s)

        spending = {} if plan.budget_node_hours is None \
            else {'node_hours': plan.budget_node_hours}
        return CampaignSpec(
            name       = campaign.campaign_id,
            budget     = CampaignBudget(
                max_concurrent_workflows     = plan.max_concurrent,
                max_concurrency_per_workflow = plan.max_concurrent,
                capacity                     = dict(plan.capacity),
                spending                     = spending),
            controller = self.controller,
            workflows  = [wfc],
            policy_interval_s = plan.policy_interval_s,
            scheduling_policy = FIFOSchedulingPolicy(),
            idle_exit_s       = plan.idle_exit_s)

    # ----------------------------------------------------------------------
    async def run(self, campaign: Campaign) -> Campaign:
        """Run *campaign* to its end; return it in its final state."""

        spec = self.spec(campaign)
        try:
            out = await self.manager.run(spec)
        except asyncio.CancelledError:
            await self._drain(campaign)
            self._finish(campaign, None, interrupted=True)
            raise
        except Exception as exc:                              # noqa: BLE001
            log.exception('[atomic_campaign] AuCaM campaign %s crashed',
                          campaign.campaign_id)
            await self._drain(campaign)
            self._fatal = self._fatal or exc
            self._finish(campaign, None)
            return campaign

        await self._drain(campaign)
        self._finish(campaign, out)
        return campaign

    # ----------------------------------------------------------------------
    async def _drain(self, campaign: Campaign) -> None:
        """Wait for cancelled frames to take their ORBIT tasks down -- or,
        on a shutdown, just for their runners to unwind."""

        pending = [t for t in self._inner if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=SHUTDOWN_WAIT_S
                               if campaign.state == INTERRUPTED
                               else 2 * self._plan.cancel_wait_s)

    # ----------------------------------------------------------------------
    async def _run_frame(self, campaign: Campaign, reporter: Any,
                         context: dict) -> None:
        """The body of one frame: one ATOMIC workflow instance, reported."""

        frame   = int(context['frame'])
        attempt = int(context.get('attempt') or 1)
        wf_id   = reporter.wf_id
        wf      = self._plan.instance(self._frames[frame], wf_id, attempt)
        campaign.workflows.append(wf)
        self.launches[wf_id] = wf
        self.controller.launched(wf_id, frame, wf.id)
        self._changed()

        moved = asyncio.Event()

        def _on_change() -> None:
            moved.set()
            self._changed()

        stop   = asyncio.Event()
        runner = StageRunner(self._fed, self._store, on_change=_on_change,
                             **self._stage_kw)
        task   = asyncio.ensure_future(
            runner.run_workflow(campaign, wf, stop))
        self._inner.add(task)
        task.add_done_callback(self._inner.discard)

        try:
            while not task.done():
                moved.clear()
                waiter = asyncio.ensure_future(moved.wait())
                try:
                    await asyncio.wait({task, waiter},
                                       timeout=self._plan.report_interval_s,
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
                await self._report(campaign, reporter, wf)
            await self._report(campaign, reporter, wf)
            task.result()
        except asyncio.CancelledError:
            await self._stop_frame(campaign, task, stop, wf, wf_id)
            raise
        except FederationUnavailable as exc:
            # no federation: no frame can run -- end the whole campaign
            self._fatal = self._fatal or exc
            self.manager.stop()
            raise

        if wf.state != DONE:
            raise RuntimeError(wf.reason or 'the frame did not complete')

    # ----------------------------------------------------------------------
    async def _stop_frame(self, campaign: Campaign, task: asyncio.Future,
                          stop: asyncio.Event, wf: WorkflowInstance,
                          wf_id: str) -> None:
        """AuCaM cancelled the frame: take its ORBIT task down as well.

        First the runner's own stop event -- it cancels the ORBIT task at
        its next poll.  A runner stuck in a call (a hung submit or poll)
        does not see it; after ``cancel_wait_s`` it is cancelled as an
        asyncio task, and the runner (track A) cancels the ORBIT task.

        Not on a shutdown (the campaign is INTERRUPTED already): then the
        runner task is cancelled at once, sees the stamp and leaves the
        ORBIT task running -- nothing is cancelled, nothing is waited for.
        """

        if campaign.state == INTERRUPTED:
            task.cancel()
            await asyncio.wait({task}, timeout=SHUTDOWN_WAIT_S)
            return

        stop.set()
        wait = self._plan.cancel_wait_s
        done, _ = await asyncio.wait({task}, timeout=wait)
        if not done:
            task.cancel()
            await asyncio.wait({task}, timeout=wait)
        if wf.state not in TERMINAL_STATES:
            wf.state = CANCELED
        if wf.state == CANCELED:
            # None: filled in from the campaign's end once it is known
            _explain(wf, self.controller.why.get(wf_id))
        wf.finished_at = wf.finished_at or self._clock()
        self._changed()

    # ----------------------------------------------------------------------
    def spent(self, wf: WorkflowInstance) -> Tuple[float, float]:
        """``(node_hours, cpu_hours)`` of *wf* so far.

        Counted from each stage's first RUNNING poll (``started_at``) to
        its end (or now): queue wait and the time before the task was
        placed are not charged, as a batch system would not charge them.
        An outage while running is, and so is a re-queue after RUNNING
        (``started_at`` is not reset; see the module docstring).
        """

        now = self._clock()
        nh = ch = 0.0
        for st in wf.stages:
            if st.started_at is None:
                continue
            end = st.finished_at if st.finished_at is not None \
                and st.state in TERMINAL_STATES else now
            hours = max(0.0, float(end) - float(st.started_at)) / 3600.0
            cores = (st.requirements or {}).get('cores') or 1
            nh += hours * self._plan.nodes_per_stage
            ch += hours * float(cores)
        scale = self._plan.node_hour_scale
        return nh * scale, ch * scale

    # ----------------------------------------------------------------------
    def score(self, campaign: Campaign, wf: WorkflowInstance
              ) -> Optional[float]:
        """The frame's score, once its score stage is collected."""

        if wf.id in self._scores:
            return self._scores[wf.id]
        stage = wf.stage(self._plan.score_stage)
        if stage is None or stage.state != DONE:
            return None
        data  = self._store.read_output(campaign.campaign_id, wf.id,
                                        stage.name, self._plan.score_file)
        value = None
        if data is not None:
            try:
                value = _dotted(json.loads(data), self._plan.score_key)
            except ValueError:
                value = None
        self._scores[wf.id] = value
        return value

    # ----------------------------------------------------------------------
    async def _report(self, campaign: Campaign, reporter: Any,
                      wf: WorkflowInstance) -> None:
        nh, ch = self.spent(wf)
        done   = [s for s in wf.stages if s.state == DONE]
        now    = next((s.name for s in wf.stages
                       if s.state not in TERMINAL_STATES), None)
        await reporter.emit(
            score     = self.score(campaign, wf),
            metrics   = {'node_hours' : nh,
                         'cpu_hours'  : ch,
                         'frame'      : wf.params.get('frame'),
                         'workflow'   : wf.id,
                         'stage'      : now,
                         'stages_done': len(done),
                         'state'      : wf.state},
            iteration = len(done))

    # ----------------------------------------------------------------------
    def _finish(self, campaign: Campaign, out: Any, *,
                interrupted: bool = False) -> None:
        """Stamp the campaign's final state and build the outcome."""

        exit_reason = getattr(out, 'exit_reason', None)
        if self._fatal is not None:
            exit_reason = 'failed'
        end = _END_REASONS.get(exit_reason or '', REASON_STOPPED)

        for wf in self.launches.values():
            for st in wf.stages:
                if st.state not in TERMINAL_STATES:
                    st.state = CANCELED
            if wf.state not in TERMINAL_STATES:
                wf.state = CANCELED
            if wf.state == CANCELED and not wf.reason:
                _explain(wf, end)

        if campaign.state in (INTERRUPTED, CANCELED):
            pass                                   # shutdown / cancel won
        elif self._fatal is not None:
            campaign.state  = FAILED
            campaign.reason = str(self._fatal) \
                if isinstance(self._fatal, FederationUnavailable) \
                else 'the campaign manager failed'
            campaign.detail = '%s: %s' % (type(self._fatal).__name__,
                                          self._fatal)
        elif interrupted or self._stop_req or exit_reason == 'signal':
            campaign.state  = CANCELED
            campaign.reason = REASON_STOPPED
        else:
            campaign.state  = DONE
            campaign.reason = self._end_phrase(exit_reason)
        campaign.finished_at = campaign.finished_at or time.time()

        summary = self.controller.get_summary()
        self.outcome = {
            'planner'     : 'aucam',
            'exit_reason' : exit_reason,
            'goal_reached': bool(getattr(out, 'goal_reached', False)),
            'spent'       : dict(getattr(out, 'spent', {}) or {}),
            'budget'      : {'node_hours': self._plan.budget_node_hours},
            'elapsed_s'   : getattr(out, 'elapsed_s', None),
            'aucam'       : {
                'completed'       : dict(getattr(out, 'completed', {}) or {}),
                'cancelled'       : dict(getattr(out, 'cancelled', {}) or {}),
                'failed'          : dict(getattr(out, 'failed', {}) or {}),
                'budget_cancels'  : getattr(out, 'budget_cancels', 0),
                'unfit_dropped'   : getattr(out, 'unfit_dropped', 0),
                'controller_errors': getattr(out, 'controller_errors', 0)},
            **summary}
        self._changed()

    # ----------------------------------------------------------------------
    def _end_phrase(self, exit_reason: Optional[str]) -> str:
        good = len(self.controller.good_frames())
        if exit_reason == 'goals_satisfied':
            return 'the goal was reached: %d good frame%s' \
                   % (good, '' if good == 1 else 's')
        if exit_reason == 'spending_limit':
            return 'the node-hour budget is used up (%d good frame%s)' \
                   % (good, '' if good == 1 else 's')
        return 'every frame has run (%d good frame%s)' \
               % (good, '' if good == 1 else 's')


# --------------------------------------------------------------------------
def _explain(wf: WorkflowInstance, why: Optional[str]) -> None:
    """Say why a cancelled frame was cancelled -- on it and its stages.

    The runner stamps its own "the campaign was stopped" on the stage it
    stopped; for a frame the campaign manager cancelled that is wrong, so
    the manager's reason replaces it.  ``None`` clears it for later.
    """

    wf.reason = why
    for st in wf.stages:
        if st.state == CANCELED:
            st.reason = why or st.reason


# --------------------------------------------------------------------------
def _dotted(doc: Any, key: str) -> Optional[float]:
    cur = doc
    for part in key.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return float(cur) if _is_num(cur) else None


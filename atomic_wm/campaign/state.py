"""Campaign state: dataclasses + JSON persistence.

Three nested records describe everything the plugin knows:

    Campaign  ->  WorkflowInstance  ->  StageRun

A ``Campaign`` is one submitted request (one workflow spec plus a sweep); a
``WorkflowInstance`` is one sweep point (one copy of the spec with its
parameters substituted); a ``StageRun`` is one stage of one instance, i.e.
exactly one task submitted through the federation.

The records are plain dataclasses so ``asdict()`` is the wire format -- the
REST routes return these dicts verbatim (see ``docs/campaign.md``).  The whole
set is persisted to a single JSON file after every state change; on restart
campaigns that were still ``RUNNING`` are marked ``INTERRUPTED`` (there is no
resume -- the demo re-submits).

Interrupting a campaign puts the tasks of its unfinished stages on the
campaign's ``pending_cancels`` list, which is persisted with it and drained
by the plugin once the federation answers -- without a resume nobody would
collect those tasks, and they would hold HPC resources until they end.
"""

from __future__ import annotations

import json
import os
import time
import uuid

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# states.  Stage states mirror the dispatcher's task states where they
# overlap (DONE / FAILED / CANCELED / RUNNING), so a reader does not have to
# translate between two vocabularies.
PENDING     = 'PENDING'       # not started yet
STAGING     = 'STAGING'       # inputs being staged
SUBMITTED   = 'SUBMITTED'     # handed to the federation, not running yet
RUNNING     = 'RUNNING'
DONE        = 'DONE'
FAILED      = 'FAILED'
CANCELED    = 'CANCELED'
SKIPPED     = 'SKIPPED'       # stages only: an earlier stage did not finish
INTERRUPTED = 'INTERRUPTED'   # the service restarted mid-flight

TERMINAL_STATES = (DONE, FAILED, CANCELED, SKIPPED, INTERRUPTED)

STATE_FILE = 'state.json'

# every ``reason`` is rendered VERBATIM by the demo UI, so it is written in
# the demo's vocabulary (resources, campaigns, workflows, stages) and never
# in ORBIT's (broker, pilot, endpoint, plugin, dispatcher).  The technical
# text belongs in ``detail`` and in the log.
REASON_INTERRUPTED = 'the service was restarted while this was running'

# how many finished campaigns to keep (in memory and on disk)
KEEP_TERMINAL = 50

# the dispatcher session the federation submits every task through
# (``FED_SESSION_SID`` in radical.orbit's ``plugin_federation``) -- the
# fallback for a cancel of a task whose submit answer never arrived
FED_DISPATCHER_SID = 'fed'


# --------------------------------------------------------------------------
def default_state_root() -> Path:
    """Directory holding ``state.json`` (``$ATOMIC_CAMPAIGN_STATE`` wins)."""

    env = os.environ.get('ATOMIC_CAMPAIGN_STATE')
    if env:
        return Path(env).expanduser()
    return Path.home() / '.radical' / 'orbit' / 'atomic_campaign'


# --------------------------------------------------------------------------
def default_store_root() -> Path:
    """Root of the central result store (``$ATOMIC_STORE_ROOT`` wins)."""

    env = os.environ.get('ATOMIC_STORE_ROOT')
    if env:
        return Path(env).expanduser()
    return Path.home() / '.radical' / 'orbit' / 'atomic_store'


# --------------------------------------------------------------------------
def new_campaign_id() -> str:
    """Mint a campaign id -- short, unique, and safe in a path or a URL."""

    return 'cmp-%s' % uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------
def _keep_known(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop keys the dataclass does not know (forward compatible reads)."""

    known = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
    return {k: v for k, v in (data or {}).items() if k in known}


# --------------------------------------------------------------------------
@dataclass
class StageRun:
    """One stage of one workflow instance == one federation task."""

    name:            str
    type:            str            = ''
    cmd:             List[str]      = field(default_factory=list)
    inputs:          List[str]      = field(default_factory=list)
    declared_outputs: List[str]     = field(default_factory=list)
    requirements:    Dict[str, Any] = field(default_factory=dict)

    state:           str            = PENDING
    reason:          Optional[str]  = None   # shown on screen, demo words
    detail:          Optional[str]  = None   # raw technical text, for humans

    # filled in as the stage progresses
    task_id:         Optional[str]  = None
    # Placement.  The submit response can only name an *advisory* resource
    # -- the dispatcher picks the member of the class pool when it actually
    # dispatches -- so these are overwritten by what the polls report, and
    # any of them may legitimately be None while a task waits.
    resource:        Optional[str]  = None
    member:          Optional[str]  = None   # short name within `resource`
    member_id:       Optional[str]  = None   # '<resource>.<member>'
    cls:             Optional[str]  = None   # capability class, 'cpu'/'gpu'
    pool:            Optional[str]  = None   # 'fed-<class>'
    dispatcher_sid:  Optional[str]  = None
    child_endpoint:  Optional[str]  = None
    cwd:             Optional[str]  = None
    exit_code:       Optional[int]  = None

    submitted_at:    Optional[float] = None
    started_at:      Optional[float] = None
    finished_at:     Optional[float] = None

    # [{'name', 'size', 'via', 'path'}] -- one entry per collected output
    outputs:         List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # the Explorer module reads a stage's failure text as `error`
        d['error'] = self.reason
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StageRun':
        return cls(**_keep_known(cls, data))

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def summary(self) -> Dict[str, Any]:
        """The compact view used in the POST response and in listings."""

        return {'name'    : self.name,
                'state'   : self.state,
                'resource': self.resource,
                'member'  : self.member,
                'cls'     : self.cls,
                'task_id' : self.task_id,
                'reason'  : self.reason,
                'error'   : self.reason}


# --------------------------------------------------------------------------
@dataclass
class WorkflowInstance:
    """One sweep point: the workflow spec with its parameters substituted."""

    id:         str
    name:       str                  = ''
    params:     Dict[str, Any]       = field(default_factory=dict)
    stages:     List[StageRun]       = field(default_factory=list)
    state:      str                  = PENDING
    reason:     Optional[str]        = None
    detail:     Optional[str]        = None
    created_at: float                = field(default_factory=time.time)
    finished_at: Optional[float]     = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['stages'] = [s.to_dict() for s in self.stages]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkflowInstance':
        data   = dict(data or {})
        stages = [StageRun.from_dict(s) for s in data.pop('stages', [])]
        wf     = cls(**_keep_known(cls, data))
        wf.stages = stages
        return wf

    def stage(self, name: str) -> Optional[StageRun]:
        for s in self.stages:
            if s.name == name:
                return s
        return None

    def summary(self) -> Dict[str, Any]:
        return {'id'    : self.id,
                'params': self.params,
                'state' : self.state,
                'reason': self.reason,
                'stages': [s.summary() for s in self.stages]}


# --------------------------------------------------------------------------
@dataclass
class Campaign:
    """One submitted campaign: N workflow instances over a parameter sweep."""

    campaign_id: str
    name:        str                    = ''
    state:       str                    = RUNNING
    reason:      Optional[str]          = None
    detail:      Optional[str]          = None
    sweep:       Dict[str, List[Any]]   = field(default_factory=dict)
    workflows:   List[WorkflowInstance] = field(default_factory=list)
    # created_at == started_at: a campaign starts driving the moment it is
    # created.  Both are epoch seconds; the UI reads either.
    created_at:  float                  = field(default_factory=time.time)
    started_at:  float                  = field(default_factory=time.time)
    finished_at: Optional[float]        = None
    # [{'task_id', 'dispatcher_sid'[, 'attempts', 'failing_since']}]:
    # cancels the federation has not confirmed yet (it was unreachable);
    # drained once it answers again, abandoned when that takes too long
    pending_cancels: List[Dict[str, Any]] = field(default_factory=list)
    # who plans the campaign: 'sweep' (fixed instances) or 'aucam' (the
    # Campaign Manager launches frames as it goes); AuCaM's outcome summary
    # lands in `outcome` when the campaign ends
    planner:     str                    = 'sweep'
    outcome:     Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['workflows'] = [w.to_dict() for w in self.workflows]
        return d

    def queue_cancel(self, task_id: str,
                     dispatcher_sid: Optional[str] = None) -> bool:
        """Add one task to ``pending_cancels`` (once per task id)."""

        if self.cancel_pending(task_id):
            return False
        self.pending_cancels.append(
            {'task_id': task_id,
             'dispatcher_sid': dispatcher_sid or FED_DISPATCHER_SID})
        return True

    def unqueue_cancel(self, task_id: str) -> None:
        """Drop one task from ``pending_cancels`` (it is settled)."""

        self.pending_cancels[:] = [e for e in self.pending_cancels
                                   if e.get('task_id') != task_id]

    def note_cancel_failure(self, task_id: str, now: float) -> None:
        """One more try of *task_id*'s cancel could not reach the
        federation; the first such failure starts its clock."""

        for entry in self.pending_cancels:
            if entry.get('task_id') == task_id:
                entry['attempts'] = int(entry.get('attempts') or 0) + 1
                entry.setdefault('failing_since', now)

    def requeue_cancel_last(self, task_id: str) -> None:
        """Move *task_id*'s entry to the end: the next drain pass starts
        with the entries behind it."""

        mine = [e for e in self.pending_cancels if e.get('task_id') == task_id]
        self.unqueue_cancel(task_id)
        self.pending_cancels.extend(mine)

    def abandon_cancel(self, task_id: str, why: str) -> None:
        """Give up on *task_id*'s cancel; say so in ``detail``."""

        self.unqueue_cancel(task_id)
        note = 'cancel of task %s abandoned (%s)' % (task_id, why)
        self.detail = '%s; %s' % (self.detail, note) if self.detail else note

    def cancel_pending(self, task_id: str) -> bool:
        """Whether *task_id* still waits on ``pending_cancels``."""

        return any(e.get('task_id') == task_id for e in self.pending_cancels)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Campaign':
        data = dict(data or {})
        wfs  = [WorkflowInstance.from_dict(w)
                for w in data.pop('workflows', [])]
        camp = cls(**_keep_known(cls, data))
        camp.workflows = wfs
        return camp

    def workflow(self, wf_id: str) -> Optional[WorkflowInstance]:
        for wf in self.workflows:
            if wf.id == wf_id:
                return wf
        return None

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def summary(self) -> Dict[str, Any]:
        """List view: no per-stage detail, but the workflow state counts."""

        counts: Dict[str, int] = {}
        for wf in self.workflows:
            counts[wf.state] = counts.get(wf.state, 0) + 1
        return {'campaign_id': self.campaign_id,
                'name'       : self.name,
                'state'      : self.state,
                'reason'     : self.reason,
                'sweep'      : self.sweep,
                'planner'    : self.planner,
                'created_at' : self.created_at,
                'started_at' : self.started_at,
                'finished_at': self.finished_at,
                'n_workflows': len(self.workflows),
                'workflow_states': counts}

    def brief(self) -> Dict[str, Any]:
        """The POST ``campaigns`` response: id, state, workflow summaries."""

        return {'campaign_id': self.campaign_id,
                'name'       : self.name,
                'state'      : self.state,
                'created_at' : self.created_at,
                'started_at' : self.started_at,
                'finished_at': self.finished_at,
                'workflows'  : [wf.summary() for wf in self.workflows]}

    # -- roll-up ----------------------------------------------------------
    def refresh_state(self) -> str:
        """Recompute the campaign state from its workflows.

        RUNNING while any workflow is unfinished; then CANCELED if any was
        canceled, FAILED if any failed, DONE otherwise.  A campaign that was
        explicitly canceled or interrupted keeps that state.
        """

        if self.state in (CANCELED, INTERRUPTED):
            return self.state
        if any(wf.state not in TERMINAL_STATES for wf in self.workflows):
            self.state = RUNNING
            return self.state
        states = {wf.state for wf in self.workflows}
        if CANCELED in states:  self.state = CANCELED
        elif FAILED in states:  self.state = FAILED
        else:
            # everything succeeded: a cancel that arrived after the last
            # stage finished must not leave its reason behind
            self.state  = DONE
            self.reason = None
            self.detail = None
        if self.state in (FAILED, CANCELED) and not self.reason:
            self._explain_rollup()
        if self.finished_at is None:
            self.finished_at = time.time()
        return self.state

    def _explain_rollup(self) -> None:
        """Give a rolled-up FAILED/CANCELED campaign its own reason.

        The demo card and ``atomic-campaign status`` show the *campaign's*
        reason, so a campaign that failed only because its workflows did
        must repeat what they said -- verbatim, since a workflow reason is
        already an on-screen phrase ("stage 'md': no resources have joined
        the federation yet").  Workflows that failed for different reasons
        cannot be summed up in one phrase, so they are counted instead and
        the per-workflow reasons stay where they are.

        Never called when a reason is already set: cancel, interrupt and
        the driver's own ``_terminate`` all know better than the roll-up.
        """

        bad = [wf for wf in self.workflows if wf.state in (FAILED, CANCELED)]
        if not bad:
            return
        reasons = {wf.reason for wf in bad}
        if len(reasons) == 1 and bad[0].reason:
            self.reason = bad[0].reason
            self.detail = self.detail or bad[0].detail
        else:
            self.reason = '%d of %d workflows failed' % (len(bad),
                                                         len(self.workflows))
            self.detail = self.detail or bad[0].detail or bad[0].reason


# --------------------------------------------------------------------------
def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via a temp file + ``os.replace`` (never a partial read)."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as fd:
        json.dump(data, fd, indent=2, sort_keys=False, default=str)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
def prune_campaigns(campaigns: List[Campaign],
                    keep_terminal: int = KEEP_TERMINAL) -> List[Campaign]:
    """Keep every unfinished campaign and the newest *keep_terminal* others.

    A demo broker that is up for a day must not grow an unbounded state file
    (or an unbounded ``GET campaigns`` listing).  A finished campaign with
    unsettled ``pending_cancels`` counts as unfinished: dropping it would
    drop the only record of tasks still to be cancelled.
    """

    def _live(c: Campaign) -> bool:
        return not c.is_terminal() or bool(c.pending_cancels)

    live = [c for c in campaigns if _live(c)]
    done = sorted((c for c in campaigns if not _live(c)),
                  key=lambda c: (c.created_at if c.finished_at is None
                                 else c.finished_at), reverse=True)
    keep = set(id(c) for c in live) | set(id(c) for c in done[:keep_terminal])
    return [c for c in campaigns if id(c) in keep]


# --------------------------------------------------------------------------
def save_campaigns(path: Path, campaigns: List[Campaign]) -> None:
    """Persist every campaign to ``path`` (one JSON document)."""

    write_json_atomic(Path(path),
                      {'version'  : 1,
                       'saved_at' : time.time(),
                       'campaigns': [c.to_dict() for c in campaigns]})


# --------------------------------------------------------------------------
def load_campaigns(path: Path) -> List[Campaign]:
    """Read campaigns back; a running campaign becomes ``INTERRUPTED``.

    A missing or unreadable state file yields an empty list -- a broken state
    file must never keep the broker from starting.
    """

    path = Path(path)
    if not path.is_file():
        return []
    try:
        with open(path, encoding='utf-8') as fd:
            data = json.load(fd)
    except (OSError, ValueError):
        return []

    out: List[Campaign] = []
    for entry in (data or {}).get('campaigns', []):
        try:
            camp = Campaign.from_dict(entry)
        except (TypeError, ValueError):
            continue
        if not camp.is_terminal():
            mark_interrupted(camp)
        out.append(camp)
    return out


# --------------------------------------------------------------------------
def mark_interrupted(camp: Campaign) -> None:
    """Mark a campaign (and its unfinished parts) INTERRUPTED.

    Used both on restart -- a campaign found ``RUNNING`` in the state file --
    and on an orderly shutdown, before the driver tasks are cancelled.

    The task of every stage interrupted here is queued for a cancel
    (``pending_cancels``).  Nothing is called from here: on shutdown the
    federation may be going down too, and the plugin drains the list once
    it answers -- at the latest after the next start.
    """

    why = REASON_INTERRUPTED
    camp.state       = INTERRUPTED
    camp.reason      = camp.reason or why
    camp.finished_at = camp.finished_at or time.time()
    for wf in camp.workflows:
        if wf.state not in TERMINAL_STATES:
            wf.state  = INTERRUPTED
            wf.reason = wf.reason or why
        for stage in wf.stages:
            if not stage.is_terminal():
                if stage.task_id and stage.state != PENDING:
                    camp.queue_cancel(stage.task_id, stage.dispatcher_sid)
                stage.state  = INTERRUPTED
                stage.reason = stage.reason or why

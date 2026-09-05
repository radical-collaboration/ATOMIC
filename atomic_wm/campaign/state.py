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
    resource:        Optional[str]  = None
    pool:            Optional[str]  = None
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

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['workflows'] = [w.to_dict() for w in self.workflows]
        return d

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
        if self.finished_at is None:
            self.finished_at = time.time()
        return self.state


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
    (or an unbounded ``GET campaigns`` listing).
    """

    live = [c for c in campaigns if not c.is_terminal()]
    done = sorted((c for c in campaigns if c.is_terminal()),
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
                stage.state  = INTERRUPTED
                stage.reason = stage.reason or why

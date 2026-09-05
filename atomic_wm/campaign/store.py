"""The central result store on the broker host.

Layout (root defaults to ``~/.radical/orbit/atomic_store``, override with
``$ATOMIC_STORE_ROOT``)::

    <root>/<campaign_id>/<workflow_id>/<stage>/md.json
    <root>/<campaign_id>/<workflow_id>/<stage>/manifest.json

Every declared output of every stage is copied here as soon as its task
reaches a terminal state -- *before* the pilot that produced it can go away.
``manifest.json`` records where each file came from and which of the three
collection paths worked (see :func:`collect`).
"""

from __future__ import annotations

import json
import logging
import time

from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, \
                   Tuple

from .state import Campaign, StageRun, WorkflowInstance, default_store_root, \
                   write_json_atomic

log = logging.getLogger('radical.orbit')

MANIFEST = 'manifest.json'

# one collection source: a label recorded in the manifest plus a coroutine
# which returns the file's bytes, or None when this path cannot serve it.
Source = Tuple[str, Callable[[str], Awaitable[Optional[bytes]]]]


# --------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    """Reject anything that would escape the store directory."""

    if not name or '/' in name or '\\' in name or name in ('.', '..'):
        raise ValueError('unsafe store path component: %r' % name)
    return name


# --------------------------------------------------------------------------
class ResultStore:
    """Filesystem layout + collection for one broker's campaigns."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = Path(root) if root else default_store_root()

    # -- layout -----------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    def campaign_dir(self, cid: str) -> Path:
        return self._root / _safe_name(cid)

    def workflow_dir(self, cid: str, wf_id: str) -> Path:
        return self.campaign_dir(cid) / _safe_name(wf_id)

    def stage_dir(self, cid: str, wf_id: str, stage: str) -> Path:
        return self.workflow_dir(cid, wf_id) / _safe_name(stage)

    # -- files ------------------------------------------------------------
    def write_output(self, cid: str, wf_id: str, stage: str, name: str,
                     data: bytes) -> Path:
        """Copy one collected output into the store; return its path."""

        path = self.stage_dir(cid, wf_id, stage) / _safe_name(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def read_output(self, cid: str, wf_id: str, stage: str,
                    name: str) -> Optional[bytes]:
        """Read a collected output back (``None`` when it was not collected)."""

        path = self.stage_dir(cid, wf_id, stage) / _safe_name(name)
        if not path.is_file():
            return None
        return path.read_bytes()

    def write_manifest(self, cid: str, wf_id: str, stage: str,
                       manifest: Dict[str, Any]) -> Path:
        path = self.stage_dir(cid, wf_id, stage) / MANIFEST
        write_json_atomic(path, manifest)
        return path

    # -- collection -------------------------------------------------------
    async def collect(self, cid: str, wf_id: str, stage: str,
                      filenames: Sequence[str],
                      sources: Sequence[Source]) -> List[Dict[str, Any]]:
        """Fetch every file in *filenames* through the first source that works.

        *sources* is tried in order per file; the label of the source that
        delivered the bytes is recorded as ``via``.  A file no source can
        deliver gets an entry with ``via: None`` and the per-source errors --
        collection never raises, because a missing output must fail the stage
        with a readable reason rather than blow up the driver.
        """

        collected: List[Dict[str, Any]] = []
        for name in filenames:
            entry: Dict[str, Any] = {'name': name, 'via': None, 'size': None,
                                     'path': None, 'errors': {}}
            for label, fetch in sources:
                try:
                    data = await fetch(name)
                except Exception as exc:                 # noqa: BLE001
                    entry['errors'][label] = '%s: %s' % (type(exc).__name__,
                                                         exc)
                    continue
                if data is None:
                    entry['errors'][label] = 'not available'
                    continue
                try:
                    path = self.write_output(cid, wf_id, stage, name, data)
                except (OSError, ValueError) as exc:
                    entry['errors'][label] = 'store write failed: %s' % exc
                    break
                entry.update({'via' : label, 'size': len(data),
                              'path': str(path)})
                break
            collected.append(entry)
        return collected

    # -- views ------------------------------------------------------------
    def layout(self, cid: Optional[str] = None) -> Dict[str, Any]:
        """The store as the UI links it: campaigns / workflows / stages."""

        campaigns = []
        roots = [self.campaign_dir(cid)] if cid else \
                sorted(p for p in self._safe_iterdir(self._root) if p.is_dir())
        for cdir in roots:
            if not cdir.is_dir():
                continue
            workflows = []
            for wdir in sorted(p for p in self._safe_iterdir(cdir)
                               if p.is_dir()):
                stages = []
                for sdir in sorted(p for p in self._safe_iterdir(wdir)
                                   if p.is_dir()):
                    stages.append({
                        'name' : sdir.name,
                        'path' : str(sdir),
                        'files': [{'name': f.name, 'size': f.stat().st_size}
                                  for f in sorted(self._safe_iterdir(sdir))
                                  if f.is_file()]})
                workflows.append({'id': wdir.name, 'path': str(wdir),
                                  'stages': stages})
            campaigns.append({'campaign_id': cdir.name, 'path': str(cdir),
                              'workflows': workflows})
        return {'root': str(self._root), 'campaigns': campaigns}

    @staticmethod
    def _safe_iterdir(path: Path) -> List[Path]:
        try:
            return list(path.iterdir())
        except OSError:
            return []

    # -- results ----------------------------------------------------------
    def results(self, campaign: Campaign) -> Dict[str, Any]:
        """Parse every collected ``*.json`` output -- what the UI plots.

        Per workflow: ``metrics[stage]`` is the parsed JSON envelope of the
        stage's *first* collected ``.json`` output; every collected file
        (JSON or not) is listed under ``files[stage]`` with its size.
        """

        workflows = []
        for wf in campaign.workflows:
            metrics: Dict[str, Any] = {}
            files:   Dict[str, List[Dict[str, Any]]] = {}
            for stage in wf.stages:
                listed, envelope = self._stage_results(campaign.campaign_id,
                                                       wf, stage)
                if listed:
                    files[stage.name] = listed
                if envelope is not None:
                    metrics[stage.name] = envelope
            workflows.append({'id'     : wf.id,
                              'params' : wf.params,
                              'state'  : wf.state,
                              'reason' : wf.reason,
                              'metrics': metrics,
                              'files'  : files})
        return {'campaign_id': campaign.campaign_id,
                'state'      : campaign.state,
                'workflows'  : workflows}

    # ----------------------------------------------------------------------
    def _stage_results(self, cid: str, wf: WorkflowInstance, stage: StageRun
                       ) -> Tuple[List[Dict[str, Any]], Optional[Any]]:
        """Return ``(file listing, first parsed JSON envelope or None)``."""

        listing:  List[Dict[str, Any]] = []
        envelope: Optional[Any] = None

        sdir = self.stage_dir(cid, wf.id, stage.name)
        for name in self._collected_names(stage, sdir):
            path = sdir / name
            if not path.is_file():
                continue
            size  = path.stat().st_size
            entry = {'name': name, 'size': size, 'json': False}
            if name.lower().endswith('.json'):
                try:
                    with open(path, encoding='utf-8') as fd:
                        data = json.load(fd)
                    entry['json'] = True
                    if envelope is None:
                        envelope = data
                except (OSError, ValueError) as exc:
                    entry['error'] = 'not valid JSON: %s' % exc
            listing.append(entry)
        return listing, envelope

    @staticmethod
    def _collected_names(stage: StageRun, sdir: Path) -> List[str]:
        """Declared outputs first (stable order), then anything else found."""

        names = [n for n in stage.declared_outputs]
        try:
            extra = sorted(p.name for p in sdir.iterdir()
                           if p.is_file() and p.name != MANIFEST)
        except OSError:
            extra = []
        for name in extra:
            if name not in names:
                names.append(name)
        return names


# --------------------------------------------------------------------------
def stage_manifest(campaign_id: str, wf: WorkflowInstance, stage: StageRun,
                   collected: List[Dict[str, Any]],
                   extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build the ``manifest.json`` document for one finished stage."""

    manifest = {
        'campaign_id'   : campaign_id,
        'workflow_id'   : wf.id,
        'workflow_name' : wf.name,
        'params'        : wf.params,
        'stage'         : stage.name,
        'stage_type'    : stage.type,
        'state'         : stage.state,
        'reason'        : stage.reason,
        'exit_code'     : stage.exit_code,
        'cmd'           : stage.cmd,
        'resource'      : stage.resource,
        'pool'          : stage.pool,
        'dispatcher_sid': stage.dispatcher_sid,
        'child_endpoint': stage.child_endpoint,
        'task_id'       : stage.task_id,
        'cwd'           : stage.cwd,
        'submitted_at'  : stage.submitted_at,
        'started_at'    : stage.started_at,
        'finished_at'   : stage.finished_at,
        'collected_at'  : time.time(),
        'outputs'       : collected,
    }
    manifest.update(extra or {})
    return manifest

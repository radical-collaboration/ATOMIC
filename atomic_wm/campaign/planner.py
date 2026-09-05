"""Campaign planner: one workflow spec + a sweep -> N workflow instances.

This is the *deliberately simple* stand-in for a campaign manager (see
``docs/campaign_seam.md``).  ``CampaignPlanner`` is the seam; ``SweepPlanner``
is the only implementation the demo needs: the cartesian product of the sweep
values, one workflow instance per point, ``{param}`` placeholders in the
stage commands substituted with that point's values.

Everything the planner rejects is rejected *before* a campaign is created, so
a bad spec is a 400 at submit time and never a half-run campaign.
"""

from __future__ import annotations

import itertools
import re

from typing import Any, Dict, Iterable, List, Optional, Sequence

from .state import StageRun, WorkflowInstance

# A placeholder is a bare ``{name}``; nothing fancier is supported (no
# ``{a.b}``, no format specs), so a command that legitimately contains braces
# -- a JSON snippet, a shell brace expansion -- is left alone unless it looks
# exactly like an identifier in braces.
_PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')


# --------------------------------------------------------------------------
class PlannerError(ValueError):
    """A workflow spec or sweep the planner cannot expand (-> HTTP 400)."""


# --------------------------------------------------------------------------
def format_value(value: Any) -> str:
    """Render a sweep value for a command line.

    Ints stay ints (``300``, not ``300.0``), bools become ``true``/``false``
    (what a JSON-minded tool expects), everything else is ``str()``.
    """

    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


# --------------------------------------------------------------------------
def substitute(text: str, params: Dict[str, Any]) -> str:
    """Replace every ``{param}`` in *text*; raise on an unknown placeholder."""

    def _repl(match: 're.Match[str]') -> str:
        key = match.group(1)
        if key not in params:
            raise PlannerError(
                "unknown parameter %r in %r (known: %s)"
                % (key, text, ', '.join(sorted(params)) or 'none'))
        return format_value(params[key])

    return _PLACEHOLDER.sub(_repl, text)


# --------------------------------------------------------------------------
def placeholders(text: str) -> List[str]:
    """Every ``{param}`` name occurring in *text*."""

    return _PLACEHOLDER.findall(text)


# --------------------------------------------------------------------------
def _as_str_list(value: Any, what: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise PlannerError('%s must be a list of strings' % what)
    out = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise PlannerError('%s must be a list of non-empty strings' % what)
        out.append(item)
    return out


# --------------------------------------------------------------------------
def validate_workflow(workflow: Any) -> None:
    """Reject a workflow spec the runner could not execute.

    Checked: the spec is a dict with a non-empty ``stages`` list; every stage
    has a unique, path-safe ``name`` and a non-empty ``cmd`` list of strings;
    ``inputs``/``outputs`` are lists of file names (no directories);
    ``requirements`` is a dict.
    """

    if not isinstance(workflow, dict):
        raise PlannerError('workflow must be a JSON object')

    stages = workflow.get('stages')
    if not isinstance(stages, list) or not stages:
        raise PlannerError('workflow needs a non-empty "stages" list')

    params = workflow.get('params')
    if params is not None and not isinstance(params, dict):
        raise PlannerError('workflow "params" must be an object')

    seen = set()
    for idx, stage in enumerate(stages):
        name = _validate_stage(idx, stage)
        if name in seen:
            raise PlannerError('duplicate stage name: %r' % name)
        seen.add(name)


# --------------------------------------------------------------------------
def _validate_stage(idx: int, stage: Any) -> str:
    """Validate one stage; return its name."""

    if not isinstance(stage, dict):
        raise PlannerError('stage %d must be a JSON object' % idx)

    name = stage.get('name')
    if not isinstance(name, str) or not name:
        raise PlannerError('stage %d needs a "name"' % idx)
    if '/' in name or name in ('.', '..'):
        raise PlannerError('invalid stage name: %r' % name)

    cmd = stage.get('cmd')
    if not isinstance(cmd, list) or not cmd:
        raise PlannerError('stage %r needs a non-empty "cmd" list' % name)
    if any(not isinstance(arg, str) for arg in cmd):
        raise PlannerError('stage %r: every "cmd" element must be a string'
                           % name)

    for key in ('inputs', 'outputs'):
        for fname in _as_str_list(stage.get(key),
                                  'stage %r "%s"' % (name, key)):
            if '/' in fname or fname in ('.', '..'):
                raise PlannerError(
                    'stage %r: %s entry %r must be a plain file name '
                    '(staging works on the task working directory only)'
                    % (name, key, fname))

    reqs = stage.get('requirements')
    if reqs is not None and not isinstance(reqs, dict):
        raise PlannerError('stage %r: "requirements" must be an object' % name)
    return name


# --------------------------------------------------------------------------
def validate_sweep(sweep: Any) -> Dict[str, List[Any]]:
    """Normalise the sweep to ``{param: [values]}``; reject anything else."""

    if sweep is None:
        return {}
    if not isinstance(sweep, dict):
        raise PlannerError('sweep must be a JSON object of '
                           '{"param": [values]}')
    out: Dict[str, List[Any]] = {}
    for key, values in sweep.items():
        if not isinstance(key, str) or not key:
            raise PlannerError('sweep keys must be non-empty strings')
        if not isinstance(values, (list, tuple)) or not values:
            raise PlannerError('sweep parameter %r needs a non-empty list '
                               'of values' % key)
        out[key] = list(values)
    return out


# --------------------------------------------------------------------------
class CampaignPlanner:
    """Seam: expand a workflow spec into the instances a campaign runs.

    A real campaign manager (``radical.orbit/cm/``) plans a DAG of workflow
    groups; this interface is the single method the runner needs from it.
    """

    def expand(self, workflow: Dict[str, Any],
               sweep: Optional[Dict[str, List[Any]]]
               ) -> List[WorkflowInstance]:
        raise NotImplementedError('subclass responsibility')


# --------------------------------------------------------------------------
class SweepPlanner(CampaignPlanner):
    """The fake campaign manager: one workflow copy per sweep point.

    With several sweep keys the points are the cartesian product, generated
    in key-declaration order with the *last* key varying fastest -- so
    ``{"temperature": [300, 600], "steps": [10, 20]}`` yields
    ``(300, 10), (300, 20), (600, 10), (600, 20)``.
    """

    def expand(self, workflow: Dict[str, Any],
               sweep: Optional[Dict[str, List[Any]]]
               ) -> List[WorkflowInstance]:

        validate_workflow(workflow)
        points = validate_sweep(sweep)

        base_params = dict(workflow.get('params') or {})
        wf_name     = str(workflow.get('name') or 'workflow')

        instances: List[WorkflowInstance] = []
        for idx, point in enumerate(self._points(points)):
            params = dict(base_params)
            params.update(point)
            instances.append(
                self._instance('wf-%03d' % idx, wf_name, params, workflow))
        return instances

    # ----------------------------------------------------------------------
    @staticmethod
    def _points(sweep: Dict[str, List[Any]]) -> Iterable[Dict[str, Any]]:
        """Yield the sweep points (a single empty point when no sweep)."""

        if not sweep:
            return [{}]
        keys = list(sweep)
        return [dict(zip(keys, combo))
                for combo in itertools.product(*(sweep[k] for k in keys))]

    # ----------------------------------------------------------------------
    def _instance(self, wf_id: str, wf_name: str, params: Dict[str, Any],
                  workflow: Dict[str, Any]) -> WorkflowInstance:
        """Build one instance: substitute the parameters into every stage."""

        stages: List[StageRun] = []
        for spec in workflow['stages']:
            cmd = [substitute(arg, params) for arg in spec['cmd']]
            stages.append(StageRun(
                name             = spec['name'],
                type             = str(spec.get('type') or ''),
                cmd              = cmd,
                inputs           = list(spec.get('inputs') or []),
                declared_outputs = list(spec.get('outputs') or []),
                requirements     = dict(spec.get('requirements') or {})))

        return WorkflowInstance(id=wf_id, name=wf_name, params=params,
                                stages=stages)


# --------------------------------------------------------------------------
def required_parameters(workflow: Dict[str, Any]) -> Sequence[str]:
    """Every ``{param}`` the spec's commands reference, in first-seen order."""

    seen: List[str] = []
    for stage in (workflow or {}).get('stages') or []:
        for arg in stage.get('cmd') or []:
            if not isinstance(arg, str):
                continue
            for name in placeholders(arg):
                if name not in seen:
                    seen.append(name)
    return seen

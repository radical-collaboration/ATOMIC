"""The (deliberately simple) ATOMIC campaign manager.

One workflow spec plus a parameter sweep become N workflow instances; each
instance runs its stages sequentially through the resource federation, its
outputs are collected into a central store, and the whole thing is exposed
over the ``atomic_campaign`` broker plugin.

Two seams keep the fake replaceable by the real Campaign Manager (see
``docs/campaign_seam.md``):

- :class:`~atomic_wm.campaign.planner.CampaignPlanner` -- what to run.
- :class:`~atomic_wm.campaign.runner.FederationAPI` /
  :class:`~atomic_wm.campaign.runner.StageRunner` -- how to run it.

This package is import-safe without ``radical.orbit``: only
``atomic_wm.plugins.campaign`` (the plugin shell) needs orbit.
"""

from .planner import (CampaignPlanner, PlannerError, SweepPlanner,
                      required_parameters, substitute, validate_sweep,
                      validate_workflow)
from .runner  import (CampaignRunner, FederationAPI, FederationUnavailable,
                      StageFailed, StageRunner)
from .state   import (Campaign, StageRun, WorkflowInstance,
                      default_state_root, default_store_root, load_campaigns,
                      new_campaign_id, save_campaigns)
from .store   import ResultStore, stage_manifest

__all__ = [
    'Campaign', 'CampaignPlanner', 'CampaignRunner', 'FederationAPI',
    'FederationUnavailable', 'PlannerError', 'ResultStore', 'StageFailed',
    'StageRun', 'StageRunner', 'SweepPlanner', 'WorkflowInstance',
    'default_state_root', 'default_store_root', 'load_campaigns',
    'new_campaign_id', 'required_parameters', 'save_campaigns',
    'stage_manifest', 'substitute', 'validate_sweep', 'validate_workflow',
]

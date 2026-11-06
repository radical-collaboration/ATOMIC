"""ATOMIC workflow/campaign demo layer on top of ``radical.orbit``.

Sub-packages:

* ``atomic_wm.workload`` -- synthetic ("fake") workload tools which stand
  in for the real scientific codes.  Stdlib only, see ``docs/workload.md``.
* ``atomic_wm.cli``      -- command line clients for the federation and
  campaign services (``atomic-join`` & friends).
* ``atomic_wm.plugins``  -- broker-hosted orbit plugins (campaign manager),
  registered through the ``radical.orbit.plugins`` entry-point group.
"""

__version__ = '0.1.0'

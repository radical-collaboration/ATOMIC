"""Broker-hosted orbit plugins shipped by ATOMIC.

This package is the target of the ``radical.orbit.plugins`` entry point
declared in ``pyproject.toml``::

    [project.entry-points."radical.orbit.plugins"]
    atomic_campaign = "atomic_wm.plugins"

Orbit's ``plugin_host_base._discover_entry_points()`` simply imports the
entry point target; importing the plugin class triggers
``Plugin.__init_subclass__`` and thereby its registration.

The import below is deliberately defensive: the entry point must resolve
in *every* environment atomic-wm gets installed into -- including a login
node or a pilot's python which has no ``radical.orbit`` -- while the
campaign plugin itself is only added by build piece 04
(``plans/04-atomic-campaign.md``), so ``campaign.py`` does not exist yet.

Only those two causes are swallowed.  A ``ModuleNotFoundError`` raised
from *inside* ``campaign.py`` (a typo, a forgotten dependency) is
re-raised -- silently registering no plugin would be far worse than a
loud failure, and orbit logs entry-point exceptions.
"""

try:
    # `type: ignore` keeps type checkers quiet about the module which
    # piece 04 has yet to add -- drop the comment once it exists.
    from .campaign import PluginAtomicCampaign  # noqa: F401  # type: ignore

except ModuleNotFoundError as e:
    missing = e.name or ''
    if missing != 'atomic_wm.plugins.campaign' \
            and not missing.startswith('radical'):
        raise

"""Explorer UI assets for the ATOMIC campaign plugin.

The package holds no python logic -- only the ES module the ORBIT Explorer
loads as the ``atomic_campaign`` plugin's ``ui_module``.  It is a package
(rather than a bare data directory) so that ``importlib.resources`` can
locate the file inside an installed wheel, and so that setuptools ships it
via the ``[tool.setuptools.package-data] atomic_wm = ["ui/*.js"]`` entry.

The campaign plugin (P4) points ``ui_module`` at :func:`ui_module_path`.
"""

import os

__all__ = ['UI_MODULE', 'ui_module_path']

# the Explorer serves the module at /plugins/<plugin name>.js -- the plugin
# registry name (`atomic_campaign`) decides that URL, not this file name,
# but keeping them equal avoids a needless indirection
UI_MODULE = 'atomic_campaign.js'


def ui_module_path() -> str:
    """Absolute path of the Explorer UI module.

    ``ui_module`` must be an absolute ``.js`` path; the broker plugin host
    reads the file off disk (``broker_plugin_host.get_ui_modules()``) and
    the gateway caches it until a miss -- restart the broker after editing
    the module.
    """

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), UI_MODULE)


from .atomic_module import *


# ------------------------------------------------------------------------------
#
import os as _os
import radical.utils as _ru


_mod_root = _os.path.dirname (__file__)

version_short, version_base, version_branch, version_tag, version_detail \
             = _ru.get_version(_mod_root)
version      = version_short
__version__  = version_detail


# ------------------------------------------------------------------------------


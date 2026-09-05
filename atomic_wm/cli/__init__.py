"""Command line clients for the ATOMIC demo.

``atomic-join`` / ``atomic-leave`` / ``atomic-resources`` talk to the
orbit ``federation`` plugin (piece 02), ``atomic-campaign`` talks to the
``atomic_campaign`` plugin (piece 04).

Until those pieces land, each module here is a placeholder which reports
'not implemented yet' and exits 2 -- so the console scripts declared in
``pyproject.toml`` always resolve and an install never breaks.
"""

import sys

from typing import Optional, Sequence


# ---------------------------------------------------------------------------
def not_implemented(tool: str, piece: str,
                    argv: Optional[Sequence[str]] = None) -> int:
    """Report a not-yet-implemented CLI and exit 2.

    Kept in one place so replacing a placeholder means replacing exactly
    one module.
    """

    del argv  # placeholders ignore their arguments

    sys.stderr.write('%s: not implemented yet (build piece %s)\n'
                     % (tool, piece))
    return 2

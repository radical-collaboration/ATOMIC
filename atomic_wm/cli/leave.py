"""``atomic-leave NAME`` -- remove a resource from the federation.

Placeholder; implemented by build piece 02 (``plans/02-atomic-join-cli.md``).
"""

import sys

from typing import Optional, Sequence

from . import not_implemented

TOOL = 'atomic-leave'


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console script entry point (placeholder)."""

    return not_implemented(TOOL, '02', argv)


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.exit(main())

"""``atomic-resources`` -- list federated resources (incl. node-hours).

Placeholder; implemented by build piece 02 (``plans/02-atomic-join-cli.md``).
"""

import sys

from typing import Optional, Sequence

from . import not_implemented

TOOL = 'atomic-resources'


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console script entry point (placeholder)."""

    return not_implemented(TOOL, '02', argv)


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.exit(main())

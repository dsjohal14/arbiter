"""`python -m arbiter`, for an environment where the console script is not on PATH."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

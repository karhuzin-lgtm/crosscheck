"""``python -m crosscheck`` — same as the ``crosscheck`` command."""

import sys

from .cli import main

sys.exit(main())

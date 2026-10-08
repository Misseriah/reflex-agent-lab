import sys

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required. On this machine use: python3.12 -m reflex")

from .cli import main

raise SystemExit(main())

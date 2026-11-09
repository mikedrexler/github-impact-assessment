#!/usr/bin/env python3
"""Launch from a checkout by file path, independent of the working directory."""

import sys


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        raise SystemExit("GitHub Impact Assessment requires Python 3.11 or newer.")

    from impact.app import main

    raise SystemExit(main())

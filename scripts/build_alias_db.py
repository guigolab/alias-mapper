#!/usr/bin/env python3
"""
scripts/build_alias_db.py
-------------------------
Thin CLI wrapper around alias_mapper.build_alias_db. Kept here so the
GitHub Actions workflow (and anyone else referencing the script path)
keeps working after the v1 packaging restructure.

The actual logic lives in alias_mapper.build_alias_db.build_db; this
file just makes sure the script is on the Python path and invokes
its CLI entry point.
"""

import sys
from pathlib import Path

# Make the package importable when running from a source checkout
# without `pip install`. After `pip install`, this insert is harmless.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alias_mapper.build_alias_db import _cli

if __name__ == "__main__":
    _cli()

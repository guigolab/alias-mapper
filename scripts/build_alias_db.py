#!/usr/bin/env python3
"""
scripts/build_alias_db.py
-------------------------
Thin CLI wrapper around `alias_mapper.build_alias_db`. Used for manual
debugging — "I have a TSV on disk, build me a SQLite from it":

    python3 scripts/build_alias_db.py --tsv aliases.tsv.gz --db out.db

Not called by CI anymore. The weekly workflow only runs
scripts/collect_aliases.py, and the CLI's bootstrap flow imports
build_db directly from the package on first run. This wrapper exists
for cases where you want to rebuild a DB from a Release artifact
without going through the CLI's full bootstrap.

The actual logic lives in `alias_mapper.build_alias_db.build_db`; this
file just makes sure the package is importable from a source checkout
and invokes the CLI entry point.
"""

import sys
from pathlib import Path

# Make the package importable when running from a source checkout
# without `pip install`. After `pip install`, this insert is harmless.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alias_mapper.build_alias_db import _cli

if __name__ == "__main__":
    _cli()

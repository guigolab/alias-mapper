#!/usr/bin/env python3
"""
alias_source.py
---------------
Abstraction over the alias data source. The CLI and translator code
ask an `AliasSource` for an alias map; they don't care whether the
source is a local SQLite file, a TSV, or (eventually) a remote HTTP
endpoint.

Today's only implementation is SqliteAliasSource. When the hosted API
ships, HttpAliasSource will live alongside it implementing the same
interface, and the CLI will pick one based on config.
"""

from abc import ABC, abstractmethod
from pathlib import Path
import sqlite3
import sys


class AliasNotFoundError(Exception):
    """Raised when an assembly has no rows for the requested convention pair."""


class AssemblyNotFoundError(Exception):
    """Raised when the requested assembly accession doesn't exist in the source."""


class AliasSource(ABC):
    """
    Interface for anything that can answer alias lookup queries.

    Implementations:
      - SqliteAliasSource: reads from a local SQLite DB built by build_alias_db.py
      - (future) HttpAliasSource: reads from the hosted API
    """

    @abstractmethod
    def assembly_exists(self, assembly: str) -> bool:
        """Return True if the assembly accession is known to this source."""

    @abstractmethod
    def get_map(
        self,
        assembly: str,
        source_column: str,
        target_column: str,
    ) -> dict[str, str]:
        """
        Return a {source_name -> target_name} dict for one assembly.

        Rows where either column is NULL are skipped.

        Raises:
            AssemblyNotFoundError: assembly accession not in the source.
            AliasNotFoundError:    assembly exists, but no rows have both
                                   source_column and target_column populated.
        """


class SqliteAliasSource(AliasSource):
    """Alias source backed by a local SQLite DB (the one build_alias_db.py produces)."""

    def __init__(self, db_path: Path):
        if not db_path.exists():
            sys.exit(f"error: alias database not found at {db_path}")
        self.db_path = db_path

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def assembly_exists(self, assembly: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM assemblies WHERE accession = ?", (assembly,))
            return cur.fetchone() is not None
        finally:
            conn.close()

    def get_map(
        self,
        assembly: str,
        source_column: str,
        target_column: str,
    ) -> dict[str, str]:
        conn = self._connect()
        try:
            cur = conn.cursor()

            # Sanity check the assembly exists before running the lookup.
            cur.execute("SELECT 1 FROM assemblies WHERE accession = ?", (assembly,))
            if not cur.fetchone():
                raise AssemblyNotFoundError(assembly)

            # idx_accession makes this fast.
            query = f"""
                SELECT {source_column}, {target_column}
                FROM aliases
                WHERE accession = ?
                  AND {source_column} IS NOT NULL
                  AND {target_column} IS NOT NULL
            """
            try:
                cur.execute(query, (assembly,))
                rows = cur.fetchall()
            except sqlite3.OperationalError as e:
                sys.exit(f"error: SQL query failed: {e}")

            if not rows:
                raise AliasNotFoundError(
                    f"no rows for assembly {assembly!r} with both "
                    f"{source_column} and {target_column} populated"
                )

            return dict(rows)
        finally:
            conn.close()
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
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import sys


# Bumped whenever the SQLite schema changes incompatibly. Mirrored in
# build_alias_db.SCHEMA_VERSION; both must agree at runtime.
CURRENT_SCHEMA_VERSION = "2"


# Convention column names in the aliases table. Kept here (not in the
# CLI) so SqliteAliasSource can iterate over them during detection
# without the CLI having to pass the list in.
CONVENTION_COLUMNS = (
    "genbank_acc",
    "refseq_acc",
    "ucsc_name",
    "sequence_name",
    "assigned_molecule",
)


class AliasNotFoundError(Exception):
    """Raised when an assembly has no rows for the requested convention pair."""


class AssemblyNotFoundError(Exception):
    """Raised when the requested assembly accession doesn't exist in the source."""


class StaleSchemaError(Exception):
    """
    Raised when the local DB exists but was built against an older schema
    (or by a build script that didn't write _meta at all).

    Caught by bootstrap.ensure_db, which responds by forcing a rebuild.
    """
    def __init__(self, found: str | None, expected: str):
        self.found = found
        self.expected = expected
        super().__init__(
            f"DB schema is {found!r}, expected {expected!r}"
        )


class LowConfidenceDetection(Exception):
    """
    Raised when auto-detection can't pick a clear winner.

    The user must supply the corresponding flag (--from or --assembly)
    explicitly.
    """


@dataclass
class DetectionResult:
    """One auto-detection outcome with the runner-up for confidence checks."""
    winner: str
    winner_score: int
    runner_up: str | None
    runner_up_score: int


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

    @abstractmethod
    def detect_convention(self, sample_names: list[str]) -> DetectionResult:
        """
        Pick the convention column whose values best match the sample.

        For each candidate convention column, count how many of the
        sample names appear in that column (anywhere in the DB).
        Returns the winner with its score and the runner-up.

        Raises:
            LowConfidenceDetection: no convention has a clear winner
                                    (see _is_confident for the rule).
        """

    @abstractmethod
    def detect_assembly(self, sample_names: list[str]) -> DetectionResult:
        """
        Pick the assembly whose rows best match the sample names.

        For each assembly, count how many of the sample names match
        any convention column for that assembly. Returns the winner
        with its score and the runner-up.

        Raises:
            LowConfidenceDetection: no assembly has a clear winner.
        """


# Confidence rule shared by both detection methods. Conservative on
# purpose; we can loosen these after seeing real-world failure modes.
MIN_ABSOLUTE_MATCHES = 5
MIN_RATIO_OVER_RUNNER_UP = 2.0


def _is_confident(winner_score: int, runner_up_score: int) -> bool:
    """Apply the confidence rule. Centralized so both detection paths agree."""
    if winner_score < MIN_ABSOLUTE_MATCHES:
        return False
    if runner_up_score == 0:
        return True
    return (winner_score / runner_up_score) >= MIN_RATIO_OVER_RUNNER_UP


def verify_schema_version(db_path: Path) -> None:
    """
    Confirm the SQLite at db_path matches CURRENT_SCHEMA_VERSION.

    Raises StaleSchemaError if the DB exists but is stale (or pre-v2:
    lacks _meta entirely). The caller (typically bootstrap.ensure_db)
    is expected to respond by rebuilding.

    Cheap: opens the DB, runs one SELECT, closes. Safe to call before
    any other DB work.
    """
    if not db_path.exists():
        # Not stale, just absent. Caller decides whether to build.
        return
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            )
            row = cur.fetchone()
        except sqlite3.OperationalError:
            # _meta table doesn't exist — this is a pre-v2 DB.
            raise StaleSchemaError(found=None, expected=CURRENT_SCHEMA_VERSION)
        found = row[0] if row else None
        if found != CURRENT_SCHEMA_VERSION:
            raise StaleSchemaError(found=found, expected=CURRENT_SCHEMA_VERSION)
    finally:
        conn.close()


class SqliteAliasSource(AliasSource):
    """Alias source backed by a local SQLite DB (the one build_alias_db.py produces)."""

    def __init__(self, db_path: Path):
        if not db_path.exists():
            sys.exit(f"error: alias database not found at {db_path}")
        # Verify schema upfront so a stale cache surfaces as StaleSchemaError
        # rather than a confusing query error later.
        verify_schema_version(db_path)
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

    def detect_convention(self, sample_names: list[str]) -> DetectionResult:
        if not sample_names:
            raise LowConfidenceDetection("no sample names to detect from")

        placeholders = ",".join("?" * len(sample_names))
        scores: list[tuple[str, int]] = []

        conn = self._connect()
        try:
            cur = conn.cursor()
            for col in CONVENTION_COLUMNS:
                # Count how many distinct sample names appear in this column.
                # COUNT(DISTINCT) so a name that appears for many assemblies
                # counts as one match, not many.
                query = f"""
                    SELECT COUNT(DISTINCT {col})
                    FROM aliases
                    WHERE {col} IN ({placeholders})
                """
                cur.execute(query, sample_names)
                count = cur.fetchone()[0] or 0
                scores.append((col, count))
        finally:
            conn.close()

        scores.sort(key=lambda x: x[1], reverse=True)
        winner_col, winner_score = scores[0]
        runner_up_col, runner_up_score = scores[1] if len(scores) > 1 else (None, 0)

        if not _is_confident(winner_score, runner_up_score):
            raise LowConfidenceDetection(
                f"could not determine source convention from sample. "
                f"Top candidate {winner_col!r} matched {winner_score}/{len(sample_names)}, "
                f"runner-up {runner_up_col!r} matched {runner_up_score}. "
                f"Pass --from to specify explicitly."
            )

        return DetectionResult(
            winner=winner_col,
            winner_score=winner_score,
            runner_up=runner_up_col,
            runner_up_score=runner_up_score,
        )

    def detect_assembly(self, sample_names: list[str]) -> DetectionResult:
        if not sample_names:
            raise LowConfidenceDetection("no sample names to detect from")

        placeholders = ",".join("?" * len(sample_names))

        # Build the WHERE clause: a name matches if it appears in ANY
        # convention column. We bind the sample names once per column.
        column_clauses = " OR ".join(
            f"{col} IN ({placeholders})" for col in CONVENTION_COLUMNS
        )
        params = sample_names * len(CONVENTION_COLUMNS)

        query = f"""
            SELECT accession, COUNT(*) AS hits
            FROM aliases
            WHERE {column_clauses}
            GROUP BY accession
            ORDER BY hits DESC
            LIMIT 2
        """

        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            raise LowConfidenceDetection(
                f"no assembly in the database contained any of the {len(sample_names)} "
                f"sample names. The input may use names from an assembly we don't have, "
                f"or the names may be lab-internal IDs. Pass --assembly to specify."
            )

        winner, winner_score = rows[0]
        runner_up, runner_up_score = rows[1] if len(rows) > 1 else (None, 0)

        if not _is_confident(winner_score, runner_up_score):
            raise LowConfidenceDetection(
                f"could not determine assembly from sample. "
                f"Top candidate {winner!r} matched {winner_score}/{len(sample_names)}, "
                f"runner-up {runner_up!r} matched {runner_up_score}. "
                f"Pass --assembly to specify explicitly."
            )

        return DetectionResult(
            winner=winner,
            winner_score=winner_score,
            runner_up=runner_up,
            runner_up_score=runner_up_score,
        )

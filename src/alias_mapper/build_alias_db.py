"""
build_alias_db.py
-----------------
Builds a normalized SQLite alias database from a TSV produced by the
weekly collection workflow.

The same code is invoked two ways:
  - Via the CLI's bootstrap flow on first run (and on `update`)
  - Via scripts/build_alias_db.py from the GitHub Actions workflow

Schema (normalized):
  - `assemblies` table: one row per assembly. Holds accession, name,
    and last_updated. Small (~66k rows).
  - `aliases` table: one row per molecule. Holds the per-molecule
    columns plus accession as a foreign key. Big (~3M rows).
  - Single index on aliases(accession). Other indexes are intentionally
    not created — the CLI's only filter is by accession, and unused
    indexes would inflate the file by hundreds of MB for no benefit.

Destructive idempotency: the DB is dropped and rebuilt on each run.
The eventual incremental-update workflow will use INSERT instead.
"""

import csv
import gzip
import sqlite3
import sys
from datetime import date
from pathlib import Path

CREATE_ASSEMBLIES_SQL = """
CREATE TABLE assemblies (
    accession      TEXT PRIMARY KEY,
    assembly_name  TEXT,
    last_updated   TEXT
)
"""

CREATE_ALIASES_SQL = """
CREATE TABLE aliases (
    accession         TEXT NOT NULL,
    sequence_name     TEXT,
    assigned_molecule TEXT,
    genbank_acc       TEXT,
    refseq_acc        TEXT,
    ucsc_name         TEXT,
    length            INTEGER
)
"""

# Single index. The CLI always filters by accession; other columns are
# returned but not used as filters. Adding more indexes would inflate
# the DB by ~200 MB for queries the CLI never makes.
INDEX_SQL = "CREATE INDEX idx_accession ON aliases(accession)"


def open_tsv(path: Path):
    """Open the TSV, transparently handling .gz compression."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def build_db(tsv_path: Path, db_path: Path, batch_size: int = 10_000) -> None:
    """
    Build a normalized SQLite alias DB from a TSV.

    Destructive: drops the existing DB at db_path before rebuilding.
    Prints progress to stderr.

    Raises:
        FileNotFoundError: if tsv_path doesn't exist.
        ValueError:        if the TSV is missing expected columns.
    """
    tsv_path = Path(tsv_path)
    db_path = Path(db_path)

    if not tsv_path.exists():
        raise FileNotFoundError(f"TSV not found at {tsv_path}")

    if db_path.exists():
        print(f"Removing existing DB at {db_path}", file=sys.stderr)
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Creating SQLite database at {db_path}", file=sys.stderr)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Speed up the bulk insert by relaxing durability guarantees.
    cur.execute("PRAGMA journal_mode = OFF")
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA temp_store = MEMORY")

    print("Creating tables...", file=sys.stderr)
    cur.execute(CREATE_ASSEMBLIES_SQL)
    cur.execute(CREATE_ALIASES_SQL)

    today_iso = date.today().isoformat()

    insert_assembly_sql = """
        INSERT OR IGNORE INTO assemblies (accession, assembly_name, last_updated)
        VALUES (?, ?, ?)
    """
    insert_alias_sql = """
        INSERT INTO aliases (
            accession, sequence_name, assigned_molecule,
            genbank_acc, refseq_acc, ucsc_name, length
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """

    def text_or_null(v):
        v = (v or "").strip()
        return v if v else None

    rows_inserted = 0
    seen_assemblies = set()
    alias_batch = []

    print(f"Reading {tsv_path} and inserting rows...", file=sys.stderr)
    with open_tsv(tsv_path) as f:
        reader = csv.DictReader(f, delimiter="\t")

        expected_cols = [
            "ACCESSION", "ASSEMBLY_NAME", "GENBANK_ACC", "REFSEQ_ACC",
            "SEQUENCE_NAME", "ASSIGNED_MOLECULE", "UCSC_NAME", "LENGTH",
        ]
        missing = [c for c in expected_cols if c not in reader.fieldnames]
        if missing:
            conn.close()
            raise ValueError(f"TSV missing expected columns: {missing}")

        for row in reader:
            accession = text_or_null(row["ACCESSION"])
            assembly_name = text_or_null(row["ASSEMBLY_NAME"])

            if accession and accession not in seen_assemblies:
                cur.execute(
                    insert_assembly_sql,
                    (accession, assembly_name, today_iso),
                )
                seen_assemblies.add(accession)

            length_str = row["LENGTH"].strip()
            length = int(length_str) if length_str.isdigit() else None

            alias_batch.append((
                accession,
                text_or_null(row["SEQUENCE_NAME"]),
                text_or_null(row["ASSIGNED_MOLECULE"]),
                text_or_null(row["GENBANK_ACC"]),
                text_or_null(row["REFSEQ_ACC"]),
                text_or_null(row["UCSC_NAME"]),
                length,
            ))

            if len(alias_batch) >= batch_size:
                cur.executemany(insert_alias_sql, alias_batch)
                rows_inserted += len(alias_batch)
                alias_batch.clear()
                if rows_inserted % 100_000 == 0:
                    print(f"  ... {rows_inserted:>10,} rows", file=sys.stderr)

        if alias_batch:
            cur.executemany(insert_alias_sql, alias_batch)
            rows_inserted += len(alias_batch)

    print(
        f"Inserted {rows_inserted:,} alias rows across "
        f"{len(seen_assemblies):,} assemblies.",
        file=sys.stderr,
    )
    print("Creating index...", file=sys.stderr)
    cur.execute(INDEX_SQL)

    conn.commit()

    # Restore safe defaults after the bulk load.
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA synchronous = NORMAL")

    cur.execute("SELECT COUNT(*) FROM aliases")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM assemblies")
    asm_total = cur.fetchone()[0]
    conn.close()

    db_size_mb = db_path.stat().st_size / (1024 * 1024)
    print(
        f"Done.\n"
        f"  Alias rows:        {total:,}\n"
        f"  Assemblies:        {asm_total:,}\n"
        f"  DB size:           {db_size_mb:.1f} MB\n"
        f"  Path:              {db_path}",
        file=sys.stderr,
    )


def _cli():
    """Entry point when this module is run directly as a script."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tsv", type=Path, required=True,
        help="Path to the input TSV (gzipped or plain).",
    )
    parser.add_argument(
        "--db", type=Path, required=True,
        help="Path to the SQLite DB to create (overwritten if it exists).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=10_000,
        help="Number of rows to insert per transaction (default: 10000).",
    )
    args = parser.parse_args()

    try:
        build_db(args.tsv, args.db, args.batch_size)
    except FileNotFoundError as e:
        sys.exit(f"error: {e}")
    except ValueError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    _cli()

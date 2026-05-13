#!/usr/bin/env python3
"""
build_alias_db.py
-----------------
Migration script: takes the gzipped alias TSV produced by the weekly
collection workflow and builds a normalized SQLite database from it.

Schema (normalized):
  - `assemblies` table: one row per assembly. Holds accession, name,
    and last_updated. Small (~66k rows).
  - `aliases` table: one row per molecule. Holds the per-molecule
    columns plus accession as a foreign key. Big (~3M rows).
  - Single index on aliases(accession). Other indexes are intentionally
    not created — the CLI's only filter is by accession, and unused
    indexes would inflate the file by hundreds of MB for no benefit.

Usage:
    python3 scripts/build_alias_db.py --tsv data/aliases.tsv.gz \\
                                      --db data/aliases.db

Destructive idempotency: the DB is dropped and rebuilt on each run.
The eventual incremental-update workflow will use INSERT instead.
"""

import argparse
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


def open_tsv(path):
    """Open the TSV, transparently handling .gz compression."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def main():
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

    if not args.tsv.exists():
        sys.exit(f"error: TSV not found at {args.tsv}")

    if args.db.exists():
        print(f"Removing existing DB at {args.db}", file=sys.stderr)
        args.db.unlink()

    args.db.parent.mkdir(parents=True, exist_ok=True)

    print(f"Creating SQLite database at {args.db}", file=sys.stderr)
    conn = sqlite3.connect(args.db)
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
    seen_assemblies = set()  # to avoid hammering assemblies table with duplicates
    alias_batch = []

    print(f"Reading {args.tsv} and inserting rows...", file=sys.stderr)
    with open_tsv(args.tsv) as f:
        reader = csv.DictReader(f, delimiter="\t")

        expected_cols = [
            "ACCESSION", "ASSEMBLY_NAME", "GENBANK_ACC", "REFSEQ_ACC",
            "SEQUENCE_NAME", "ASSIGNED_MOLECULE", "UCSC_NAME", "LENGTH",
        ]
        missing = [c for c in expected_cols if c not in reader.fieldnames]
        if missing:
            sys.exit(f"error: TSV missing expected columns: {missing}")

        for row in reader:
            accession = text_or_null(row["ACCESSION"])
            assembly_name = text_or_null(row["ASSEMBLY_NAME"])

            # Insert into assemblies once per accession.
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

            if len(alias_batch) >= args.batch_size:
                cur.executemany(insert_alias_sql, alias_batch)
                rows_inserted += len(alias_batch)
                alias_batch.clear()
                if rows_inserted % 100_000 == 0:
                    print(f"  ... {rows_inserted:>10,} rows", file=sys.stderr)

        if alias_batch:
            cur.executemany(insert_alias_sql, alias_batch)
            rows_inserted += len(alias_batch)

    print(f"Inserted {rows_inserted:,} alias rows across {len(seen_assemblies):,} assemblies.", file=sys.stderr)
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

    db_size_mb = args.db.stat().st_size / (1024 * 1024)
    print(
        f"Done.\n"
        f"  Alias rows:        {total:,}\n"
        f"  Assemblies:        {asm_total:,}\n"
        f"  DB size:           {db_size_mb:.1f} MB\n"
        f"  Path:              {args.db}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
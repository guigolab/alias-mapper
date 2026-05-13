#!/usr/bin/env python3
"""
build_alias_db.py
-----------------
Migration script: takes the gzipped alias TSV produced by the weekly
collection workflow and builds a SQLite database from it.
 
The resulting database has:
  - One `aliases` table mirroring the TSV columns
  - A `last_updated` column for future incremental-update workflows
  - Indexes on every column the alias-mapper CLI is likely to query by
 
Usage:
    python3 scripts/build_alias_db.py --tsv data/aliases.tsv.gz \\
                                      --db data/aliases.db
 
The script is destructively idempotent: it drops and re-creates the
table on each run. That's fine for an initial build; the eventual
incremental-update workflow will use INSERT / UPDATE.
"""
 
import argparse
import csv
import gzip
import sqlite3
import sys
from datetime import date
from pathlib import Path
 
# Schema — columns mirror the TSV plus last_updated for incremental refreshes.
CREATE_TABLE_SQL = """
CREATE TABLE aliases (
    accession         TEXT NOT NULL,
    assembly_name     TEXT,
    genbank_acc       TEXT,
    refseq_acc        TEXT,
    sequence_name     TEXT,
    assigned_molecule TEXT,
    ucsc_name         TEXT,
    length            INTEGER,
    last_updated      TEXT
)
"""
 
# Indexes on each column the CLI might filter or look up by.
INDEX_SQL = [
    "CREATE INDEX idx_accession   ON aliases(accession)",
    "CREATE INDEX idx_genbank     ON aliases(genbank_acc)",
    "CREATE INDEX idx_refseq      ON aliases(refseq_acc)",
    "CREATE INDEX idx_ucsc        ON aliases(ucsc_name)",
    "CREATE INDEX idx_seqname     ON aliases(sequence_name)",
    "CREATE INDEX idx_assigned    ON aliases(assigned_molecule)",
]
 
 
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
    # Safe defaults restored after the load.
    cur.execute("PRAGMA journal_mode = OFF")
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA temp_store = MEMORY")
 
    print("Creating table and indexes...", file=sys.stderr)
    cur.execute(CREATE_TABLE_SQL)
 
    today_iso = date.today().isoformat()
 
    insert_sql = """
        INSERT INTO aliases (
            accession, assembly_name, genbank_acc, refseq_acc,
            sequence_name, assigned_molecule, ucsc_name, length,
            last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
 
    rows_inserted = 0
    batch = []
 
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
 
        def text_or_null(v):
            v = (v or "").strip()
            return v if v else None
 
        for row in reader:
            length_str = row["LENGTH"].strip()
            length = int(length_str) if length_str.isdigit() else None
 
            batch.append((
                text_or_null(row["ACCESSION"]),
                text_or_null(row["ASSEMBLY_NAME"]),
                text_or_null(row["GENBANK_ACC"]),
                text_or_null(row["REFSEQ_ACC"]),
                text_or_null(row["SEQUENCE_NAME"]),
                text_or_null(row["ASSIGNED_MOLECULE"]),
                text_or_null(row["UCSC_NAME"]),
                length,
                today_iso,
            ))
 
            if len(batch) >= args.batch_size:
                cur.executemany(insert_sql, batch)
                rows_inserted += len(batch)
                batch.clear()
                if rows_inserted % 100_000 == 0:
                    print(f"  ... {rows_inserted:>10,} rows", file=sys.stderr)
 
        if batch:
            cur.executemany(insert_sql, batch)
            rows_inserted += len(batch)
 
    print(f"Inserted {rows_inserted:,} rows. Creating indexes...", file=sys.stderr)
    for stmt in INDEX_SQL:
        cur.execute(stmt)
 
    # Commit before changing journal_mode / synchronous (they cannot
    # be modified inside a transaction).
    conn.commit()
 
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA synchronous = NORMAL")
 
    cur.execute("SELECT COUNT(*) FROM aliases")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT accession) FROM aliases")
    unique_assemblies = cur.fetchone()[0]
    conn.close()
 
    db_size_mb = args.db.stat().st_size / (1024 * 1024)
    print(
        f"Done.\n"
        f"  Rows:               {total:,}\n"
        f"  Unique assemblies:  {unique_assemblies:,}\n"
        f"  DB size:            {db_size_mb:.1f} MB\n"
        f"  Path:               {args.db}",
        file=sys.stderr,
    )
 
 
if __name__ == "__main__":
    main()
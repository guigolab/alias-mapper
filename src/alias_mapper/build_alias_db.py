"""
build_alias_db.py
-----------------
Builds a SQLite alias database from a TSV produced by the weekly
collection workflow.

The TSV is the merged-row format (schema v2): one row per assembly,
with per-molecule data as comma-separated, position-aligned list
columns. This builder explodes those lists back into per-molecule
rows for indexed lookup at query time. The TSV is the human-readable
source of truth; the DB is whatever shape is fastest for queries.

The same code is invoked two ways:
  - Via the CLI's bootstrap flow on first run (and on `update`)
  - Via scripts/build_alias_db.py from the GitHub Actions workflow

Schema:
  - `_meta`: schema version + build date. Used by SqliteAliasSource
    to detect stale caches and trigger rebuild.
  - `assemblies`: one row per assembly. Holds assembly-level metadata
    (organism, taxid, level, etc) plus the paired RefSeq assembly
    accession when known. Small (~50k rows).
  - `aliases`: one row per molecule (exploded from the TSV's list
    columns). Holds per-molecule names. Big (~3M rows).
  - Single index on aliases(accession). Other indexes are intentionally
    not created — the CLI's only filter is by accession, and unused
    indexes would inflate the file by hundreds of MB for no benefit.

Destructive idempotency: the DB is dropped and rebuilt on each run.
"""

import csv
import gzip
import sqlite3
import sys
from datetime import date
from pathlib import Path

# Bumped whenever the SQLite schema changes incompatibly. Kept here
# rather than imported from alias_source to avoid a circular import
# at script-invocation time; alias_source imports the same number.
#
# Stored as int in `_meta` so future versions can compare numerically
# ("is this cache older than v3?") without string-vs-numeric pitfalls.
SCHEMA_VERSION = 2

CREATE_META_SQL = """
CREATE TABLE _meta (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""

CREATE_ASSEMBLIES_SQL = """
CREATE TABLE assemblies (
    accession           TEXT PRIMARY KEY,    -- assembly-level GenBank acc (GCA_*)
    assembly_name       TEXT,
    paired_refseq_acc   TEXT,                -- assembly-level RefSeq acc (GCF_*), if paired
    taxid               INTEGER,
    organism_name       TEXT,
    group_name          TEXT,                -- "group" is a reserved word in SQL
    assembly_level      TEXT,
    last_updated        TEXT
)
"""

CREATE_ALIASES_SQL = """
CREATE TABLE aliases (
    accession         TEXT NOT NULL,         -- FK to assemblies.accession
    position          INTEGER NOT NULL,      -- 0-based, longest molecule first
    sequence_name     TEXT,
    assigned_molecule TEXT,
    genbank_acc       TEXT,                  -- per-sequence (e.g. CM000663.2)
    refseq_acc        TEXT,                  -- per-sequence (e.g. NC_000001.11)
    ucsc_name         TEXT,
    length            INTEGER
)
"""

# Single index. The CLI always filters by accession; other columns are
# returned but not used as filters. Adding more indexes would inflate
# the DB by ~200 MB for queries the CLI never makes.
INDEX_SQL = "CREATE INDEX idx_accession ON aliases(accession)"

# Expected TSV column set (schema v2 merged-row format).
EXPECTED_TSV_COLS = [
    "genbank_acc", "refseq_acc", "assembly_name", "taxid",
    "organism_name", "group", "assembly_level",
    "sequence_names", "genbank_seq_accs", "refseq_seq_accs",
    "ucsc_names", "assigned_molecules", "lengths",
]

# Schema-v1 columns. Used only for detection — if the TSV looks like
# the old per-molecule format, we want to give the user a useful error
# instead of a wall of column names.
V1_TSV_COLS_MARKER = {"ACCESSION", "GENBANK_ACC", "REFSEQ_ACC", "LENGTH"}

# Which TSV list columns hold per-molecule data, and what SQLite column
# each maps to. The first element of each tuple is the TSV column, the
# second is the SQLite alias column.
LIST_COLUMN_MAP = [
    ("sequence_names",     "sequence_name"),
    ("assigned_molecules", "assigned_molecule"),
    ("genbank_seq_accs",   "genbank_acc"),
    ("refseq_seq_accs",    "refseq_acc"),
    ("ucsc_names",         "ucsc_name"),
    ("lengths",             "length"),
]


def open_tsv(path: Path):
    """Open the TSV, transparently handling .gz compression."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def _text_or_none(v):
    """Empty/whitespace -> None; otherwise stripped."""
    v = (v or "").strip()
    return v if v else None


def _int_or_none(v):
    """Best-effort integer parse; non-digit (incl empty) -> None."""
    v = (v or "").strip()
    if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
        try:
            return int(v)
        except ValueError:
            return None
    return None


def _explode_row(row: dict[str, str]) -> tuple[dict, list[tuple]]:
    """
    Take one merged TSV row and return:
      - assembly_record: dict ready for the assemblies INSERT
      - molecule_tuples: list of tuples ready for the aliases INSERT,
        one per molecule, in position order.

    Validates that all list columns have the same length within this
    row. Mismatched-length lists indicate a TSV bug worth failing on.
    """
    accession = _text_or_none(row["genbank_acc"])
    if not accession:
        # The TSV writer should never produce a row without a genbank_acc.
        # If it ever does, fail loudly rather than silently drop.
        raise ValueError(f"TSV row missing genbank_acc: {row!r}")

    assembly_record = {
        "accession":         accession,
        "assembly_name":     _text_or_none(row["assembly_name"]),
        "paired_refseq_acc": _text_or_none(row["refseq_acc"]),
        "taxid":             _int_or_none(row["taxid"]),
        "organism_name":     _text_or_none(row["organism_name"]),
        "group_name":        _text_or_none(row["group"]),
        "assembly_level":    _text_or_none(row["assembly_level"]),
    }

    # Split each list column on commas. An empty cell yields [""], which
    # split() would treat as one empty molecule — guard against that by
    # treating the whole list as empty if every entry is empty.
    split_lists = {}
    for tsv_col, _sql_col in LIST_COLUMN_MAP:
        raw = row.get(tsv_col, "") or ""
        if raw == "":
            split_lists[tsv_col] = []
        else:
            split_lists[tsv_col] = raw.split(",")

    # Validate position alignment. All non-empty lists must have the
    # same length. (Empty lists are tolerated for fields that genuinely
    # have no data, e.g. UCSC names absent for non-vertebrates.)
    lengths = {col: len(vals) for col, vals in split_lists.items() if vals}
    if lengths:
        canonical = next(iter(lengths.values()))
        if not all(v == canonical for v in lengths.values()):
            raise ValueError(
                f"TSV row for {accession} has misaligned list lengths: {lengths}"
            )
        n_molecules = canonical
    else:
        n_molecules = 0

    molecule_tuples = []
    for pos in range(n_molecules):
        def _at(tsv_col):
            vals = split_lists[tsv_col]
            return vals[pos] if pos < len(vals) else ""

        molecule_tuples.append((
            accession,
            pos,
            _text_or_none(_at("sequence_names")),
            _text_or_none(_at("assigned_molecules")),
            _text_or_none(_at("genbank_seq_accs")),
            _text_or_none(_at("refseq_seq_accs")),
            _text_or_none(_at("ucsc_names")),
            _int_or_none(_at("lengths")),
        ))

    return assembly_record, molecule_tuples


def build_db(tsv_path: Path, db_path: Path, batch_size: int = 10_000) -> None:
    """
    Build a SQLite alias DB from a merged-row TSV (schema v2).

    Destructive: drops the existing DB at db_path before rebuilding.
    Prints progress to stderr.

    Raises:
        FileNotFoundError: if tsv_path doesn't exist.
        ValueError:        if the TSV is missing expected columns or
                           has misaligned position-aligned lists.
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
    cur.execute(CREATE_META_SQL)
    cur.execute(CREATE_ASSEMBLIES_SQL)
    cur.execute(CREATE_ALIASES_SQL)

    today_iso = date.today().isoformat()

    # Write meta first so a corrupted/partial DB is still recognizable
    # as the right schema version (downstream rebuild can be confident).
    cur.execute("INSERT INTO _meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)))
    cur.execute("INSERT INTO _meta (key, value) VALUES (?, ?)",
                ("build_date", today_iso))

    insert_assembly_sql = """
        INSERT OR IGNORE INTO assemblies (
            accession, assembly_name, paired_refseq_acc, taxid,
            organism_name, group_name, assembly_level, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    insert_alias_sql = """
        INSERT INTO aliases (
            accession, position, sequence_name, assigned_molecule,
            genbank_acc, refseq_acc, ucsc_name, length
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    n_assemblies = 0
    n_aliases = 0
    alias_batch: list[tuple] = []

    print(f"Reading {tsv_path} and inserting rows...", file=sys.stderr)
    with open_tsv(tsv_path) as f:
        reader = csv.DictReader(f, delimiter="\t")

        missing = [c for c in EXPECTED_TSV_COLS if c not in reader.fieldnames]
        if missing:
            conn.close()
            # Detect the v1 schema specifically so we can give a useful
            # upgrade message instead of dumping column lists.
            found_set = set(reader.fieldnames or [])
            if V1_TSV_COLS_MARKER.issubset(found_set):
                raise ValueError(
                    f"TSV at {tsv_path} uses schema v1 (per-molecule rows), "
                    f"but this CLI expects schema v2 (merged-row). This "
                    f"usually means the data release predates the schema "
                    f"upgrade. Either wait for the next weekly data release, "
                    f"or downgrade the CLI to a v1-compatible version."
                )
            raise ValueError(
                f"TSV missing expected columns: {missing}. "
                f"Found: {reader.fieldnames}"
            )

        for row in reader:
            assembly_record, molecule_tuples = _explode_row(row)

            cur.execute(insert_assembly_sql, (
                assembly_record["accession"],
                assembly_record["assembly_name"],
                assembly_record["paired_refseq_acc"],
                assembly_record["taxid"],
                assembly_record["organism_name"],
                assembly_record["group_name"],
                assembly_record["assembly_level"],
                today_iso,
            ))
            n_assemblies += 1

            alias_batch.extend(molecule_tuples)

            if len(alias_batch) >= batch_size:
                cur.executemany(insert_alias_sql, alias_batch)
                n_aliases += len(alias_batch)
                alias_batch.clear()
                if n_aliases % 100_000 == 0:
                    print(f"  ... {n_aliases:>10,} molecule rows", file=sys.stderr)

        if alias_batch:
            cur.executemany(insert_alias_sql, alias_batch)
            n_aliases += len(alias_batch)

    print(
        f"Inserted {n_aliases:,} molecule rows across "
        f"{n_assemblies:,} assemblies.",
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
        f"  Schema version:    {SCHEMA_VERSION}\n"
        f"  Molecule rows:     {total:,}\n"
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

#!/usr/bin/env python3
"""
alias_mapper.py (v0.1)
----------------------
Translates the chromosome / scaffold names in column 1 of a GFF file
from one naming convention to another, using a SQLite alias database
produced by build_alias_db.py.

v0.1 differs from v0 only in the data source: v0 loaded a gzipped TSV
and filtered to one assembly in Python; v0.1 issues one SQL query that
returns only the rows for the chosen assembly (using the index on
ACCESSION). Same CLI, same output.

Usage:
    python3 src/alias_mapper.py convert INPUT.gff \\
        --from refseq --to ucsc \\
        --assembly GCF_000001405.40 \\
        -o OUTPUT.gff
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# Short names exposed on the CLI mapped to columns in the SQLite table.
CONVENTIONS = {
    "genbank":           "genbank_acc",
    "refseq":            "refseq_acc",
    "ucsc":              "ucsc_name",
    "sequence-name":     "sequence_name",
    "assigned-molecule": "assigned_molecule",
}

# Default path: <repo>/data/aliases.db (resolved relative to this script).
DEFAULT_ALIAS_DB = (
    Path(__file__).resolve().parent.parent / "data" / "aliases.db"
)


def load_alias_table(db_path, source_col, target_col, assembly):
    """
    Returns a dict { source_name -> target_name } for one assembly.

    One SQL query, indexed on `accession`. Rows where either the source
    or target column is empty are skipped.
    """
    if not db_path.exists():
        sys.exit(f"error: alias database not found at {db_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    query = f"""
        SELECT {source_col}, {target_col}
        FROM aliases
        WHERE accession = ?
          AND {source_col} IS NOT NULL
          AND {target_col} IS NOT NULL
    """
    try:
        cur.execute(query, (assembly,))
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        sys.exit(f"error: SQL query failed: {e}")
    finally:
        conn.close()

    if not rows:
        sys.exit(
            f"error: no rows found for assembly {assembly!r} with both "
            f"{source_col} and {target_col} populated. "
            f"Check the --assembly value and that the database includes it."
        )

    return dict(rows)


def translate_gff_line(line, alias_map, stats):
    """Translate one GFF line. Comments and blank lines pass through."""
    if not line or line.startswith("#"):
        return line

    parts = line.rstrip("\n").split("\t")
    if len(parts) < 1:
        return line

    seq_name = parts[0]
    new_name = alias_map.get(seq_name)
    if new_name is None:
        stats["unmapped"] += 1
        stats["unmapped_examples"].add(seq_name)
        return line

    parts[0] = new_name
    stats["mapped"] += 1
    return "\t".join(parts) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Translate sequence names in a GFF file (v0.1)."
    )
    parser.add_argument(
        "command", choices=["convert"],
        help="Subcommand to run (v0.1 only supports 'convert').",
    )
    parser.add_argument("input", type=Path, help="Path to the input GFF file.")
    parser.add_argument(
        "--from", dest="src", required=True, choices=CONVENTIONS.keys(),
        help="Source naming convention.",
    )
    parser.add_argument(
        "--to", dest="tgt", required=True, choices=CONVENTIONS.keys(),
        help="Target naming convention.",
    )
    parser.add_argument(
        "--assembly", required=True,
        help="Assembly accession (e.g. GCF_000001405.40) to scope the lookup.",
    )
    parser.add_argument(
        "-o", "--output", type=Path, required=True,
        help="Path to write the translated GFF file.",
    )
    parser.add_argument(
        "--alias-db", type=Path, default=DEFAULT_ALIAS_DB,
        help=f"Path to the alias SQLite database (default: {DEFAULT_ALIAS_DB}).",
    )
    args = parser.parse_args()

    if args.src == args.tgt:
        sys.exit("error: --from and --to must be different conventions.")

    if not args.input.exists():
        sys.exit(f"error: input file not found: {args.input}")

    if args.output.exists():
        sys.exit(
            f"error: output file already exists: {args.output} "
            f"(refusing to overwrite — choose another path or delete it first)"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)

    src_col = CONVENTIONS[args.src]
    tgt_col = CONVENTIONS[args.tgt]

    print(
        f"Loading alias table from {args.alias_db}\n"
        f"  assembly={args.assembly}, from={args.src}, to={args.tgt}",
        file=sys.stderr,
    )
    alias_map = load_alias_table(args.alias_db, src_col, tgt_col, args.assembly)
    print(f"  -> {len(alias_map)} entries loaded", file=sys.stderr)

    stats = {"mapped": 0, "unmapped": 0, "unmapped_examples": set()}
    print(f"Translating {args.input} → {args.output}", file=sys.stderr)

    with open(args.input, "r", encoding="utf-8") as in_f, \
         open(args.output, "w", encoding="utf-8") as out_f:
        for line in in_f:
            out_f.write(translate_gff_line(line, alias_map, stats))

    print(
        f"Done. mapped={stats['mapped']}, unmapped={stats['unmapped']}",
        file=sys.stderr,
    )
    if stats["unmapped"]:
        examples = sorted(stats["unmapped_examples"])[:5]
        print(
            f"  warning: {stats['unmapped']} rows had sequence names not found "
            f"in the alias database for assembly {args.assembly}. "
            f"Examples: {examples}. These rows were passed through unchanged.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
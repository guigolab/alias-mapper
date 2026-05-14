#!/usr/bin/env python3
"""
alias_mapper.py (v0.2)
----------------------
Translates the chromosome / scaffold names in column 1 of a GFF file
from one naming convention to another, using an alias source (SQLite
DB today; HTTP API in the future).

Usage:
    python3 src/alias_mapper.py convert INPUT.gff \\
        --from refseq --to ucsc \\
        --assembly GCF_000001405.40 \\
        -o OUTPUT.gff
"""

import argparse
import sys
from pathlib import Path

from alias_source import (
    SqliteAliasSource,
    AssemblyNotFoundError,
    AliasNotFoundError,
)
from formats import translator_for

# Short names exposed on the CLI mapped to columns in the aliases table.
CONVENTIONS = {
    "genbank":           "genbank_acc",
    "refseq":            "refseq_acc",
    "ucsc":              "ucsc_name",
    "sequence-name":     "sequence_name",
    "assigned-molecule": "assigned_molecule",
}

DEFAULT_ALIAS_DB = (
    Path(__file__).resolve().parent.parent / "data" / "aliases.db"
)


def main():
    parser = argparse.ArgumentParser(
        description="Translate sequence names in a GFF/GTF/FASTA file."
    )
    parser.add_argument(
        "command", choices=["convert"],
        help="Subcommand to run (v0.2 only supports 'convert').",
    )
    parser.add_argument("input", type=Path, help="Path to the input file (GFF, GTF, or FASTA).")
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
        help="Path to write the translated file.",
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

    # Pick a translator based on the input file's extension.
    try:
        translator = translator_for(args.input)
    except ValueError as e:
        sys.exit(f"error: {e}")

    src_col = CONVENTIONS[args.src]
    tgt_col = CONVENTIONS[args.tgt]

    print(
        f"Loading alias table from {args.alias_db}\n"
        f"  assembly={args.assembly}, from={args.src}, to={args.tgt}",
        file=sys.stderr,
    )

    source = SqliteAliasSource(args.alias_db)
    try:
        alias_map = source.get_map(args.assembly, src_col, tgt_col)
    except AssemblyNotFoundError:
        sys.exit(
            f"error: assembly {args.assembly!r} not found in the database. "
            f"Check the --assembly value."
        )
    except AliasNotFoundError as e:
        sys.exit(
            f"error: {e}. "
            f"This assembly may not have aliases in those conventions."
        )

    print(f"  -> {len(alias_map)} entries loaded", file=sys.stderr)

    stats = {"mapped": 0, "unmapped": 0, "unmapped_examples": set()}
    print(f"Translating {args.input} → {args.output}", file=sys.stderr)

    with open(args.input, "r", encoding="utf-8") as in_f, \
         open(args.output, "w", encoding="utf-8") as out_f:
        for line in in_f:
            out_f.write(translator.translate_line(line, alias_map, stats))

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

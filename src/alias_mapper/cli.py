#!/usr/bin/env python3
"""
cli.py
------
Command-line entry point for alias-mapper.

Translates the chromosome / scaffold names in GFF, GTF, or FASTA files
from one naming convention to another, using an alias source (SQLite
DB today; HTTP API in the future).

If --from or --assembly is omitted, the tool samples the input file
and auto-detects the convention and/or assembly from the database.

Subcommands:
    convert   Translate one file from one convention to another.
    update    Re-download the latest alias data and rebuild the cache.

The first time `convert` is run, the tool downloads the latest alias
TSV from GitHub Releases and builds a local SQLite database in the
platform cache directory. Subsequent invocations use that cache.
Run `update` manually to refresh the cache with newer data.

Usage:
    alias-mapper convert INPUT.gff --to ucsc -o OUTPUT.gff
    alias-mapper convert INPUT.gff --from refseq --to ucsc \\
        --assembly GCF_000001405.40 -o OUTPUT.gff
    alias-mapper update
"""

import argparse
import sys
from pathlib import Path

from .alias_source import (
    SqliteAliasSource,
    AssemblyNotFoundError,
    AliasNotFoundError,
    LowConfidenceDetection,
)
from .formats import translator_for
from .bootstrap import (
    BootstrapError,
    default_cache_path,
    ensure_db,
)

# Short names exposed on the CLI mapped to columns in the aliases table.
CONVENTIONS = {
    "genbank":           "genbank_acc",
    "refseq":            "refseq_acc",
    "ucsc":              "ucsc_name",
    "sequence-name":     "sequence_name",
    "assigned-molecule": "assigned_molecule",
}

# Reverse lookup: column name -> CLI-facing convention name. Used to
# report auto-detection results back to the user in their vocabulary.
COLUMN_TO_CONVENTION = {v: k for k, v in CONVENTIONS.items()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alias-mapper",
        description="Translate sequence names in a GFF/GTF/FASTA file.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # convert ---------------------------------------------------------------
    p_convert = subparsers.add_parser(
        "convert",
        help="Translate one file from one naming convention to another.",
    )
    p_convert.add_argument(
        "input", type=Path,
        help="Path to the input file (GFF, GTF, or FASTA).",
    )
    p_convert.add_argument(
        "--from", dest="src", choices=CONVENTIONS.keys(),
        help="Source naming convention. Auto-detected if omitted.",
    )
    p_convert.add_argument(
        "--to", dest="tgt", required=True, choices=CONVENTIONS.keys(),
        help="Target naming convention.",
    )
    p_convert.add_argument(
        "--assembly",
        help="Assembly accession (e.g. GCF_000001405.40). Auto-detected if omitted.",
    )
    p_convert.add_argument(
        "-o", "--output", type=Path, required=True,
        help="Path to write the translated file.",
    )
    p_convert.add_argument(
        "--alias-db", type=Path, default=None,
        help=(
            "Path to the alias SQLite database. Defaults to the platform cache "
            "location (created on first run if missing)."
        ),
    )

    # update ----------------------------------------------------------------
    p_update = subparsers.add_parser(
        "update",
        help="Re-download the latest alias data and rebuild the local cache.",
    )
    p_update.add_argument(
        "--alias-db", type=Path, default=None,
        help=(
            "Path to the alias SQLite database to refresh. Defaults to the "
            "platform cache location."
        ),
    )

    return parser


def cmd_update(args) -> int:
    """Force a rebuild of the local alias DB from the latest TSV."""
    try:
        path = ensure_db(args.alias_db, force=True)
    except BootstrapError as e:
        sys.exit(f"error: {e}")
    print(f"Done. Local alias database is up to date at {path}", file=sys.stderr)
    return 0


def cmd_convert(args) -> int:
    """Translate one file from --from to --to."""
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

    # Ensure the local DB exists. On first run this downloads + builds it.
    # If --alias-db was explicitly passed and doesn't exist, that's a user
    # error: don't surprise them by auto-downloading to a path they chose.
    if args.alias_db is not None and not args.alias_db.exists():
        sys.exit(
            f"error: alias database not found at {args.alias_db}. "
            f"Either omit --alias-db to use the cached default, or run "
            f"`alias-mapper update --alias-db {args.alias_db}` to create it there."
        )

    try:
        db_path = ensure_db(args.alias_db)
    except BootstrapError as e:
        sys.exit(f"error: {e}")

    source = SqliteAliasSource(db_path)

    # Auto-detect what's missing. Only sample the input file if we
    # actually need to — if both --from and --assembly are given,
    # we skip the sampling entirely.
    sample = None
    if args.src is None or args.assembly is None:
        sample = translator.sample_names(args.input)
        if not sample:
            sys.exit(
                f"error: no sequence names found in {args.input} for auto-detection. "
                f"Pass --from and --assembly explicitly."
            )
        print(
            f"Sampled {len(sample)} unique sequence names from {args.input} "
            f"for auto-detection.",
            file=sys.stderr,
        )

    # Resolve --from.
    if args.src is None:
        try:
            result = source.detect_convention(sample)
        except LowConfidenceDetection as e:
            sys.exit(f"error: {e}")
        src_col = result.winner
        src_name = COLUMN_TO_CONVENTION.get(src_col, src_col)
        print(
            f"  detected source convention: {src_name} "
            f"({result.winner_score}/{len(sample)} matches, "
            f"runner-up {result.runner_up_score})",
            file=sys.stderr,
        )
    else:
        src_col = CONVENTIONS[args.src]
        src_name = args.src

    # Resolve --assembly.
    if args.assembly is None:
        try:
            result = source.detect_assembly(sample)
        except LowConfidenceDetection as e:
            sys.exit(f"error: {e}")
        assembly = result.winner
        print(
            f"  detected assembly: {assembly} "
            f"({result.winner_score}/{len(sample)} matches, "
            f"runner-up {result.runner_up_score})",
            file=sys.stderr,
        )
    else:
        assembly = args.assembly

    tgt_col = CONVENTIONS[args.tgt]

    if src_col == tgt_col:
        sys.exit(
            f"error: source and target conventions are the same ({src_name}). "
            f"Nothing to translate."
        )

    print(
        f"Loading alias table from {db_path}\n"
        f"  assembly={assembly}, from={src_name}, to={args.tgt}",
        file=sys.stderr,
    )

    try:
        alias_map = source.get_map(assembly, src_col, tgt_col)
    except AssemblyNotFoundError:
        sys.exit(
            f"error: assembly {assembly!r} not found in the database. "
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
            f"in the alias database for assembly {assembly}. "
            f"Examples: {examples}. These rows were passed through unchanged.",
            file=sys.stderr,
        )
    return 0


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "convert":
        return cmd_convert(args)
    elif args.command == "update":
        return cmd_update(args)
    else:
        parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())

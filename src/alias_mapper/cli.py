#!/usr/bin/env python3
"""
cli.py
------
Command-line entry point for alias-mapper.

Translates the chromosome / scaffold names in GFF, GTF, or FASTA files
from one naming convention to another, using an alias source (SQLite
DB today; HTTP API in the future).

Modes of `convert`:

  Single file:
      alias-mapper convert INPUT --to ucsc -o OUTPUT
  Multi file, conform (omit --to): conform the annotations to whatever
  convention the reference FASTA is already in; the FASTA is left
  untouched:
      alias-mapper convert --fasta REF.fa ANN1.gff ANN2.gtf --out-dir OUT/
  Multi file, overwrite (--overwrite-to): convert the FASTA and every
  annotation into one chosen convention:
      alias-mapper convert --fasta REF.fa ANN1.gff --overwrite-to ucsc --out-dir OUT/

In multi-file mode the assembly is detected ONCE from the FASTA. Conform
mode then maps any recognized name (in any convention) to the FASTA's own
convention, matching the common workflow where you have one genome and
want its annotations to line up with it. Overwrite mode instead detects
the shared source convention from the FASTA and forces everything to the
target. Outputs are written to --out-dir as `<stem>.<conv>.<ext>` (gzip
preserved); in conform mode the FASTA itself is not written, since it is
unchanged.

Input files may be gzipped: compression is detected from contents, and
output is gzipped when the chosen path ends in .gz.

If --from or --assembly is omitted, the tool samples the input (or the
FASTA, in multi-file mode) and auto-detects from the database.

Subcommands:
    convert   Translate one file, or a FASTA + its annotation files.
    update    Re-download the latest alias data and rebuild the cache.

On first run `convert` downloads the latest alias TSV from GitHub
Releases and builds a local SQLite database in the platform cache
directory; later invocations reuse it. Run `update` to refresh.

Usage:
    alias-mapper convert INPUT.gff --to ucsc -o OUTPUT.gff
    alias-mapper convert INPUT.gff.gz --to ucsc -o OUTPUT.gff.gz
    alias-mapper convert --fasta REF.fa ann1.gff ann2.gtf --out-dir out/
    alias-mapper convert --fasta REF.fa ann1.gff --overwrite-to ucsc --out-dir out/
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
    CONVENTION_COLUMNS,
)
from .formats import translator_for, open_text_read, open_text_write
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
        description="Translate sequence names in GFF/GTF/FASTA files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # convert ---------------------------------------------------------------
    p_convert = subparsers.add_parser(
        "convert",
        help="Translate one file, or a FASTA plus its annotation files.",
        description=(
            "Single-file:        convert INPUT --to TGT -o OUT\n"
            "Multi-file conform: convert --fasta REF [ANN ...] --out-dir DIR\n"
            "Multi-file force:   convert --fasta REF [ANN ...] --overwrite-to TGT --out-dir DIR\n\n"
            "In multi-file mode the assembly is detected once from the FASTA. "
            "Without --overwrite-to, the annotations are conformed to the "
            "FASTA's own convention and the FASTA is left unchanged. With "
            "--overwrite-to, the FASTA and every annotation are forced to the "
            "target convention."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_convert.add_argument(
        "input", type=Path, nargs="*",
        help=(
            "Single-file mode: one input file (GFF, GTF, or FASTA; optionally "
            ".gz). Multi-file mode (with --fasta): the annotation files to "
            "convert alongside the FASTA."
        ),
    )
    p_convert.add_argument(
        "--fasta", type=Path, default=None,
        help=(
            "Reference FASTA. Enables multi-file mode: detect the assembly "
            "from this FASTA, then conform the annotation inputs to its "
            "convention (or, with --overwrite-to, force everything to a "
            "chosen convention)."
        ),
    )
    p_convert.add_argument(
        "--from", dest="src", choices=CONVENTIONS.keys(),
        help=(
            "Source naming convention. Auto-detected if omitted. Not used in "
            "conform mode (the FASTA's convention is the target there)."
        ),
    )
    p_convert.add_argument(
        "--to", dest="tgt", choices=CONVENTIONS.keys(),
        help=(
            "Target naming convention. Required in single-file mode. In "
            "--fasta mode use --overwrite-to instead (or omit to conform)."
        ),
    )
    p_convert.add_argument(
        "--overwrite-to", dest="overwrite_to", choices=CONVENTIONS.keys(),
        help=(
            "(--fasta mode) Force the FASTA and all annotations to this "
            "convention. Omit to conform the annotations to the FASTA's own "
            "convention, leaving the FASTA unchanged."
        ),
    )
    p_convert.add_argument(
        "--assembly",
        help="Assembly accession (e.g. GCF_000001405.40). Auto-detected if omitted.",
    )
    p_convert.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output path (single-file mode only; gzipped if it ends in .gz).",
    )
    p_convert.add_argument(
        "--out-dir", dest="out_dir", type=Path, default=None,
        help=(
            "Output directory (multi-file/--fasta mode only). Each converted "
            "input is written as <stem>.<conv>.<ext>, preserving any .gz."
        ),
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


def _open_source(args):
    """Resolve --alias-db (or the cached default) and open a SqliteAliasSource."""
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
    return SqliteAliasSource(db_path), db_path


def _resolve_from_assembly(source, sample_path, args, role="source convention"):
    """
    Determine (conv_col, conv_name, assembly), sampling names from
    `sample_path` to auto-detect whichever of --from / --assembly was
    omitted. In multi-file mode `sample_path` is the FASTA, so detection
    happens once and is reused for every annotation file.

    `role` only changes the label printed for the detected convention,
    so conform mode can report it as the target rather than the source.
    """
    translator = translator_for(sample_path)
    sample = None
    if args.src is None or args.assembly is None:
        sample = translator.sample_names(sample_path)
        if not sample:
            sys.exit(
                f"error: no sequence names found in {sample_path} for auto-detection. "
                f"Pass --from and --assembly explicitly."
            )
        print(
            f"Sampled {len(sample)} unique sequence names from {sample_path} "
            f"for auto-detection.",
            file=sys.stderr,
        )

    if args.src is None:
        try:
            result = source.detect_convention(sample)
        except LowConfidenceDetection as e:
            sys.exit(f"error: {e}")
        conv_col = result.winner
        conv_name = COLUMN_TO_CONVENTION.get(conv_col, conv_col)
        print(
            f"  detected {role}: {conv_name} "
            f"({result.winner_score}/{len(sample)} matches, "
            f"runner-up {result.runner_up_score})",
            file=sys.stderr,
        )
    else:
        conv_col = CONVENTIONS[args.src]
        conv_name = args.src

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

    return conv_col, conv_name, assembly


def _load_map(source, assembly, src_col, src_name, tgt_col):
    """Fetch the {source_name -> target_name} map for one assembly."""
    if src_col == tgt_col:
        sys.exit(
            f"error: source and target conventions are the same ({src_name}). "
            f"Nothing to translate."
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
            f"error: {e}. This assembly may not have aliases in those conventions."
        )
    print(f"  -> {len(alias_map)} entries loaded", file=sys.stderr)
    return alias_map


def _load_conform_map(source, assembly, target_col, target_name):
    """
    Build a {any_convention_name -> target_name} map for conform mode.

    Merges get_map() across every convention column except the target,
    so a name in any recognized convention resolves to the FASTA's
    convention. Convention columns with no rows paired to the target for
    this assembly are skipped.

    This is built from the existing one-source/one-target get_map, so it
    needs no change to the AliasSource interface. A consequence: names
    that are *already* in the target convention are not keys here, so
    they pass through unchanged (the correct output) but land in the
    "unmapped" tally — see the conform-mode note in _translate_file.
    """
    if not source.assembly_exists(assembly):
        sys.exit(
            f"error: assembly {assembly!r} not found in the database. "
            f"Check the --assembly value."
        )
    conform_map: dict[str, str] = {}
    contributing: list[str] = []
    for col in CONVENTION_COLUMNS:
        if col == target_col:
            continue
        try:
            partial = source.get_map(assembly, col, target_col)
        except AliasNotFoundError:
            # This convention has no rows paired with the target for this
            # assembly (e.g. RefSeq/UCSC absent). Nothing to contribute.
            continue
        conform_map.update(partial)
        contributing.append(COLUMN_TO_CONVENTION.get(col, col))

    if contributing:
        print(
            f"  conform map: {len(conform_map)} names -> {target_name} "
            f"(from {', '.join(contributing)})",
            file=sys.stderr,
        )
    else:
        print(
            f"  warning: no other convention has data paired to {target_name} "
            f"for this assembly; nothing can be conformed. Annotation names "
            f"already in {target_name} will pass through unchanged.",
            file=sys.stderr,
        )
    return conform_map


def _translate_file(in_path: Path, out_path: Path, alias_map: dict,
                    conform_target: str | None = None) -> dict:
    """
    Translate one file with a prepared alias map. Returns its stats.

    When `conform_target` is set (conform mode), the passthrough message
    is worded as a neutral note rather than a warning: a name that is
    already in the target convention is not in the conform map and so is
    correctly left unchanged, which is not an error.
    """
    translator = translator_for(in_path)
    stats = {"mapped": 0, "unmapped": 0, "unmapped_examples": set()}
    print(f"Translating {in_path} → {out_path}", file=sys.stderr)
    with open_text_read(in_path) as in_f, open_text_write(out_path) as out_f:
        for line in in_f:
            out_f.write(translator.translate_line(line, alias_map, stats))
    print(
        f"  {in_path.name}: mapped={stats['mapped']}, unmapped={stats['unmapped']}",
        file=sys.stderr,
    )
    if stats["unmapped"]:
        examples = sorted(stats["unmapped_examples"])[:5]
        if conform_target is not None:
            print(
                f"  note: {stats['unmapped']} names in {in_path.name} were already "
                f"in {conform_target} convention or not recognized; passed through "
                f"unchanged. Examples: {examples}",
                file=sys.stderr,
            )
        else:
            print(
                f"  warning: {stats['unmapped']} names in {in_path.name} not found in "
                f"the alias database for this assembly; passed through unchanged. "
                f"Examples: {examples}",
                file=sys.stderr,
            )
    return stats


def _output_name(in_path: Path, to: str) -> str:
    """
    Build the multi-file output filename: insert `.<to>` before the
    extension(s), preserving a trailing .gz.

    genome.fa.gz -> genome.<to>.fa.gz ; ann1.gff -> ann1.<to>.gff
    """
    p = Path(in_path)
    if p.suffix.lower() == ".gz":
        base = Path(p.stem).stem
        exts = Path(p.stem).suffix + p.suffix
    else:
        base = p.stem
        exts = p.suffix
    return f"{base}.{to}{exts}"


def cmd_convert(args) -> int:
    """Dispatch to single-file or multi-file (--fasta) translation."""
    if args.fasta is not None:
        return _convert_multi(args)
    return _convert_single(args)


def _convert_single(args) -> int:
    if args.overwrite_to is not None:
        sys.exit(
            "error: --overwrite-to is only for --fasta (multi-file) mode. "
            "Use --to for single-file output."
        )
    if args.tgt is None:
        sys.exit("error: --to is required in single-file mode.")
    if args.out_dir is not None:
        sys.exit(
            "error: --out-dir is only for --fasta (multi-file) mode. "
            "Use -o for single-file output."
        )
    if len(args.input) != 1:
        sys.exit(
            "error: single-file mode takes exactly one input file. For multiple "
            "files use --fasta REF ANN... with --out-dir."
        )
    if args.output is None:
        sys.exit("error: -o/--output is required in single-file mode.")

    in_path, out_path = args.input[0], args.output
    if not in_path.exists():
        sys.exit(f"error: input file not found: {in_path}")
    if out_path.exists():
        sys.exit(
            f"error: output file already exists: {out_path} "
            f"(refusing to overwrite — choose another path or delete it first)"
        )
    try:
        translator_for(in_path)
    except ValueError as e:
        sys.exit(f"error: {e}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    source, db_path = _open_source(args)
    src_col, src_name, assembly = _resolve_from_assembly(source, in_path, args)
    tgt_col = CONVENTIONS[args.tgt]
    print(
        f"Loading alias table from {db_path}\n"
        f"  assembly={assembly}, from={src_name}, to={args.tgt}",
        file=sys.stderr,
    )
    alias_map = _load_map(source, assembly, src_col, src_name, tgt_col)
    _translate_file(in_path, out_path, alias_map)
    print("Done.", file=sys.stderr)
    return 0


def _convert_multi(args) -> int:
    if args.output is not None:
        sys.exit(
            "error: -o/--output is for single-file mode. In --fasta mode, "
            "outputs go to --out-dir."
        )
    if args.out_dir is None:
        sys.exit("error: --out-dir is required in --fasta (multi-file) mode.")
    if args.tgt is not None:
        sys.exit(
            "error: --to is single-file only. In --fasta mode, use "
            "--overwrite-to to force every file into one convention, or omit "
            "it to conform the annotations to the FASTA's own convention."
        )

    fasta = args.fasta
    if not fasta.exists():
        sys.exit(f"error: FASTA not found: {fasta}")
    annotations = list(args.input)
    for f in annotations:
        if not f.exists():
            sys.exit(f"error: input file not found: {f}")

    conform = args.overwrite_to is None
    if conform and args.src is not None:
        sys.exit(
            "error: --from is not used in conform mode. The FASTA's own "
            "convention is detected and used as the target. To force a "
            "specific convention for every file, use --overwrite-to."
        )
    if conform and not annotations:
        sys.exit(
            "error: conform mode needs at least one annotation file to conform "
            "to the FASTA. (To convert just the FASTA, use --overwrite-to.)"
        )

    # Validate every translator up front so a bad extension fails before
    # we touch the (possibly large) database download.
    for f in [fasta, *annotations]:
        try:
            translator_for(f)
        except ValueError as e:
            sys.exit(f"error: {e}")

    out_dir = args.out_dir
    source, db_path = _open_source(args)

    # Detect from the FASTA. In conform mode the FASTA's convention is the
    # target; in overwrite mode it's the (shared) source convention. The
    # assembly is detected from the FASTA either way. Output naming and
    # planning need the convention name, so this runs before planning.
    if conform:
        tgt_col, tgt_name, assembly = _resolve_from_assembly(
            source, fasta, args, role="FASTA convention (conform target)"
        )
        files_to_convert = annotations  # FASTA is the reference, left untouched
    else:
        src_col, src_name, assembly = _resolve_from_assembly(source, fasta, args)
        tgt_name = args.overwrite_to
        tgt_col = CONVENTIONS[args.overwrite_to]
        files_to_convert = [fasta, *annotations]

    # Plan outputs, refusing both overwrites and same-output collisions.
    planned, seen = [], {}
    for f in files_to_convert:
        out_path = out_dir / _output_name(f, tgt_name)
        if out_path in seen:
            sys.exit(
                f"error: inputs {seen[out_path]} and {f} both map to output "
                f"{out_path.name}. Rename one."
            )
        seen[out_path] = f
        if out_path.exists():
            sys.exit(f"error: output already exists: {out_path} (refusing to overwrite).")
        planned.append((f, out_path))

    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"Loading alias table from {db_path}\n"
        f"  assembly={assembly}, target={tgt_name}, "
        f"mode={'conform' if conform else 'overwrite'}",
        file=sys.stderr,
    )

    if conform:
        alias_map = _load_conform_map(source, assembly, tgt_col, tgt_name)
        conform_target = tgt_name
        print(
            f"Conforming {len(planned)} annotation file(s) to {tgt_name} in "
            f"{out_dir}/; FASTA {fasta.name} left unchanged.",
            file=sys.stderr,
        )
    else:
        alias_map = _load_map(source, assembly, src_col, src_name, tgt_col)
        conform_target = None
        print(f"Converting {len(planned)} file(s) into {out_dir}/", file=sys.stderr)

    totals = {"mapped": 0, "unmapped": 0}
    for in_path, out_path in planned:
        stats = _translate_file(in_path, out_path, alias_map,
                                conform_target=conform_target)
        totals["mapped"] += stats["mapped"]
        totals["unmapped"] += stats["unmapped"]
    print(
        f"Done. {len(planned)} file(s), total mapped={totals['mapped']}, "
        f"unmapped={totals['unmapped']}",
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

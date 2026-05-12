#!/usr/bin/env python3
"""
alias_mapper.py (v0)
--------------------
Minimal first version of the alias-mapper CLI. Translates the
chromosome/scaffold names in column 1 of a GFF file from one naming
convention to another, using the alias TSV produced by the weekly
collection workflow.
 
v0 scope (deliberately minimal):
  - GFF input only (no FASTA, no BED, no VCF)
  - Single input file per invocation
  - User must declare --from, --to, --assembly explicitly (no auto-detect)
  - Unknown sequence names: warn and pass through unchanged
  - No support for gzipped input
  - No translation of `##sequence-region` metadata lines yet
 
Future versions will add FASTA, auto-detection, gzipped input,
stricter modes, and other formats. See docs/DESIGN.md.
 
Usage:
    python3 src/alias_mapper.py convert INPUT.gff \\
        --from refseq --to ucsc \\
        --assembly GCF_000001405.40 \\
        -o OUTPUT.gff
"""
 
import argparse
import csv
import gzip
import sys
from pathlib import Path
 
# ---------------------------------------------------------------------------
# Naming-convention identifiers and which TSV column each maps to.
# These short names are what the user passes on the command line.
# ---------------------------------------------------------------------------
CONVENTIONS = {
    "genbank":           "GENBANK_ACC",
    "refseq":            "REFSEQ_ACC",
    "ucsc":              "UCSC_NAME",
    "sequence-name":     "SEQUENCE_NAME",
    "assigned-molecule": "ASSIGNED_MOLECULE",
}
 
# Default path the script falls back to when --alias-table is not supplied.
# Resolved relative to the repo root (the parent of this script's folder).
DEFAULT_ALIAS_TABLE = (
    Path(__file__).resolve().parent.parent / "data" / "aliases.tsv.gz"
)
 
 
# ---------------------------------------------------------------------------
# Load the alias TSV into an in-memory dict keyed by (assembly, source-name).
#
# Why a dict: hash lookups are O(1), so translating a million rows costs
# a million lookups instead of a million-times-the-TSV scans.
#
# Why scoped by assembly: the same name (e.g. "1") can refer to different
# chromosomes in different species. The user picks one assembly per run.
# ---------------------------------------------------------------------------
def load_alias_table(path, source_col, target_col, assembly):
    """
    Returns a dict: { source_name -> target_name } restricted to one assembly.
 
    Rows whose target column is empty are skipped — there's no useful
    translation for them under this --to convention.
    """
    if not path.exists():
        sys.exit(f"error: alias table not found at {path}")
 
    # Open transparently whether or not the file is gzipped.
    opener = gzip.open if path.suffix == ".gz" else open
    table = {}
    with opener(path, "rt", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        missing = [c for c in (source_col, target_col, "ACCESSION") if c not in reader.fieldnames]
        if missing:
            sys.exit(f"error: alias table missing columns: {missing}")
 
        for row in reader:
            if row["ACCESSION"] != assembly:
                continue
            src = row[source_col].strip()
            tgt = row[target_col].strip()
            if src and tgt:
                table[src] = tgt
 
    if not table:
        sys.exit(
            f"error: no rows found for assembly {assembly!r} with "
            f"both {source_col} and {target_col} populated. "
            f"Check the --assembly value and that the alias table includes it."
        )
    return table
 
 
# ---------------------------------------------------------------------------
# Translate one GFF line.
#
# GFF format reminder:
#   - Lines starting with '#' are comments / metadata. We pass these through
#     unchanged in v0 (translating metadata is a v1 feature).
#   - Data lines have tab-separated columns. Column 1 (index 0) is the
#     sequence name we want to translate.
# ---------------------------------------------------------------------------
def translate_gff_line(line, alias_map, stats):
    if not line or line.startswith("#"):
        return line  # comment / blank — pass through unchanged
 
    # split on tab, but only the first split is structurally important
    # (we only touch column 1; other columns stay byte-identical).
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 1:
        return line
 
    seq_name = parts[0]
    new_name = alias_map.get(seq_name)
    if new_name is None:
        # Unknown name — warn-and-pass-through (the default behaviour
        # we agreed on in the design doc).
        stats["unmapped"] += 1
        stats["unmapped_examples"].add(seq_name)
        return line
 
    parts[0] = new_name
    stats["mapped"] += 1
    return "\t".join(parts) + "\n"
 
 
# ---------------------------------------------------------------------------
# Main: parse args, set up the lookup table, stream input → output.
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Translate sequence names in a GFF file (v0)."
    )
    parser.add_argument(
        "command",
        choices=["convert"],
        help="Subcommand to run (v0 only supports 'convert').",
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
        "--alias-table", type=Path, default=DEFAULT_ALIAS_TABLE,
        help=f"Path to the alias TSV (default: {DEFAULT_ALIAS_TABLE}).",
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
 
    # Make sure the output directory exists.
    args.output.parent.mkdir(parents=True, exist_ok=True)
 
    src_col = CONVENTIONS[args.src]
    tgt_col = CONVENTIONS[args.tgt]
 
    print(
        f"Loading alias table from {args.alias_table}\n"
        f"  assembly={args.assembly}, from={args.src}, to={args.tgt}",
        file=sys.stderr,
    )
    alias_map = load_alias_table(args.alias_table, src_col, tgt_col, args.assembly)
    print(f"  -> {len(alias_map)} entries loaded", file=sys.stderr)
 
    # Stream the input file line-by-line, write the output the same way.
    # Constant memory regardless of input size.
    stats = {"mapped": 0, "unmapped": 0, "unmapped_examples": set()}
    print(f"Translating {args.input} → {args.output}", file=sys.stderr)
 
    with open(args.input, "r", encoding="utf-8") as in_f, \
         open(args.output, "w", encoding="utf-8") as out_f:
        for line in in_f:
            out_f.write(translate_gff_line(line, alias_map, stats))
 
    # Summary report to stderr (not stdout, which is reserved for data).
    print(
        f"Done. mapped={stats['mapped']}, unmapped={stats['unmapped']}",
        file=sys.stderr,
    )
    if stats["unmapped"]:
        examples = sorted(stats["unmapped_examples"])[:5]
        print(
            f"  warning: {stats['unmapped']} rows had sequence names not found "
            f"in the alias table for assembly {args.assembly}. "
            f"Examples: {examples}. These rows were passed through unchanged.",
            file=sys.stderr,
        )
 
 
if __name__ == "__main__":
    main()
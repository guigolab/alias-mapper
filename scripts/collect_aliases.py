#!/usr/bin/env python3
"""
collect_aliases.py
------------------
Collects alias names of the 50 longest molecules from every eukaryotic
genome assembly available on NCBI, and writes them to a TSV file.

Usage:
    python collect_aliases.py --output ../data/aliases.tsv
    python collect_aliases.py --output ../data/aliases.tsv --limit 10   # test run

Requires:
    - The NCBI Datasets CLI (`datasets`) installed and on $PATH
    - Python 3.8+
"""

import argparse           # parses command-line flags like --output, --limit
import csv                # writes TSV rows safely (handles tabs in fields, etc.)
import json               # parses the JSON output from `datasets`
import shutil             # deletes temp folders
import subprocess         # runs the `datasets` CLI from inside Python
import sys                # for printing to stderr and exiting on errors
import tempfile           # makes temporary working folders
import zipfile            # unzips the ncbi_dataset.zip files
from pathlib import Path  # nicer file path handling than raw strings

# ---------------------------------------------------------------------------
# The columns we want in the output TSV. Each row = one molecule.
# These names match the fields in NCBI's sequence_report.jsonl.
# ---------------------------------------------------------------------------
TSV_COLUMNS = [
    "assembly_accession",   # which assembly this molecule belongs to (e.g., GCF_000721785.1)
    "organism_name",        # the species name (e.g., Aureobasidium pullulans)
    "chr_name",             # Sequence-Name as the submitter chose (e.g., "1", "scaffold_3")
    "length",               # length of the molecule in base pairs
    "genbank_accession",    # GenBank ID (e.g., CM000663.2)
    "refseq_accession",     # RefSeq ID (e.g., NC_000001.11)
    "ucsc_style_name",      # UCSC name (e.g., chr1)
    "assigned_molecule",    # the chromosome it's part of, if known
    "role",                 # e.g., "assembled-molecule", "unplaced-scaffold"
]


def list_eukaryotic_assemblies(limit=None):
    """
    Calls `datasets summary genome taxon eukaryota` and returns a list of
    assembly accession strings.

    If `limit` is given, only returns the first N accessions (useful for testing).
    """
    print("Fetching the list of eukaryotic assemblies from NCBI...", file=sys.stderr)

    cmd = [
        "datasets", "summary", "genome",
        "taxon", "eukaryota",
        "--assembly-source", "RefSeq",   # only curated RefSeq assemblies (cleaner data)
        "--as-json-lines",               # one JSON object per line — easier to stream
    ]
    # capture_output=True grabs stdout/stderr; text=True returns strings not bytes
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)

    accessions = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        accession = record.get("accession")
        if accession:
            accessions.append(accession)
        if limit and len(accessions) >= limit:
            break

    print(f"Found {len(accessions)} assemblies.", file=sys.stderr)
    return accessions


def get_sequence_report(accession, work_dir):
    """
    Downloads the sequence report for a single assembly accession into
    work_dir, unzips it, and returns the parsed list of molecule records.

    Each record is a dict with fields like chr_name, length, genbank_accession, etc.
    """
    zip_path = work_dir / f"{accession}.zip"

    # Download just the sequence report (no FASTA, no annotation, etc.)
    cmd = [
        "datasets", "download", "genome",
        "accession", accession,
        "--include", "seq-report",
        "--filename", str(zip_path),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)

    # Unzip into a folder named after the accession
    extract_dir = work_dir / accession
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    # Find the sequence_report.jsonl file inside the extracted folder
    report_files = list(extract_dir.rglob("sequence_report.jsonl"))
    if not report_files:
        return []  # no report found — skip this assembly

    # Read all molecule records from the .jsonl file (one JSON per line)
    molecules = []
    with open(report_files[0]) as f:
        for line in f:
            line = line.strip()
            if line:
                molecules.append(json.loads(line))

    return molecules


def write_top_molecules(writer, accession, molecules, top_n=50):
    """
    Sort molecules by length (longest first), take the top N, and write
    each one as a row to the TSV writer.
    """
    # Some records may be missing length — treat those as 0 so they sort to the bottom
    molecules_sorted = sorted(
        molecules,
        key=lambda m: m.get("length", 0) or 0,
        reverse=True,
    )
    top = molecules_sorted[:top_n]

    for mol in top:
        row = {
            "assembly_accession": accession,
            "organism_name":      mol.get("organism_name", ""),
            "chr_name":           mol.get("chr_name", ""),
            "length":             mol.get("length", ""),
            "genbank_accession":  mol.get("genbank_accession", ""),
            "refseq_accession":   mol.get("refseq_accession", ""),
            "ucsc_style_name":    mol.get("ucsc_style_name", ""),
            "assigned_molecule":  mol.get("assigned_molecule", ""),
            "role":               mol.get("role", ""),
        }
        writer.writerow(row)


def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Path to write the output TSV file (e.g., ../data/aliases.tsv)",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="Only process the first N assemblies (for testing).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="How many longest molecules to keep per assembly (default: 50).",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)  # make sure data/ exists

    accessions = list_eukaryotic_assemblies(limit=args.limit)

    # Open the output TSV for writing. csv.DictWriter handles tabs/newlines safely.
    with open(output_path, "w", newline="") as out_f:
        writer = csv.DictWriter(
            out_f,
            fieldnames=TSV_COLUMNS,
            delimiter="\t",
        )
        writer.writeheader()

        # Use a temporary working directory for all the downloads.
        # It gets auto-deleted when the `with` block ends.
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)

            for i, acc in enumerate(accessions, start=1):
                print(f"[{i}/{len(accessions)}] {acc}", file=sys.stderr)
                try:
                    molecules = get_sequence_report(acc, work_dir)
                    if not molecules:
                        print(f"  (no sequence report — skipped)", file=sys.stderr)
                        continue
                    write_top_molecules(writer, acc, molecules, top_n=args.top)
                except subprocess.CalledProcessError as e:
                    # Don't crash the whole run if one assembly fails — log and continue
                    print(f"  ERROR on {acc}: {e.stderr.strip()[:200]}", file=sys.stderr)
                    continue

                # Clean up this accession's files so the temp dir doesn't balloon
                shutil.rmtree(work_dir / acc, ignore_errors=True)
                (work_dir / f"{acc}.zip").unlink(missing_ok=True)

    print(f"Done. Wrote {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
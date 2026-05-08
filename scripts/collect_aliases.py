#!/usr/bin/env python3
"""
collect_aliases.py
------------------
Collects alias names of the 50 longest molecules from every eukaryotic
genome assembly available on NCBI (GenBank + RefSeq), writes them to a
TSV, and gzips the result.

Workflow (per Emilio):
  1. Use `datasets summary genome taxon eukaryota` to get accessions and
     assembly names (one CLI call total - not per assembly).
  2. For each assembly, build the FTP path to its *_assembly_report.txt.
  3. Stream that file directly off NCBI's FTP server (no download to disk).
  4. Pick the 50 longest molecules and append rows to the TSV.
  5. Gzip the final TSV.

Usage:
    python3 collect_aliases.py --output ../data/aliases.tsv.gz
    python3 collect_aliases.py --output ../data/aliases.tsv.gz --limit 10
"""

import argparse
import csv
import gzip
import json
import re
import ssl
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

# -- SSL setup -------------------------------------------------------------
# On some macOS Python installs, the system cert bundle isn't trusted by
# urllib (or there's a TLS-inspecting middlebox on the network). We build
# an SSL context that uses the certifi bundle if available; otherwise we
# fall back to the system default.
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()
# --------------------------------------------------------------------------

TSV_COLUMNS = [
    "ACCESSION",
    "ASSEMBLY_NAME",
    "GENBANK_ACC",
    "REFSEQ_ACC",
    "SEQUENCE_NAME",
    "ASSIGNED_MOLECULE",
    "UCSC_NAME",
    "LENGTH",
]

FTP_BASE = "https://ftp.ncbi.nlm.nih.gov/genomes/all"
HTTP_HEADERS = {"User-Agent": "alias-mapper/0.1 (https://github.com/Max25R/alias-mapper)"}


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT)


def list_eukaryotic_assemblies(limit=None):
    print("Fetching eukaryotic assembly list from NCBI...", file=sys.stderr)
    cmd = [
        "datasets", "summary", "genome",
        "taxon", "eukaryota",
        "--as-json-lines",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)

    assemblies = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        acc = rec.get("accession")
        name = (rec.get("assembly_info") or {}).get("assembly_name")
        if acc and name:
            assemblies.append((acc, name))
        if limit and len(assemblies) >= limit:
            break

    print(f"  -> {len(assemblies)} assemblies", file=sys.stderr)
    return assemblies


def build_ftp_folder(accession, assembly_name):
    prefix = accession[:3]
    digits = accession.split("_")[1]
    digits_only = digits.split(".")[0]
    a, b, c = digits_only[0:3], digits_only[3:6], digits_only[6:9]
    safe_name = re.sub(r"[^A-Za-z0-9._\-]", "_", assembly_name)
    folder = f"{accession}_{safe_name}"
    return f"{FTP_BASE}/{prefix}/{a}/{b}/{c}/{folder}/"


def stream_assembly_report(folder_url, accession):
    folder_name = folder_url.rstrip("/").split("/")[-1]
    direct_url = folder_url + folder_name + "_assembly_report.txt"

    try:
        with http_get(direct_url) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    except urllib.error.URLError:
        pass

    parent = "/".join(folder_url.rstrip("/").split("/")[:-1]) + "/"
    try:
        with http_get(parent) as r:
            html = r.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None

    match = re.search(rf'href="({re.escape(accession)}[^"]*?)/"', html)
    if not match:
        return None

    real_folder = match.group(1)
    real_url = f"{parent}{real_folder}/{real_folder}_assembly_report.txt"
    try:
        with http_get(real_url) as r:
            return r.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None


def parse_assembly_report(text):
    header_cols = None
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            stripped = line.lstrip("#").strip()
            if "Sequence-Name" in stripped and "Sequence-Length" in stripped:
                header_cols = re.split(r"\t+", stripped)
            continue
        if header_cols is None:
            continue
        fields = line.split("\t")
        if len(fields) < len(header_cols):
            fields += [""] * (len(header_cols) - len(fields))
        rows.append(dict(zip(header_cols, fields)))
    return rows


def top_n_longest(rows, n=50):
    def length_of(r):
        try:
            return int(r.get("Sequence-Length", "0"))
        except (TypeError, ValueError):
            return 0
    return sorted(rows, key=length_of, reverse=True)[:n]


def row_to_tsv(row, accession, assembly_name):
    def clean(v):
        if v is None or v.strip().lower() == "na":
            return ""
        return v.strip()
    return {
        "ACCESSION":         accession,
        "ASSEMBLY_NAME":     assembly_name,
        "GENBANK_ACC":       clean(row.get("GenBank-Accn", "")),
        "REFSEQ_ACC":        clean(row.get("RefSeq-Accn", "")),
        "SEQUENCE_NAME":     clean(row.get("Sequence-Name", "")),
        "ASSIGNED_MOLECULE": clean(row.get("Assigned-Molecule", "")),
        "UCSC_NAME":         clean(row.get("UCSC-style-name", "")),
        "LENGTH":            clean(row.get("Sequence-Length", "")),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", "-o", required=True)
    parser.add_argument("--limit", "-n", type=int, default=None)
    parser.add_argument("--top", type=int, default=50)
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    assemblies = list_eukaryotic_assemblies(limit=args.limit)

    if output_path.suffix == ".gz":
        out_f = gzip.open(output_path, "wt", newline="", encoding="utf-8")
    else:
        out_f = open(output_path, "w", newline="", encoding="utf-8")

    n_ok = n_fail = 0
    try:
        writer = csv.DictWriter(out_f, fieldnames=TSV_COLUMNS, delimiter="\t")
        writer.writeheader()

        for i, (acc, name) in enumerate(assemblies, start=1):
            print(f"[{i}/{len(assemblies)}] {acc}  {name[:60]}", file=sys.stderr)
            folder_url = build_ftp_folder(acc, name)

            try:
                text = stream_assembly_report(folder_url, acc)
            except Exception as e:
                print(f"  ! network error: {e}", file=sys.stderr)
                n_fail += 1
                continue

            if text is None:
                print(f"  ! assembly_report.txt not found", file=sys.stderr)
                n_fail += 1
                continue

            rows = parse_assembly_report(text)
            top = top_n_longest(rows, n=args.top)
            for r in top:
                writer.writerow(row_to_tsv(r, acc, name))
            n_ok += 1
    finally:
        out_f.close()

    print(f"Done. OK: {n_ok}, failed: {n_fail}. Wrote {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
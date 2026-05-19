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

HTTP behavior:
  - 404/410 are treated as permanent (the assembly genuinely doesn't
    exist at the expected path; fall through to the parent-directory
    fallback or give up).
  - 429 (rate-limited) and 5xx server errors are retried with
    exponential backoff.
  - Connection-level failures (timeouts, DNS, TLS) are retried with
    the same backoff.
  - Other 4xx codes (e.g. 403) are treated as permanent.

Usage:
    python3 collect_aliases.py --output ../data/aliases.tsv.gz
    python3 collect_aliases.py --output ../data/aliases.tsv.gz --limit 10
"""

import argparse
import csv
import gzip
import json
import random
import re
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# -- SSL setup -------------------------------------------------------------
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()
# --------------------------------------------------------------------------

TSV_COLUMNS = [
    "ACCESSION", "ASSEMBLY_NAME", "GENBANK_ACC", "REFSEQ_ACC",
    "SEQUENCE_NAME", "ASSIGNED_MOLECULE", "UCSC_NAME", "LENGTH",
]

FAILURE_COLUMNS = ["ACCESSION", "ASSEMBLY_NAME", "STAGE", "REASON", "DETAIL"]

FTP_BASE = "https://ftp.ncbi.nlm.nih.gov/genomes/all"
HTTP_HEADERS = {"User-Agent": "alias-mapper/0.1 (https://github.com/Max25R/alias-mapper)"}

# HTTP error classification.
PERMANENT_HTTP_CODES = frozenset({404, 410})
RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

# Retry policy: 3 attempts, exponential backoff with jitter.
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 2.0


class PermanentHTTPError(Exception):
    """Definitive failure: 404/410, or non-retryable 4xx. Don't retry."""
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(message)


class TransientHTTPError(Exception):
    """All retries exhausted on transient errors. Worth trying again next week."""


def http_get_with_retry(url: str, timeout: int = 60) -> str:
    """
    Fetch a URL with retry on transient errors.

    Returns response body text on success.
    Raises PermanentHTTPError on 404/410/non-retryable 4xx.
    Raises TransientHTTPError if all retries are exhausted.
    """
    last_detail = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in PERMANENT_HTTP_CODES:
                raise PermanentHTTPError(e.code, f"HTTP {e.code}: {e.reason}")
            if e.code in RETRYABLE_HTTP_CODES:
                last_detail = f"HTTP {e.code}: {e.reason}"
            else:
                raise PermanentHTTPError(e.code, f"HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            last_detail = f"URLError: {e.reason}"

        if attempt < MAX_ATTEMPTS:
            sleep_for = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            sleep_for += random.uniform(0, sleep_for / 2)
            time.sleep(sleep_for)

    raise TransientHTTPError(f"all {MAX_ATTEMPTS} attempts failed for {url}: {last_detail}")


def list_eukaryotic_assemblies(limit=None):
    print("Fetching eukaryotic assembly list from NCBI...", file=sys.stderr)
    cmd = ["datasets", "summary", "genome", "taxon", "eukaryota", "--as-json-lines"]
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
    """
    Fetch the assembly_report.txt for one assembly.

    Returns (text, None) on success, or (None, (reason, detail)) on failure.

    Failure categories:
      not_found              - confirmed 404/410. Permanent.
      http_error_permanent   - non-404 4xx (e.g. 403). Permanent.
      transient_exhausted    - retries used up on 5xx/429/connection.
      folder_not_found       - parent directory listing didn't contain accession.
    """
    folder_name = folder_url.rstrip("/").split("/")[-1]
    direct_url = folder_url + folder_name + "_assembly_report.txt"

    # Attempt 1: predicted direct URL.
    try:
        return http_get_with_retry(direct_url), None
    except PermanentHTTPError as e:
        if e.code not in PERMANENT_HTTP_CODES:
            return None, ("http_error_permanent", str(e))
        # 404/410: fall through to parent-directory fallback.
    except TransientHTTPError as e:
        return None, ("transient_exhausted", str(e))

    # Attempt 2: parent directory listing.
    parent = "/".join(folder_url.rstrip("/").split("/")[:-1]) + "/"
    try:
        html = http_get_with_retry(parent)
    except PermanentHTTPError as e:
        if e.code in PERMANENT_HTTP_CODES:
            return None, ("not_found", f"parent directory {parent} returned {e.code}")
        return None, ("http_error_permanent", str(e))
    except TransientHTTPError as e:
        return None, ("transient_exhausted", str(e))

    match = re.search(rf'href="({re.escape(accession)}[^"]*?)/"', html)
    if not match:
        return None, ("not_found",
                      f"accession not present in parent directory listing at {parent}")

    real_folder = match.group(1)
    real_url = f"{parent}{real_folder}/{real_folder}_assembly_report.txt"

    # Attempt 3: resolved URL.
    try:
        return http_get_with_retry(real_url), None
    except PermanentHTTPError as e:
        if e.code in PERMANENT_HTTP_CODES:
            return None, ("not_found", f"{real_url} returned {e.code}")
        return None, ("http_error_permanent", str(e))
    except TransientHTTPError as e:
        return None, ("transient_exhausted", str(e))


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
    parser.add_argument("--failures", default=None,
        help="Path to write per-assembly failure log as TSV. "
             "Defaults to <output-dir>/failures.tsv.")
    parser.add_argument("--limit", "-n", type=int, default=None)
    parser.add_argument("--top", type=int, default=50)
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    failures_path = (Path(args.failures) if args.failures
                     else output_path.parent / "failures.tsv")

    assemblies = list_eukaryotic_assemblies(limit=args.limit)

    if output_path.suffix == ".gz":
        out_f = gzip.open(output_path, "wt", newline="", encoding="utf-8")
    else:
        out_f = open(output_path, "w", newline="", encoding="utf-8")

    fail_f = open(failures_path, "w", newline="", encoding="utf-8")

    n_ok = n_fail = 0
    fail_counts: dict[str, int] = {}

    def log_failure(acc, name, stage, reason, detail):
        nonlocal n_fail
        fail_writer.writerow({
            "ACCESSION": acc, "ASSEMBLY_NAME": name, "STAGE": stage,
            "REASON": reason, "DETAIL": detail,
        })
        fail_counts[reason] = fail_counts.get(reason, 0) + 1
        n_fail += 1

    try:
        writer = csv.DictWriter(out_f, fieldnames=TSV_COLUMNS, delimiter="\t")
        writer.writeheader()

        fail_writer = csv.DictWriter(fail_f, fieldnames=FAILURE_COLUMNS, delimiter="\t")
        fail_writer.writeheader()

        for i, (acc, name) in enumerate(assemblies, start=1):
            print(f"[{i}/{len(assemblies)}] {acc}  {name[:60]}", file=sys.stderr)
            folder_url = build_ftp_folder(acc, name)

            try:
                text, err = stream_assembly_report(folder_url, acc)
            except Exception as e:
                print(f"  ! unexpected error: {e}", file=sys.stderr)
                log_failure(acc, name, "ftp_fetch", "unexpected", str(e))
                continue

            if text is None:
                reason, detail = err
                print(f"  ! fetch failed ({reason}): {detail}", file=sys.stderr)
                log_failure(acc, name, "ftp_fetch", reason, detail)
                continue

            rows = parse_assembly_report(text)
            if not rows:
                print(f"  ! report parsed to zero rows", file=sys.stderr)
                log_failure(acc, name, "report_parse", "empty_report",
                            "parse_assembly_report returned no rows")
                continue

            top = top_n_longest(rows, n=args.top)
            if not top:
                print(f"  ! no rows survived length-sort", file=sys.stderr)
                log_failure(acc, name, "report_parse", "no_valid_lengths",
                            "all rows had unparseable Sequence-Length")
                continue

            for r in top:
                writer.writerow(row_to_tsv(r, acc, name))
            n_ok += 1
    finally:
        out_f.close()
        fail_f.close()

    print(f"Done. OK: {n_ok}, failed: {n_fail}. Wrote {output_path}", file=sys.stderr)
    if n_fail:
        print(f"  Failure log: {failures_path}", file=sys.stderr)
        print(f"  Failures by reason:", file=sys.stderr)
        for reason, count in sorted(fail_counts.items(), key=lambda x: -x[1]):
            print(f"    {count:>6,}  {reason}", file=sys.stderr)


if __name__ == "__main__":
    main()
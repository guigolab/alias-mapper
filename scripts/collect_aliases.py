#!/usr/bin/env python3
"""
collect_aliases.py
------------------
Weekly alias data collection, driven by NCBI's four assembly_summary
files. Produces three Release artifacts:

  - aliases.tsv.gz     merged-row per-assembly data (schema v2). One
                       row per assembly; per-molecule data lives in
                       comma-separated, position-aligned list columns.
  - historical.tsv.gz  dead-accession lookup with suppression dates
                       and best-effort replacements.
  - failures.tsv       per-assembly collection failure log.

Pipeline shape:
  1. Stream the four summary files (current GenBank, current RefSeq,
     historical GenBank, historical RefSeq).
  2. Join GenBank↔RefSeq pairs via gbrs_paired_asm (stem-normalized).
  3. Filter to version_status=latest, allowed assembly_level set, and
     allowed eukaryotic taxonomic groups.
  4. Write historical.tsv.gz (pure planner output — doesn't depend on
     per-assembly fetches, so it survives a partial sweep).
  5. For each planned assembly, fetch its assembly_report.txt via the
     `ftp_path` column directly (parallelized via ThreadPoolExecutor).
  6. Adaptive coverage cap: top 50 longest molecules, expand toward
     90% of genome_size if needed, hard ceiling at 500.
  7. Emit merged TSV rows.

Local dev keeps a cache of the four summary files at
~/.cache/alias-mapper-dev/ for fast iteration. CI passes --no-cache to
stream from NCBI on every run.
"""

import argparse
import csv
import gzip
import os
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# Shared HTTP machinery. Defines PermanentHTTPError, TransientHTTPError,
# http_get_with_retry, SSL_CONTEXT, SSL_BACKEND, HTTP_HEADERS.
from _http import (
    HTTP_HEADERS,
    PermanentHTTPError,
    SSL_BACKEND,
    SSL_CONTEXT,
    TransientHTTPError,
    http_get_with_retry,
)


# --- Summary file URLs --------------------------------------------------
SUMMARY_BASE = "https://ftp.ncbi.nlm.nih.gov/genomes"
SUMMARY_FILES = {
    "genbank_current":    f"{SUMMARY_BASE}/genbank/assembly_summary_genbank.txt",
    "refseq_current":     f"{SUMMARY_BASE}/refseq/assembly_summary_refseq.txt",
    "genbank_historical": f"{SUMMARY_BASE}/genbank/assembly_summary_genbank_historical.txt",
    "refseq_historical":  f"{SUMMARY_BASE}/refseq/assembly_summary_refseq_historical.txt",
}

ALLOWED_LEVELS = frozenset({"Complete Genome", "Chromosome", "Scaffold"})

ALLOWED_GROUPS = frozenset({
    "fungi", "invertebrate", "plant", "protozoa",
    "vertebrate_mammalian", "vertebrate_other",
})

# Output schemas.
#
# TSV is the merged-row format (schema v2): one row per assembly.
# Per-molecule data lives in comma-separated, position-aligned list
# columns. The Nth comma-separated entry in every list column refers
# to the same molecule. Sort order across lists is length descending.
#
# The SQLite build (build_alias_db.py) explodes these lists back into
# per-molecule rows for indexed lookup. The TSV stays human-readable
# and diff-friendly; SQLite stays fast for queries.
TSV_COLUMNS = [
    "genbank_acc", "refseq_acc", "assembly_name", "taxid",
    "organism_name", "group", "assembly_level",
    "sequence_names", "genbank_seq_accs", "refseq_seq_accs",
    "ucsc_names", "assigned_molecules", "lengths",
]
FAILURE_COLUMNS = ["ACCESSION", "ASSEMBLY_NAME", "STAGE", "REASON", "DETAIL"]
HISTORICAL_COLUMNS = [
    "ACCESSION", "STATUS", "REPLACED_BY", "SUPPRESSION_DATE", "ASSEMBLY_NAME",
]

# Coverage cap policy.
COVERAGE_BASE = 50           # always take at least this many longest molecules
COVERAGE_CEILING = 500       # never take more than this (pathological scaffold guard)
COVERAGE_TARGET = 0.90       # expand until summed length hits this fraction of genome_size

# Parallelism. NCBI's 3-req/sec rate-limit advisory is for E-utilities;
# the FTP server we hit tolerates more. 8 workers is comfortably under
# what FTP will throttle. Bump via --workers if CI throughput needs it.
DEFAULT_WORKERS = 8

# Cache config (dev only — CI passes --no-cache).
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "alias-mapper-dev"
DEFAULT_CACHE_TTL_HOURS = 24


# --- Data model ---------------------------------------------------------

@dataclass
class HistoricalEntry:
    accession: str
    assembly_name: str
    status: str               # 'replaced' / 'suppressed' / 'withdrawn'
    asm_not_live_date: str    # when NCBI marked it dead (may be empty)


@dataclass
class AssemblyPlanEntry:
    genbank_acc: str
    refseq_acc: str | None
    assembly_name: str
    taxid: str
    organism_name: str
    group: str
    assembly_level: str
    ftp_path: str
    genome_size: int | None
    genome_size_ungapped: int | None


# --- Cache layer --------------------------------------------------------

def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _download_to(url: str, dest: Path) -> None:
    """Download with progress to stderr, atomic-rename on completion."""
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=120, context=SSL_CONTEXT) as r:
        total = int(r.headers.get("Content-Length", "0")) or None
        downloaded = 0
        last_print = time.monotonic()
        chunk = 1 << 16
        with open(tmp, "wb") as out:
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                out.write(buf)
                downloaded += len(buf)
                now = time.monotonic()
                if now - last_print >= 1.0:
                    if total:
                        pct = 100 * downloaded / total
                        msg = (
                            f"\r    {_fmt_bytes(downloaded)} / "
                            f"{_fmt_bytes(total)} ({pct:.1f}%)"
                        )
                    else:
                        msg = f"\r    {_fmt_bytes(downloaded)}"
                    sys.stderr.write(msg)
                    sys.stderr.flush()
                    last_print = now
        sys.stderr.write("\r" + " " * 60 + "\r")
        sys.stderr.flush()
    os.replace(tmp, dest)


def get_summary_file(
    key: str, cache_dir: Path, ttl_hours: float, use_cache: bool,
) -> Path:
    url = SUMMARY_FILES[key]
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not use_cache:
        dest = cache_dir / f"_nocache_{key}.txt"
        print(f"  downloading {key} (no-cache mode)...", file=sys.stderr)
        _download_to(url, dest)
        return dest

    dest = cache_dir / f"{key}.txt"
    if dest.exists():
        age_s = time.time() - dest.stat().st_mtime
        if age_s < ttl_hours * 3600:
            size = dest.stat().st_size
            print(
                f"  using cached {key} "
                f"({_fmt_bytes(size)}, age {age_s/3600:.1f}h)",
                file=sys.stderr,
            )
            return dest
        else:
            print(f"  cached {key} is stale, re-downloading...", file=sys.stderr)
    else:
        print(f"  downloading {key} (first run)...", file=sys.stderr)

    _download_to(url, dest)
    print(f"  cached {key} -> {dest}", file=sys.stderr)
    return dest


# --- Summary-file streaming reader --------------------------------------

def stream_summary_file(path: Path) -> Iterator[dict[str, str]]:
    """Yield one dict per data row from a cached summary file."""
    headers = None
    with open(path, encoding="utf-8", newline="") as f:
        for line in f:
            if line.startswith("##"):
                continue
            if line.startswith("#"):
                headers = line.lstrip("#").strip().split("\t")
                continue
            if not line.strip():
                continue
            if headers is None:
                raise RuntimeError(f"reached data row before headers in {path}")
            fields = line.rstrip("\n").split("\t")
            if len(fields) < len(headers):
                fields += [""] * (len(headers) - len(fields))
            yield dict(zip(headers, fields))


# --- Historicals + planner ----------------------------------------------

def load_historicals(
    cache_dir: Path, ttl_hours: float, use_cache: bool
) -> dict[str, HistoricalEntry]:
    out: dict[str, HistoricalEntry] = {}
    for key in ("genbank_historical", "refseq_historical"):
        path = get_summary_file(key, cache_dir, ttl_hours, use_cache)
        print(f"  parsing {key}...", file=sys.stderr)
        n = 0
        for row in stream_summary_file(path):
            acc = row.get("assembly_accession", "").strip()
            if not acc:
                continue
            out[acc] = HistoricalEntry(
                accession=acc,
                assembly_name=row.get("asm_name", "").strip(),
                status=row.get("version_status", "").strip(),
                asm_not_live_date=row.get("asm_not_live_date", "").strip(),
            )
            n += 1
        print(f"    -> {n:,} entries", file=sys.stderr)
    return out


def build_assembly_plan(
    historicals: dict[str, HistoricalEntry],
    cache_dir: Path, ttl_hours: float, use_cache: bool,
    allowed_levels: frozenset[str] = ALLOWED_LEVELS,
    allowed_groups: frozenset[str] = ALLOWED_GROUPS,
) -> Iterator[AssemblyPlanEntry]:
    gb_path = get_summary_file("genbank_current", cache_dir, ttl_hours, use_cache)
    print("  parsing genbank_current into pairing dict...", file=sys.stderr)
    genbank: dict[str, dict] = {}
    raw = kept = 0
    drops_by_reason: dict[str, int] = {}

    def _drop(reason: str) -> None:
        drops_by_reason[reason] = drops_by_reason.get(reason, 0) + 1

    for row in stream_summary_file(gb_path):
        raw += 1
        acc = row.get("assembly_accession", "").strip()
        if not acc:
            _drop("no_accession")
            continue
        if row.get("version_status", "").strip() != "latest":
            _drop("not_latest")
            continue
        if acc in historicals:
            _drop("in_historicals")
            continue
        if row.get("assembly_level", "").strip() not in allowed_levels:
            _drop("level_excluded")
            continue
        if row.get("group", "").strip() not in allowed_groups:
            _drop("group_excluded")
            continue
        genbank[acc] = row
        kept += 1
    print(f"    -> {kept:,} kept / {raw:,} total", file=sys.stderr)
    print("  drops by reason:", file=sys.stderr)
    for reason, n in sorted(drops_by_reason.items(), key=lambda x: -x[1]):
        print(f"    {n:>10,}  {reason}", file=sys.stderr)

    # Stem-normalized join. See _accession_stem.
    genbank_by_stem: dict[str, str] = {
        _accession_stem(acc): acc for acc in genbank
    }

    rs_path = get_summary_file("refseq_current", cache_dir, ttl_hours, use_cache)
    print("  parsing refseq_current and joining pairs...", file=sys.stderr)
    refseq_pair: dict[str, str] = {}
    paired = no_pair_field = unmatched_stem = 0
    for row in stream_summary_file(rs_path):
        if row.get("version_status", "").strip() != "latest":
            continue
        refseq_acc = row.get("assembly_accession", "").strip()
        gb_pair = row.get("gbrs_paired_asm", "").strip()
        if not gb_pair or gb_pair == "na":
            no_pair_field += 1
            continue
        gb_match = genbank_by_stem.get(_accession_stem(gb_pair))
        if gb_match is None:
            unmatched_stem += 1
            continue
        refseq_pair[gb_match] = refseq_acc
        paired += 1
    print(
        f"    -> {paired:,} paired, "
        f"{no_pair_field:,} refseqs with no gbrs_paired_asm, "
        f"{unmatched_stem:,} refseqs whose paired GenBank is outside our population",
        file=sys.stderr,
    )

    for genbank_acc, row in genbank.items():
        yield AssemblyPlanEntry(
            genbank_acc=genbank_acc,
            refseq_acc=refseq_pair.get(genbank_acc),
            assembly_name=row.get("asm_name", "").strip(),
            taxid=row.get("taxid", "").strip(),
            organism_name=row.get("organism_name", "").strip(),
            group=row.get("group", "").strip(),
            assembly_level=row.get("assembly_level", "").strip(),
            ftp_path=row.get("ftp_path", "").strip(),
            genome_size=_to_int_or_none(row.get("genome_size", "")),
            genome_size_ungapped=_to_int_or_none(row.get("genome_size_ungapped", "")),
        )


def _accession_stem(acc: str) -> str:
    """
    Normalize an accession to a versionless, GCA-prefixed join key.

    "GCA_000001405.15" -> "GCA_000001405"
    "GCF_000001405.40" -> "GCA_000001405"   (GCF normalized to GCA)
    """
    base = acc.split(".", 1)[0]
    if base.startswith("GCF_"):
        base = "GCA_" + base[4:]
    return base


def _to_int_or_none(s: str) -> int | None:
    s = s.strip()
    if not s or s == "na":
        return None
    try:
        return int(s)
    except ValueError:
        return None


# --- Per-assembly fetch + parse -----------------------------------------

def report_url_for(entry: AssemblyPlanEntry) -> str:
    """
    Build the assembly_report.txt URL from the plan's ftp_path.

    NCBI's convention: folder_name = basename(ftp_path), and the report
    file is f"{folder_name}_assembly_report.txt" inside that folder.
    """
    folder = entry.ftp_path.rstrip("/")
    folder_name = folder.rsplit("/", 1)[-1]
    return f"{folder}/{folder_name}_assembly_report.txt"


def fetch_and_parse_one(
    entry: AssemblyPlanEntry,
) -> tuple[AssemblyPlanEntry, list[dict[str, str]] | None, tuple[str, str, str] | None]:
    """
    Fetch + parse + cap one assembly. Pure function: no shared state, no
    I/O on output files. Safe to call from a worker thread.

    Returns (entry, top_rows, None) on success.
    Returns (entry, None, (stage, reason, detail)) on failure.
    """
    if not entry.ftp_path or entry.ftp_path == "na":
        return entry, None, ("ftp_fetch", "no_ftp_path", "plan entry has empty/na ftp_path")

    url = report_url_for(entry)
    try:
        text = http_get_with_retry(url)
    except PermanentHTTPError as e:
        if e.code in (404, 410):
            return entry, None, ("ftp_fetch", "not_found", f"{url}: {e}")
        return entry, None, ("ftp_fetch", "http_error_permanent", f"{url}: {e}")
    except TransientHTTPError as e:
        return entry, None, ("ftp_fetch", "transient_exhausted", str(e))
    except Exception as e:
        # Defensive: a worker raising would just be logged as the future's
        # exception; we'd rather see it as a structured failure row.
        return entry, None, ("ftp_fetch", "unexpected", repr(e))

    rows = parse_assembly_report(text)
    if not rows:
        return entry, None, ("report_parse", "empty_report", "parse returned no rows")

    top = adaptive_top_molecules(rows, entry.genome_size)
    if not top:
        return entry, None, ("report_parse", "no_valid_lengths",
                             "no rows survived length sort")

    return entry, top, None


def parse_assembly_report(text: str) -> list[dict[str, str]]:
    """
    Parse an assembly_report.txt into row dicts keyed by the column
    header from the file (Sequence-Name, GenBank-Accn, RefSeq-Accn, etc).
    """
    import re
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


def adaptive_top_molecules(
    rows: list[dict[str, str]],
    genome_size: int | None,
    base: int = COVERAGE_BASE,
    ceiling: int = COVERAGE_CEILING,
    target_coverage: float = COVERAGE_TARGET,
) -> list[dict[str, str]]:
    """
    Top `base` molecules by length, expanded toward `target_coverage` of
    `genome_size` if needed, with a hard `ceiling` for pathological cases.

    If genome_size is unknown, falls back to plain top-`base`.
    """
    def length_of(r: dict[str, str]) -> int:
        try:
            return int(r.get("Sequence-Length", "0"))
        except (TypeError, ValueError):
            return 0

    sorted_rows = sorted(rows, key=length_of, reverse=True)
    if len(sorted_rows) <= base:
        return sorted_rows
    if not genome_size or genome_size <= 0:
        return sorted_rows[:base]

    target = genome_size * target_coverage
    selected = sorted_rows[:base]
    running = sum(length_of(r) for r in selected)
    if running >= target:
        return selected

    for r in sorted_rows[base:]:
        selected.append(r)
        running += length_of(r)
        if running >= target or len(selected) >= ceiling:
            break
    return selected


def build_replacement_index(
    plan: list[AssemblyPlanEntry],
) -> dict[str, str]:
    """
    Build stem -> current_accession map for replacement-derivation.

    For each current assembly in the plan, its accession stem is a
    candidate replacement target for any historical assembly with the
    same stem. Stem normalization handles GCA/GCF prefix swaps.

    Only assemblies in our filtered population (eukaryotes) are in the
    index, so non-eukaryotic historicals will get an empty REPLACED_BY.
    That's honest: we don't have authoritative replacement data outside
    our population.
    """
    out: dict[str, str] = {}
    for entry in plan:
        out[_accession_stem(entry.genbank_acc)] = entry.genbank_acc
    return out


def write_historical_tsv(
    historicals: dict[str, HistoricalEntry],
    replacement_index: dict[str, str],
    output_path: Path,
) -> None:
    """
    Write the historical assemblies TSV.

    Schema: ACCESSION, STATUS, REPLACED_BY, SUPPRESSION_DATE, ASSEMBLY_NAME.
    Used by the CLI to give a useful error message when a user references
    a dead accession (issue #5).

    REPLACED_BY is best-effort: looked up by accession stem in the
    current-population index. Empty when no match.
    """
    if output_path.suffix == ".gz":
        f = gzip.open(output_path, "wt", newline="", encoding="utf-8")
    else:
        f = open(output_path, "w", newline="", encoding="utf-8")

    n_written = n_with_replacement = 0
    try:
        writer = csv.DictWriter(f, fieldnames=HISTORICAL_COLUMNS, delimiter="\t")
        writer.writeheader()
        for h in historicals.values():
            replaced_by = replacement_index.get(_accession_stem(h.accession), "")
            if replaced_by:
                n_with_replacement += 1
            writer.writerow({
                "ACCESSION": h.accession,
                "STATUS": h.status,
                "REPLACED_BY": replaced_by,
                "SUPPRESSION_DATE": h.asm_not_live_date,
                "ASSEMBLY_NAME": h.assembly_name,
            })
            n_written += 1
    finally:
        f.close()

    print(
        f"Wrote {n_written:,} historical rows ({n_with_replacement:,} with REPLACED_BY) "
        f"-> {output_path}",
        file=sys.stderr,
    )


def merge_assembly_to_row(
    molecules: list[dict[str, str]], entry: AssemblyPlanEntry,
) -> dict[str, str]:
    """
    Collapse the per-molecule rows from one assembly into a single
    merged TSV row with position-aligned comma-separated lists.

    `molecules` is assumed to already be sorted (longest first, since
    adaptive_top_molecules returns them that way). Position 0 of every
    list column refers to the longest molecule.

    Empty/na values within a list are preserved as empty between
    commas (e.g. "chr1,,chr3") so position-alignment is never broken.
    """
    def clean(v: str | None) -> str:
        if v is None or v.strip().lower() == "na":
            return ""
        return v.strip()

    def col(name: str) -> str:
        return ",".join(clean(m.get(name, "")) for m in molecules)

    return {
        "genbank_acc":        entry.genbank_acc,
        "refseq_acc":         entry.refseq_acc or "",
        "assembly_name":      entry.assembly_name,
        "taxid":              entry.taxid,
        "organism_name":      entry.organism_name,
        "group":              entry.group,
        "assembly_level":     entry.assembly_level,
        "sequence_names":     col("Sequence-Name"),
        "genbank_seq_accs":   col("GenBank-Accn"),
        "refseq_seq_accs":    col("RefSeq-Accn"),
        "ucsc_names":         col("UCSC-style-name"),
        "assigned_molecules": col("Assigned-Molecule"),
        "lengths":            col("Sequence-Length"),
    }


# --- Driver -------------------------------------------------------------

def run_collection(
    plan: list[AssemblyPlanEntry],
    output_path: Path,
    failures_path: Path,
    workers: int = DEFAULT_WORKERS,
    log_every: int = 100,
) -> None:
    """
    Parallel per-assembly fetch + parse + write.

    Workers run fetch_and_parse_one() concurrently (I/O-bound; threading
    is the right tool, not multiprocessing). The main thread owns all
    writes to the output and failure files, so no locks are needed and
    shutdown semantics are clean: the `with` blocks on ThreadPoolExecutor
    and the file handles handle Ctrl-C and exceptions correctly.

    Output rows are written in completion order (not plan order). The
    downstream SQLite build doesn't care; if we ever need plan-order
    output for clean diffs, sort at build_db time.
    """
    if output_path.suffix == ".gz":
        out_f = gzip.open(output_path, "wt", newline="", encoding="utf-8")
    else:
        out_f = open(output_path, "w", newline="", encoding="utf-8")
    fail_f = open(failures_path, "w", newline="", encoding="utf-8")

    n_ok = n_fail = n_rows = n_molecules = 0
    fail_counts: dict[str, int] = {}
    started = time.monotonic()
    total = len(plan)

    try:
        writer = csv.DictWriter(out_f, fieldnames=TSV_COLUMNS, delimiter="\t")
        writer.writeheader()
        fail_writer = csv.DictWriter(fail_f, fieldnames=FAILURE_COLUMNS, delimiter="\t")
        fail_writer.writeheader()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Submit all work upfront. With 47k entries this is a few
            # MB of futures in the pool's queue — fine.
            futures = [pool.submit(fetch_and_parse_one, e) for e in plan]

            for i, future in enumerate(as_completed(futures), start=1):
                entry, top, err = future.result()

                if err is not None:
                    stage, reason, detail = err
                    print(
                        f"  [{i}/{total}] FAIL {entry.genbank_acc} ({reason})",
                        file=sys.stderr,
                    )
                    fail_writer.writerow({
                        "ACCESSION": entry.genbank_acc,
                        "ASSEMBLY_NAME": entry.assembly_name,
                        "STAGE": stage, "REASON": reason, "DETAIL": detail,
                    })
                    fail_counts[reason] = fail_counts.get(reason, 0) + 1
                    n_fail += 1
                else:
                    writer.writerow(merge_assembly_to_row(top, entry))
                    n_rows += 1
                    n_molecules += len(top)
                    n_ok += 1

                if i % log_every == 0:
                    elapsed = time.monotonic() - started
                    rate = i / elapsed if elapsed else 0
                    eta_min = ((total - i) / rate) / 60 if rate else 0
                    print(
                        f"  [{i}/{total}] ok={n_ok:,} fail={n_fail:,} "
                        f"rows={n_rows:,} molecules={n_molecules:,} "
                        f"rate={rate:.1f}/s eta={eta_min:.0f}min",
                        file=sys.stderr,
                    )
    finally:
        out_f.close()
        fail_f.close()

    elapsed = time.monotonic() - started
    print(
        f"Done in {elapsed/60:.1f}min ({workers} workers). "
        f"OK: {n_ok:,}  failed: {n_fail:,}  "
        f"rows: {n_rows:,}  molecules: {n_molecules:,}",
        file=sys.stderr,
    )
    print(f"  TSV:      {output_path}", file=sys.stderr)
    print(f"  failures: {failures_path}", file=sys.stderr)
    if fail_counts:
        print("  failures by reason:", file=sys.stderr)
        for reason, count in sorted(fail_counts.items(), key=lambda x: -x[1]):
            print(f"    {count:>6,}  {reason}", file=sys.stderr)


# --- CLI ----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build the plan and print summary stats, then exit.",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output TSV path (.tsv or .tsv.gz). Required unless --dry-run.",
    )
    parser.add_argument(
        "--failures", default=None,
        help="Output failure log path. Default: <output-dir>/failures.tsv",
    )
    parser.add_argument(
        "--historical-output", default=None,
        help="Output historical TSV path. Default: <output-dir>/historical.tsv.gz",
    )
    parser.add_argument(
        "--limit", "-n", type=int, default=None,
        help="Process only the first N assemblies in the plan. For dev.",
    )
    parser.add_argument(
        "--workers", "-j", type=int, default=DEFAULT_WORKERS,
        help=f"Concurrent fetch workers (default: {DEFAULT_WORKERS}). "
             f"Use 1 for single-threaded debugging.",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="Disable SSL certificate verification. Local dev only.",
    )
    parser.add_argument(
        "--cache-dir", default=str(DEFAULT_CACHE_DIR),
        help=f"Cache dir for summary files (default: {DEFAULT_CACHE_DIR}).",
    )
    parser.add_argument(
        "--cache-ttl-hours", type=float, default=DEFAULT_CACHE_TTL_HOURS,
        help=f"Cache freshness window (default: {DEFAULT_CACHE_TTL_HOURS}h).",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Always download summary files fresh. Use in CI.",
    )
    args = parser.parse_args()

    # --insecure overrides the SSL context globally for both _http and us.
    if args.insecure:
        import _http
        _http.SSL_CONTEXT = ssl._create_unverified_context()
        # Also affects our own _download_to(), which captures SSL_CONTEXT
        # from this module's import. Rebind:
        global SSL_CONTEXT  # type: ignore[misc]
        SSL_CONTEXT = _http.SSL_CONTEXT
        print(
            "WARNING: SSL certificate verification disabled (--insecure).",
            file=sys.stderr,
        )
    else:
        print(f"SSL backend: {SSL_BACKEND}", file=sys.stderr)

    cache_dir = Path(args.cache_dir)
    use_cache = not args.no_cache
    if use_cache:
        print(f"Cache: {cache_dir} (TTL {args.cache_ttl_hours}h)", file=sys.stderr)
    else:
        print("Cache: disabled (--no-cache)", file=sys.stderr)

    print("Loading historicals...", file=sys.stderr)
    historicals = load_historicals(cache_dir, args.cache_ttl_hours, use_cache)
    print(f"  {len(historicals):,} dead accessions total", file=sys.stderr)

    print("Building assembly plan...", file=sys.stderr)
    plan = list(build_assembly_plan(
        historicals, cache_dir, args.cache_ttl_hours, use_cache,
    ))
    print(f"  {len(plan):,} assemblies in plan", file=sys.stderr)

    by_level: dict[str, int] = {}
    by_group: dict[str, int] = {}
    paired = 0
    for entry in plan:
        by_level[entry.assembly_level] = by_level.get(entry.assembly_level, 0) + 1
        by_group[entry.group] = by_group.get(entry.group, 0) + 1
        if entry.refseq_acc:
            paired += 1
    print("  by level:", file=sys.stderr)
    for level, n in sorted(by_level.items(), key=lambda x: -x[1]):
        print(f"    {n:>8,}  {level}", file=sys.stderr)
    print("  by group:", file=sys.stderr)
    for group, n in sorted(by_group.items(), key=lambda x: -x[1]):
        print(f"    {n:>8,}  {group}", file=sys.stderr)
    print(f"  paired with RefSeq: {paired:,} / {len(plan):,}", file=sys.stderr)

    if args.limit is not None:
        plan = plan[:args.limit]
        print(f"  --limit applied: trimmed to {len(plan):,} assemblies",
              file=sys.stderr)

    if args.dry_run:
        print("Dry run — exiting before per-assembly fetches.", file=sys.stderr)
        return 0

    if not args.output:
        sys.exit("error: --output required unless --dry-run is set")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    failures_path = (Path(args.failures) if args.failures
                     else output_path.parent / "failures.tsv")
    historical_path = (Path(args.historical_output) if args.historical_output
                       else output_path.parent / "historical.tsv.gz")

    # Write historical TSV first — it's pure planner output, doesn't
    # depend on any per-assembly fetching. If the alias sweep fails or
    # gets interrupted, the historical artifact is still valid.
    replacement_index = build_replacement_index(plan)
    write_historical_tsv(historicals, replacement_index, historical_path)

    print(f"Starting per-assembly collection ({len(plan):,} assemblies, {args.workers} workers)...",
          file=sys.stderr)
    run_collection(plan, output_path, failures_path, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

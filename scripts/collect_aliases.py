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
import re
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

# v2.0 ships Chromosome+ only. Scaffold-level (draft genomes, ~32k of
# 48k eukaryotic assemblies) made CI unfinishable inside the 6h
# GitHub-hosted ceiling: per-assembly fetch rate decays from ~15/s to
# ~1/s over a sustained sweep (NCBI FTP throttling on the long tail),
# and Scaffold assemblies are both more numerous and have more
# molecules per assembly than Chromosome+. Two failed runs (timeout at
# 360min, second at ~28k/48k) confirmed the full set doesn't fit.
# Add Scaffold back once we have either a matrix-split workflow or a
# more rate-limit-friendly fetch path (rsync bulk pull, mirror, etc).
ALLOWED_LEVELS = frozenset({"Complete Genome", "Chromosome"})

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
    # Per-assembly genome coverage: summed length of the kept molecules
    # as a percentage of the assembly's total size, against both the
    # gapped and ungapped genome_size from the summary row. Empty when
    # the summary row has no genome_size to divide by.
    "genome_coverage_pct", "genome_coverage_ungapped_pct",
]
FAILURE_COLUMNS = [
    "accession", "assembly_name", "stage", "reason", "detail",
]
HISTORICAL_COLUMNS = [
    "accession", "status", "replaced_by", "suppression_date", "assembly_name",
]

# Within-cell list delimiter for the merged-row TSV. Comma collides with
# commas that appear inside NCBI Sequence-Name / Assigned-Molecule values
# (e.g. GCA_014751505.1), which silently broke position alignment. Pipe is
# effectively absent from these fields; any assembly that does contain a
# pipe is skipped at write time (logged to failures) rather than emitted
# as a corrupt row. Must match build_alias_db.LIST_DELIM.
LIST_DELIM = "|"

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
    """Download with progress to stderr, atomic-rename on completion.

    Reads the SSL context fresh from the package's _ssl module so the
    --insecure flag's mutation is picked up automatically.
    """
    from alias_mapper import _ssl as _ssl_module
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=120, context=_ssl_module.SSL_CONTEXT) as r:
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
                "accession": h.accession,
                "status": h.status,
                "replaced_by": replaced_by,
                "suppression_date": h.asm_not_live_date,
                "assembly_name": h.assembly_name,
            })
            n_written += 1
    finally:
        f.close()

    print(
        f"Wrote {n_written:,} historical rows ({n_with_replacement:,} with REPLACED_BY) "
        f"-> {output_path}",
        file=sys.stderr,
    )


# Fields whose values are joined into the position-aligned list columns.
# If any value contains LIST_DELIM the encoding would break, so the
# assembly is skipped (logged) instead of emitting a corrupt row.
_LIST_SOURCE_FIELDS = (
    "Sequence-Name", "GenBank-Accn", "RefSeq-Accn",
    "UCSC-style-name", "Assigned-Molecule", "Sequence-Length",
)


def delimiter_collision(molecules: list[dict[str, str]]) -> str | None:
    """Return the first value containing LIST_DELIM, or None if all clean."""
    for m in molecules:
        for field in _LIST_SOURCE_FIELDS:
            v = m.get(field)
            if v and LIST_DELIM in v:
                return v
    return None


def merge_assembly_to_row(
    molecules: list[dict[str, str]], entry: AssemblyPlanEntry,
) -> dict[str, str]:
    """
    Collapse the per-molecule rows from one assembly into a single
    merged TSV row with position-aligned, pipe-separated lists.

    `molecules` is assumed to already be sorted (longest first, since
    adaptive_top_molecules returns them that way). Position 0 of every
    list column refers to the longest molecule.

    Empty/na values within a list are preserved as empty between
    delimiters (e.g. "chr1||chr3") so position-alignment is never broken.
    """
    def clean(v: str | None) -> str:
        if v is None or v.strip().lower() == "na":
            return ""
        return v.strip()

    def col(name: str) -> str:
        return LIST_DELIM.join(clean(m.get(name, "")) for m in molecules)

    def _length_of(m: dict[str, str]) -> int:
        try:
            return int(m.get("Sequence-Length", "0"))
        except (TypeError, ValueError):
            return 0

    # Coverage is the summed length of the molecules we kept (after the
    # adaptive cap) over the assembly's total size from the summary row.
    # This answers "what fraction of the genome do the chromosomes we
    # carry account for?" — high for chromosome-level assemblies, lower
    # where the cap trimmed a long scaffold tail. Empty when genome_size
    # is unknown (can't divide by nothing).
    summed_length = sum(_length_of(m) for m in molecules)

    def _coverage_pct(denom: int | None) -> str:
        if not denom or denom <= 0:
            return ""
        return f"{100 * summed_length / denom:.2f}"

    return {
        "genbank_acc":                  entry.genbank_acc,
        "refseq_acc":                   entry.refseq_acc or "",
        "assembly_name":                entry.assembly_name,
        "taxid":                        entry.taxid,
        "organism_name":                entry.organism_name,
        "group":                        entry.group,
        "assembly_level":               entry.assembly_level,
        "sequence_names":               col("Sequence-Name"),
        "genbank_seq_accs":             col("GenBank-Accn"),
        "refseq_seq_accs":              col("RefSeq-Accn"),
        "ucsc_names":                   col("UCSC-style-name"),
        "assigned_molecules":           col("Assigned-Molecule"),
        "lengths":                      col("Sequence-Length"),
        "genome_coverage_pct":          _coverage_pct(entry.genome_size),
        "genome_coverage_ungapped_pct": _coverage_pct(entry.genome_size_ungapped),
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
                        "accession": entry.genbank_acc,
                        "assembly_name": entry.assembly_name,
                        "stage": stage, "reason": reason, "detail": detail,
                    })
                    fail_counts[reason] = fail_counts.get(reason, 0) + 1
                    n_fail += 1
                else:
                    collision = delimiter_collision(top)
                    if collision is not None:
                        print(
                            f"  [{i}/{total}] SKIP {entry.genbank_acc} "
                            f"(delimiter in value)",
                            file=sys.stderr,
                        )
                        fail_writer.writerow({
                            "accession": entry.genbank_acc,
                            "assembly_name": entry.assembly_name,
                            "stage": "merge",
                            "reason": "delimiter_in_value",
                            "detail": f"value contains {LIST_DELIM!r}: {collision!r}",
                        })
                        fail_counts["delimiter_in_value"] = (
                            fail_counts.get("delimiter_in_value", 0) + 1
                        )
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
    parser.add_argument(
        "--num-shards", type=int, default=1,
        help="Split the plan into this many disjoint shards. Each shard is "
             "meant to run as a separate process/job; concatenate the "
             "per-shard TSVs afterward. Default 1 (no sharding).",
    )
    parser.add_argument(
        "--shard", type=int, default=0,
        help="Which shard to process, 0-indexed (0 .. num-shards-1). Shards "
             "are strided (plan[shard::num_shards]), so each gets a "
             "representative mix of assemblies and they finish at a similar "
             "time. Ignored when --num-shards is 1.",
    )
    parser.add_argument(
        "--skip-historical", action="store_true",
        help="Don't write historical.tsv.gz. Use on sharded fetch jobs "
             "(historical is shard-independent; let one job own it).",
    )
    parser.add_argument(
        "--levels", default=None,
        help="Comma-separated assembly levels to include, overriding the "
             "default (Complete Genome, Chromosome). Pass "
             "'Complete Genome,Chromosome,Scaffold' for the full set. Used "
             "by the sharded full-scale workflow.",
    )
    parser.add_argument(
        "--historical-only", action="store_true",
        help="Build the plan, write historical.tsv.gz from the full "
             "population, and exit before any per-assembly fetch. Used by "
             "the sharded workflow's catalog job. Doesn't need --output.",
    )
    args = parser.parse_args()

    if args.num_shards < 1:
        sys.exit("error: --num-shards must be >= 1")
    if not (0 <= args.shard < args.num_shards):
        sys.exit(
            f"error: --shard must be in [0, {args.num_shards}) "
            f"(got {args.shard})"
        )

    # --insecure overrides the SSL context globally by mutating the
    # package's _ssl module. _http.http_get_with_retry and our own
    # _download_to both read the context fresh from that module on
    # each call, so this single mutation propagates everywhere.
    if args.insecure:
        from alias_mapper import _ssl as _ssl_module
        _ssl_module.SSL_CONTEXT = ssl._create_unverified_context()
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
    allowed_levels = ALLOWED_LEVELS
    if args.levels:
        allowed_levels = frozenset(
            s.strip() for s in args.levels.split(",") if s.strip()
        )
        print(f"  levels override: {sorted(allowed_levels)}", file=sys.stderr)
    plan = list(build_assembly_plan(
        historicals, cache_dir, args.cache_ttl_hours, use_cache,
        allowed_levels=allowed_levels,
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

    # Keep a reference to the unsharded plan. The historical replacement
    # index must be built from the full population, not a single shard,
    # or REPLACED_BY lookups would be incomplete on sharded runs.
    full_plan = plan

    if args.num_shards > 1:
        before = len(plan)
        plan = plan[args.shard::args.num_shards]
        print(
            f"  --shard {args.shard}/{args.num_shards}: "
            f"{before:,} -> {len(plan):,} assemblies (strided)",
            file=sys.stderr,
        )

    if args.limit is not None:
        plan = plan[:args.limit]
        print(f"  --limit applied: trimmed to {len(plan):,} assemblies",
              file=sys.stderr)

    if args.dry_run:
        print("Dry run — exiting before per-assembly fetches.", file=sys.stderr)
        return 0

    # Historical-only: write just the (shard-independent) historical
    # artifact from the full plan and exit. Used by the sharded
    # workflow's catalog job, which owns historical so the fetch shards
    # don't each rewrite it. Doesn't need --output.
    if args.historical_only:
        historical_path = Path(args.historical_output or "historical.tsv.gz")
        historical_path.parent.mkdir(parents=True, exist_ok=True)
        replacement_index = build_replacement_index(full_plan)
        write_historical_tsv(historicals, replacement_index, historical_path)
        print("Historical-only mode: wrote historical, exiting.", file=sys.stderr)
        return 0

    if not args.output:
        sys.exit("error: --output required unless --dry-run or --historical-only")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    failures_path = (Path(args.failures) if args.failures
                     else output_path.parent / "failures.tsv")
    historical_path = (Path(args.historical_output) if args.historical_output
                       else output_path.parent / "historical.tsv.gz")

    # Write historical TSV first — it's pure planner output, doesn't
    # depend on any per-assembly fetching. If the alias sweep fails or
    # gets interrupted, the historical artifact is still valid.
    #
    # Built from full_plan (not the possibly-sharded plan) so REPLACED_BY
    # is complete regardless of sharding. Skippable so sharded fetch jobs
    # don't each redundantly rewrite the same shard-independent file.
    if args.skip_historical:
        print("Skipping historical TSV (--skip-historical).", file=sys.stderr)
    else:
        replacement_index = build_replacement_index(full_plan)
        write_historical_tsv(historicals, replacement_index, historical_path)

    print(f"Starting per-assembly collection ({len(plan):,} assemblies, {args.workers} workers)...",
          file=sys.stderr)
    run_collection(plan, output_path, failures_path, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

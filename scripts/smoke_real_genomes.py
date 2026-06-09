#!/usr/bin/env python3
"""
smoke_real_genomes.py
---------------------
End-to-end smoke test against real files from the major repositories.
Unlike the unit suite (synthetic fixtures), this exercises the CLI on
actual headers in the wild, including gzip and each source's naming quirks.

Genome files are huge (hundreds of MB to ~1.6 Gb), so we never download a
whole one: we stream the gzip, decompress on the fly, and stop after the
first N sequences OR a byte cap, whichever comes first.

Sampling caveats (both real, both about the slice, not the tool):
  - We take the first N records in FILE ORDER, while the alias DB keeps only
    the longest molecules (the builder's coverage cap). For scaffold-level
    assemblies the file-order-first scaffolds may not all be among the kept
    longest, so some sampled names legitimately won't map (you'll see a
    nonzero `unmapped`).
  - For CHROMOSOME-level assemblies a single record can be tens of MB
    compressed, so the byte cap can stop the stream after just 1-2 records —
    too few to clear auto-detection's 5-match threshold. For those, give the
    source explicit `from_`/`assembly` so detection is skipped and even one
    record proves translation works.

State of Emilio's three links against the full ~48k set:
  - NCBI GCA_001039765.2 (AptMant0, scaffold-level): present and translates.
  - UCSC aptMan1: names sequences in `NW_...v1` form (dot -> v); the DB stores
    the `.1` form, and only ~44 assemblies carry UCSC names, so it won't match
    without a `vN`<->`.N` normalization step.
  - ENA api/fasta/<GCA>: returns an unrelated single record, not the
    assembly's sequences; ENA also prefixes headers `ENA|ACC|ACC.v`.

On a TLS-inspecting network (e.g. CRG's), install the system-trust extra so
HTTPS verification succeeds:  pip install -e .[trusted]

Usage:
    python scripts/smoke_real_genomes.py
    python scripts/smoke_real_genomes.py --only kiwi --records 100
"""

import argparse
import os
import re
import subprocess
import sys
import zlib
from pathlib import Path
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# Reuse the package's SSL setup (truststore > certifi > stdlib defaults) so
# this harness gets through TLS-inspecting networks exactly like the CLI's
# own downloader. If the package can't be imported here, fall back to
# urllib's default context (None).
sys.path.insert(0, str(SRC))
try:
    from alias_mapper._ssl import SSL_CONTEXT
except Exception:                                # noqa: BLE001
    SSL_CONTEXT = None

USER_AGENT = "alias-mapper-smoke (+https://github.com/guigolab/alias-mapper)"
CHUNK = 256 * 1024
_GZIP_MAGIC = b"\x1f\x8b"

SOURCES = [
    # Emilio's three links.
    {
        "id": "ucsc",
        "label": "UCSC aptMan1 (NW_...v1 names; needs vN<->.N normalization)",
        "url": "https://hgdownload.soe.ucsc.edu/goldenPath/aptMan1/bigZips/aptMan1.fa.gz",
        "to": "genbank",
    },
    {
        "id": "ncbi",
        "label": "NCBI GCA_001039765.2 AptMant0 (scaffold-level; auto-detect)",
        "url": ("https://ftp.ncbi.nlm.nih.gov/genomes/all/GCA/001/039/765/"
                "GCA_001039765.2_AptMant0/GCA_001039765.2_AptMant0_genomic.fna.gz"),
        "to": "sequence-name",
    },
    {
        "id": "ena",
        "label": "ENA GCA_014905855.1 via api/fasta (endpoint returns wrong data)",
        "url": "https://www.ebi.ac.uk/ena/browser/api/fasta/GCA_014905855.1?download=true&gzip=true",
        "to": "sequence-name",
    },
    # Positive control, chromosome-level. One chromosome easily exceeds the
    # byte cap, so we can't collect enough names to auto-detect; pass
    # explicit from/assembly so a single record still proves translation.
    {
        "id": "kiwi",
        "label": "NCBI GCA_036417845.1 bAptMan1.hap1 (chromosome-level; explicit flags)",
        "url": ("https://ftp.ncbi.nlm.nih.gov/genomes/all/GCA/036/417/845/"
                "GCA_036417845.1_bAptMan1.hap1/GCA_036417845.1_bAptMan1.hap1_genomic.fna.gz"),
        "to": "sequence-name",
        "from_": "genbank",
        "assembly": "GCA_036417845.1",
    },
]


def count_headers(text: str) -> int:
    """Number of FASTA header lines ('>' at start of a line) in text."""
    return sum(1 for line in text.splitlines() if line.startswith(">"))


def take_first_records(text: str, n: int) -> str:
    """Return the leading whole FASTA records (up to n) from text."""
    out, headers = [], 0
    for line in text.splitlines(keepends=True):
        if line.startswith(">"):
            headers += 1
            if headers > n:
                break
        out.append(line)
    return "".join(out)


def stream_first_records(url: str, n_records: int, max_bytes: int) -> tuple[str, int]:
    """
    Stream `url`, decompressing gzip on the fly, and return the text of the
    first `n_records` FASTA records plus the number of compressed bytes read.

    Stops as soon as enough records have arrived (or max_bytes is hit), so a
    multi-GB file costs only a few MB of transfer. Handles a server that
    serves plain (non-gzip) bytes by sniffing the magic number.
    """
    req = Request(url, headers={"User-Agent": USER_AGENT})
    text_parts: list[str] = []
    downloaded = 0
    decomp = None
    sniffed = False
    is_gz = False

    with urlopen(req, timeout=120, context=SSL_CONTEXT) as resp:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            downloaded += len(chunk)

            if not sniffed:
                is_gz = chunk[:2] == _GZIP_MAGIC
                if is_gz:
                    decomp = zlib.decompressobj(zlib.MAX_WBITS | 16)
                sniffed = True

            piece = decomp.decompress(chunk) if is_gz else chunk
            if piece:
                text_parts.append(
                    piece.decode("utf-8", errors="replace")
                    if isinstance(piece, bytes) else piece
                )
                joined = "".join(text_parts)
                if count_headers(joined) > n_records or downloaded > max_bytes:
                    break

    return take_first_records("".join(text_parts), n_records), downloaded


def peek_headers(text: str, k: int = 5) -> list[str]:
    """The first k header lines, for showing the real format."""
    return [ln for ln in text.splitlines() if ln.startswith(">")][:k]


def parse_cli_stderr(stderr: str) -> dict:
    """Pull detected convention/assembly and mapped/unmapped out of CLI logs."""
    info = {}
    m = re.search(r"detected source convention:\s*(\S+)", stderr)
    if m:
        info["convention"] = m.group(1)
    m = re.search(r"detected assembly:\s*(\S+)", stderr)
    if m:
        info["assembly"] = m.group(1)
    m = re.search(r"mapped=(\d+),\s*unmapped=(\d+)", stderr)
    if m:
        info["mapped"] = int(m.group(1))
        info["unmapped"] = int(m.group(2))
    return info


def run_one(source: dict, data_dir: Path, n_records: int, max_bytes: int) -> dict:
    print(f"\n{'='*70}\n{source['id'].upper()}: {source['label']}\n{'='*70}")
    slice_path = data_dir / f"{source['id']}_slice.fa"
    out_path = data_dir / f"{source['id']}_converted.fa"
    out_path.unlink(missing_ok=True)

    print(f"  streaming first {n_records} records from:\n    {source['url']}")
    try:
        text, nbytes = stream_first_records(source["url"], n_records, max_bytes)
    except Exception as e:                       # noqa: BLE001 - report any network/decode error
        msg = str(e)
        print(f"  DOWNLOAD FAILED: {msg}")
        if "CERTIFICATE_VERIFY" in msg:
            print("  hint: on a TLS-inspecting network (e.g. CRG), install the "
                  "system-trust extra:  pip install -e .[trusted]")
        return {"id": source["id"], "status": "download-failed", "detail": msg}

    n_headers = count_headers(text)
    slice_path.write_text(text, encoding="utf-8")
    print(f"  got {n_headers} records in ~{nbytes/1e6:.1f} MB transferred "
          f"-> {slice_path}")
    print("  first headers:")
    for h in peek_headers(text):
        print(f"    {h[:100]}")

    if n_headers == 0:
        return {"id": source["id"], "status": "no-records"}

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "alias_mapper.cli", "convert",
           str(slice_path), "--to", source["to"]]
    if source.get("from_"):
        cmd += ["--from", source["from_"]]
    if source.get("assembly"):
        cmd += ["--assembly", source["assembly"]]
    cmd += ["-o", str(out_path)]

    flag_note = ""
    if source.get("from_") or source.get("assembly"):
        flag_note = (f"  (--from {source.get('from_','auto')} "
                     f"--assembly {source.get('assembly','auto')})")
    print(f"  running: alias-mapper convert <slice> --to {source['to']}{flag_note}")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)

    info = parse_cli_stderr(proc.stderr)
    # When flags are explicit the CLI won't print "detected ..." lines, so
    # show what we passed instead of "?".
    info.setdefault("convention", source.get("from_", "?"))
    info.setdefault("assembly", source.get("assembly", "?"))

    if proc.returncode != 0:
        err = next((ln for ln in reversed(proc.stderr.splitlines()) if ln.strip()), "")
        print(f"  CLI exited {proc.returncode}: {err}")
        return {"id": source["id"], "status": "cli-error", "detail": err, **info}

    print(f"  detected: convention={info.get('convention','?')}, "
          f"assembly={info.get('assembly','?')}")
    print(f"  result:   mapped={info.get('mapped','?')}, "
          f"unmapped={info.get('unmapped','?')}")
    status = "ok" if info.get("mapped", 0) > 0 else "zero-mapped"
    return {"id": source["id"], "status": status, **info}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=[s["id"] for s in SOURCES],
                    help="Run just one source.")
    ap.add_argument("--records", type=int, default=60,
                    help="Records to slice from each file (default 60).")
    ap.add_argument("--max-mb", type=float, default=50.0,
                    help="Hard cap on compressed bytes streamed per file (default 50).")
    ap.add_argument("--data-dir", type=Path, default=Path("smoke-data"),
                    help="Where to write slices and outputs (default ./smoke-data).")
    args = ap.parse_args()

    args.data_dir.mkdir(parents=True, exist_ok=True)
    sources = [s for s in SOURCES if not args.only or s["id"] == args.only]
    max_bytes = int(args.max_mb * 1e6)

    results = [run_one(s, args.data_dir, args.records, max_bytes) for s in sources]

    print(f"\n{'='*70}\nSUMMARY\n{'='*70}")
    for r in results:
        line = f"  {r['id']:5s}  {r['status']}"
        if "mapped" in r:
            line += f"  (mapped={r['mapped']}, unmapped={r['unmapped']})"
        if r.get("detail"):
            line += f"  - {r['detail'][:80]}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

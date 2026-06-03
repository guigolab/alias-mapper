"""
End-to-end smoke test: run the actual CLI as a subprocess against the
tiny fixture DB, for both plain and gzipped files.

Explicit --from/--to/--assembly avoid detection ambiguity on tiny data,
and a prebuilt --alias-db means the network bootstrap path is never hit.
"""

import gzip
import os
import subprocess
import sys
from pathlib import Path

from alias_mapper.formats import is_gzip

# Repo src dir, so the subprocess can import alias_mapper whether or not
# the package is pip-installed in the CI environment.
SRC = Path(__file__).resolve().parents[1] / "src"


def run_cli(args, extra_env=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "alias_mapper.cli", *args],
        env=env, capture_output=True, text=True,
    )


def test_cli_converts_plain_gff(tmp_path, built_db):
    inp = tmp_path / "in.gff"
    inp.write_text(
        "##gff-version 3\n"
        "NC_0001.1\ts\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        "NC_0002.1\ts\tgene\t1\t100\t.\t+\t.\tID=g2\n"
        "BADNAME\ts\tgene\t1\t100\t.\t+\t.\tID=g3\n"
    )
    out = tmp_path / "out.gff"
    res = run_cli([
        "convert", str(inp),
        "--from", "refseq", "--to", "ucsc",
        "--assembly", "GCA_000001.1",
        "--alias-db", str(built_db),
        "-o", str(out),
    ])
    assert res.returncode == 0, res.stderr
    lines = out.read_text().splitlines()
    assert lines[0] == "##gff-version 3"          # comment preserved
    assert lines[1].startswith("chr1\t")          # NC_0001.1 -> chr1
    assert lines[2].startswith("chr2\t")          # NC_0002.1 -> chr2
    assert lines[3].startswith("BADNAME\t")       # unmapped, passed through


def test_cli_converts_gzipped_in_and_out(tmp_path, built_db):
    inp = tmp_path / "in.gff.gz"
    with gzip.open(inp, "wt") as f:
        f.write(
            "NC_0003.1\ts\tgene\t1\t9\t.\t+\t.\tID=a\n"
            "NC_0004.1\ts\tgene\t1\t9\t.\t+\t.\tID=b\n"
        )
    out = tmp_path / "out.gff.gz"
    res = run_cli([
        "convert", str(inp),
        "--from", "refseq", "--to", "ucsc",
        "--assembly", "GCA_000001.1",
        "--alias-db", str(built_db),
        "-o", str(out),
    ])
    assert res.returncode == 0, res.stderr
    assert is_gzip(out)                            # output really is gzipped
    with gzip.open(out, "rt") as f:
        lines = f.read().splitlines()
    assert lines[0].startswith("chr3\t")
    assert lines[1].startswith("chr4\t")

"""
End-to-end tests for multi-file (--fasta) convert mode, via subprocess.

The assembly is detected once from the FASTA. Without --overwrite-to the
annotations are conformed to the FASTA's own convention (FASTA left
unchanged); with --overwrite-to the FASTA and every annotation are forced
to the target. GCA_000001.1 in the fixture has 6 molecules, so a FASTA
with 6 names clears the auto-detection threshold without explicit flags.
"""

import gzip
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def run_cli(args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "alias_mapper.cli", *args],
        env=env, capture_output=True, text=True,
    )


def _is_gzip(path):
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


# --- overwrite mode (--overwrite-to) -------------------------------------

def test_overwrite_detects_once_and_converts_all(tmp_path, built_db):
    ref = tmp_path / "ref.fa"
    ref.write_text("".join(f">NC_000{i}.1 desc {i}\nACGT\n" for i in range(1, 7)))
    ann1 = tmp_path / "ann1.gff"
    ann1.write_text(
        "NC_0001.1\ts\tgene\t1\t9\t.\t+\t.\tID=a\n"
        "NC_0002.1\ts\tgene\t1\t9\t.\t+\t.\tID=b\n"
        "ZZZ\ts\tgene\t1\t9\t.\t+\t.\tID=c\n"
    )
    ann2 = tmp_path / "ann2.gff.gz"
    with gzip.open(ann2, "wt") as f:
        f.write("NC_0003.1\ts\tgene\t1\t9\t.\t+\t.\tID=a\n")

    out = tmp_path / "out"
    res = run_cli([
        "convert", str(ann1), str(ann2),
        "--fasta", str(ref),
        "--overwrite-to", "ucsc",
        "--alias-db", str(built_db),
        "--out-dir", str(out),
    ])
    assert res.returncode == 0, res.stderr

    # Detection ran once, from the FASTA, as the source convention.
    assert "detected source convention: refseq" in res.stderr
    assert "detected assembly: GCA_000001.1" in res.stderr
    assert "mode=overwrite" in res.stderr

    fa_out = out / "ref.ucsc.fa"
    g1 = out / "ann1.ucsc.gff"
    g2 = out / "ann2.ucsc.gff.gz"
    assert fa_out.exists() and g1.exists() and g2.exists()

    # FASTA headers translated too (overwrite mode touches the FASTA).
    fa_txt = fa_out.read_text()
    assert ">chr1" in fa_txt and ">chr6" in fa_txt

    # Plain GFF: maps + unmapped passthrough.
    g1_lines = g1.read_text().splitlines()
    assert g1_lines[0].startswith("chr1\t")
    assert g1_lines[1].startswith("chr2\t")
    assert g1_lines[2].startswith("ZZZ\t")

    # Gzipped annotation -> gzipped output, mapped.
    assert _is_gzip(g2)
    with gzip.open(g2, "rt") as f:
        assert f.read().splitlines()[0].startswith("chr3\t")


def test_overwrite_fasta_only_converts_just_the_fasta(tmp_path, built_db):
    ref = tmp_path / "ref.fa"
    ref.write_text("".join(f">NC_000{i}.1\nACGT\n" for i in range(1, 7)))
    out = tmp_path / "out"
    res = run_cli([
        "convert", "--fasta", str(ref), "--overwrite-to", "ucsc",
        "--alias-db", str(built_db), "--out-dir", str(out),
    ])
    assert res.returncode == 0, res.stderr
    assert (out / "ref.ucsc.fa").exists()


# --- conform mode (no --to / --overwrite-to) -----------------------------

def test_conform_conforms_annotations_and_leaves_fasta(tmp_path, built_db):
    # FASTA is in UCSC convention; the annotation is in RefSeq. Conform
    # should rewrite the annotation to UCSC and not touch the FASTA.
    ref = tmp_path / "ref.fa"
    ref.write_text("".join(f">chr{i}\nACGT\n" for i in range(1, 7)))
    ann = tmp_path / "ann.gff"
    ann.write_text(
        "NC_0001.1\ts\tgene\t1\t9\t.\t+\t.\tID=a\n"
        "NC_0002.1\ts\tgene\t1\t9\t.\t+\t.\tID=b\n"
    )
    out = tmp_path / "out"
    res = run_cli([
        "convert", str(ann), "--fasta", str(ref),
        "--alias-db", str(built_db), "--out-dir", str(out),
    ])
    assert res.returncode == 0, res.stderr
    assert "FASTA convention (conform target): ucsc" in res.stderr
    assert "mode=conform" in res.stderr

    g = out / "ann.ucsc.gff"
    assert g.exists()
    lines = g.read_text().splitlines()
    assert lines[0].startswith("chr1\t")
    assert lines[1].startswith("chr2\t")

    # FASTA is the untouched reference: not written to the output dir.
    assert not (out / "ref.ucsc.fa").exists()


def test_conform_passthrough_reads_as_note_not_warning(tmp_path, built_db):
    # An annotation already in the FASTA's convention passes through
    # unchanged; in conform mode that is reported as a note, not a warning.
    ref = tmp_path / "ref.fa"
    ref.write_text("".join(f">chr{i}\nACGT\n" for i in range(1, 7)))
    ann = tmp_path / "ann.gff"
    ann.write_text("chr1\ts\tgene\t1\t9\t.\t+\t.\tID=a\n")
    out = tmp_path / "out"
    res = run_cli([
        "convert", str(ann), "--fasta", str(ref),
        "--alias-db", str(built_db), "--out-dir", str(out),
    ])
    assert res.returncode == 0, res.stderr
    assert "note:" in res.stderr
    assert "warning:" not in res.stderr
    assert (out / "ann.ucsc.gff").read_text().splitlines()[0].startswith("chr1\t")


# --- flag-combination errors ---------------------------------------------

def test_to_flag_rejected_in_fasta_mode(tmp_path, built_db):
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\nACGT\n")
    res = run_cli([
        "convert", "--fasta", str(ref), "--to", "ucsc",
        "--alias-db", str(built_db), "--out-dir", str(tmp_path / "o"),
    ])
    assert res.returncode != 0
    assert "overwrite-to" in res.stderr


def test_from_flag_rejected_in_conform_mode(tmp_path, built_db):
    ref = tmp_path / "ref.fa"
    ref.write_text("".join(f">chr{i}\nACGT\n" for i in range(1, 7)))
    ann = tmp_path / "ann.gff"
    ann.write_text("NC_0001.1\ts\tg\t1\t9\t.\t+\t.\tID=a\n")
    res = run_cli([
        "convert", str(ann), "--fasta", str(ref), "--from", "refseq",
        "--alias-db", str(built_db), "--out-dir", str(tmp_path / "o"),
    ])
    assert res.returncode != 0
    assert "--from is not used" in res.stderr


def test_conform_needs_at_least_one_annotation(tmp_path, built_db):
    ref = tmp_path / "ref.fa"
    ref.write_text("".join(f">chr{i}\nACGT\n" for i in range(1, 7)))
    res = run_cli([
        "convert", "--fasta", str(ref),
        "--alias-db", str(built_db), "--out-dir", str(tmp_path / "o"),
    ])
    assert res.returncode != 0
    assert "at least one annotation" in res.stderr


def test_output_flag_rejected_in_fasta_mode(tmp_path, built_db):
    ref = tmp_path / "ref.fa"
    ref.write_text(">NC_0001.1\nACGT\n")
    res = run_cli([
        "convert", "--fasta", str(ref), "--overwrite-to", "ucsc",
        "--alias-db", str(built_db), "-o", str(tmp_path / "x.fa"),
    ])
    assert res.returncode != 0
    assert "single-file mode" in res.stderr


def test_out_dir_rejected_in_single_mode(tmp_path, built_db):
    inp = tmp_path / "in.gff"
    inp.write_text("NC_0001.1\ts\tgene\t1\t9\t.\t+\t.\tID=a\n")
    res = run_cli([
        "convert", str(inp), "--to", "ucsc",
        "--alias-db", str(built_db), "--out-dir", str(tmp_path / "z"),
    ])
    assert res.returncode != 0
    assert "--fasta" in res.stderr


def test_two_inputs_rejected_in_single_mode(tmp_path, built_db):
    a = tmp_path / "a.gff"; a.write_text("NC_0001.1\ts\tg\t1\t9\t.\t+\t.\tID=a\n")
    b = tmp_path / "b.gff"; b.write_text("NC_0002.1\ts\tg\t1\t9\t.\t+\t.\tID=b\n")
    res = run_cli([
        "convert", str(a), str(b), "--to", "ucsc",
        "--alias-db", str(built_db), "-o", str(tmp_path / "y.gff"),
    ])
    assert res.returncode != 0
    assert "exactly one" in res.stderr

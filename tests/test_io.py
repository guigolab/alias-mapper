"""Tests for the transparent gzip I/O helpers (formats/_io.py)."""

import gzip

from alias_mapper.formats import (
    is_gzip,
    open_text_read,
    open_text_write,
    effective_suffix,
)


def test_is_gzip_plain_file(tmp_path):
    p = tmp_path / "plain.txt"
    p.write_text("hello\n")
    assert is_gzip(p) is False


def test_is_gzip_gzipped_file(tmp_path):
    p = tmp_path / "data.txt.gz"
    with gzip.open(p, "wt") as f:
        f.write("hello\n")
    assert is_gzip(p) is True


def test_is_gzip_missing_file(tmp_path):
    # A path that doesn't exist is reported not-gzip (the caller raises a
    # clearer not-found error later), rather than blowing up here.
    assert is_gzip(tmp_path / "nope") is False


def test_open_text_read_handles_both(tmp_path):
    plain = tmp_path / "p.txt"
    plain.write_text("a\nb\n")
    gz = tmp_path / "g.txt.gz"
    with gzip.open(gz, "wt") as f:
        f.write("a\nb\n")
    with open_text_read(plain) as f:
        assert f.read() == "a\nb\n"
    with open_text_read(gz) as f:
        assert f.read() == "a\nb\n"


def test_open_text_read_detects_gzip_without_suffix(tmp_path):
    # Content sniffing, not the extension: a gzipped file named ".txt"
    # is still read decompressed.
    p = tmp_path / "mislabeled.txt"
    with gzip.open(p, "wt") as f:
        f.write("x\n")
    with open_text_read(p) as f:
        assert f.read() == "x\n"


def test_open_text_write_compresses_on_gz(tmp_path):
    p = tmp_path / "out.txt.gz"
    with open_text_write(p) as f:
        f.write("z\n")
    assert is_gzip(p) is True
    with gzip.open(p, "rt") as f:
        assert f.read() == "z\n"


def test_open_text_write_plain_otherwise(tmp_path):
    p = tmp_path / "out.txt"
    with open_text_write(p) as f:
        f.write("z\n")
    assert is_gzip(p) is False
    assert p.read_text() == "z\n"


def test_effective_suffix():
    assert effective_suffix("genome.fa.gz") == ".fa"
    assert effective_suffix("genome.gff") == ".gff"
    assert effective_suffix("X.GFF3.GZ") == ".gff3"   # case-insensitive
    assert effective_suffix("bare.gz") == ""

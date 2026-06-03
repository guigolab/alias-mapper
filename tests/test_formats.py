"""Tests for translator dispatch and the GFF/FASTA line translators."""

import gzip

import pytest

from alias_mapper.formats import translator_for, GffTranslator, FastaTranslator


def fresh_stats():
    return {"mapped": 0, "unmapped": 0, "unmapped_examples": set()}


# --- translator_for ------------------------------------------------------

def test_translator_for_resolves_extensions():
    assert isinstance(translator_for("x.fa.gz"), FastaTranslator)
    assert isinstance(translator_for("x.fasta"), FastaTranslator)
    assert isinstance(translator_for("x.gff.gz"), GffTranslator)
    assert isinstance(translator_for("x.gff3"), GffTranslator)
    assert isinstance(translator_for("x.gtf"), GffTranslator)


def test_translator_for_unknown_raises():
    with pytest.raises(ValueError):
        translator_for("x.txt")


# --- GFF translate_line --------------------------------------------------

def test_gff_maps_column_one():
    g = GffTranslator()
    st = fresh_stats()
    line = "NC_0001.1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1\n"
    out = g.translate_line(line, {"NC_0001.1": "chr1"}, st)
    assert out == "chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1\n"
    assert st["mapped"] == 1 and st["unmapped"] == 0


def test_gff_unmapped_passthrough_and_stats():
    g = GffTranslator()
    st = fresh_stats()
    line = "BADNAME\tsrc\tgene\t1\t9\t.\t+\t.\tID=x\n"
    out = g.translate_line(line, {"NC_0001.1": "chr1"}, st)
    assert out == line  # unchanged
    assert st["unmapped"] == 1
    assert "BADNAME" in st["unmapped_examples"]


def test_gff_comment_passthrough():
    g = GffTranslator()
    st = fresh_stats()
    assert g.translate_line("##gff-version 3\n", {}, st) == "##gff-version 3\n"
    assert st["mapped"] == 0 and st["unmapped"] == 0


# --- FASTA translate_line ------------------------------------------------

def test_fasta_maps_and_preserves_description():
    fa = FastaTranslator()
    st = fresh_stats()
    out = fa.translate_line(">NC_0001.1 Homo sapiens chr1\n", {"NC_0001.1": "chr1"}, st)
    assert out == ">chr1 Homo sapiens chr1\n"
    assert st["mapped"] == 1


def test_fasta_preserves_exact_whitespace():
    fa = FastaTranslator()
    st = fresh_stats()
    # Two spaces and a tab between name and description must survive.
    out = fa.translate_line(">NC_0001.1  \tdesc\n", {"NC_0001.1": "chr1"}, st)
    assert out == ">chr1  \tdesc\n"


def test_fasta_sequence_line_passthrough():
    fa = FastaTranslator()
    st = fresh_stats()
    assert fa.translate_line("ACGTACGT\n", {"NC_0001.1": "chr1"}, st) == "ACGTACGT\n"


def test_fasta_unmapped_passthrough():
    fa = FastaTranslator()
    st = fresh_stats()
    assert fa.translate_line(">ZZZ\n", {"NC_0001.1": "chr1"}, st) == ">ZZZ\n"
    assert st["unmapped"] == 1


def test_fasta_malformed_header_passthrough():
    fa = FastaTranslator()
    st = fresh_stats()
    # A header with no name ('>' alone) has nothing to translate.
    assert fa.translate_line(">\n", {}, st) == ">\n"
    assert st["mapped"] == 0 and st["unmapped"] == 0


# --- sample_names (plain + gzipped) --------------------------------------

def test_gff_sample_names_plain(tmp_path):
    p = tmp_path / "s.gff"
    p.write_text("# header\nNC_0001.1\ta\nNC_0002.1\tb\nNC_0001.1\tc\n")
    assert GffTranslator().sample_names(p) == ["NC_0001.1", "NC_0002.1"]


def test_gff_sample_names_gzipped(tmp_path):
    p = tmp_path / "s.gff.gz"
    with gzip.open(p, "wt") as f:
        f.write("NC_0003.1\ta\nNC_0004.1\tb\n")
    assert GffTranslator().sample_names(p) == ["NC_0003.1", "NC_0004.1"]


def test_fasta_sample_names_gzipped(tmp_path):
    p = tmp_path / "s.fa.gz"
    with gzip.open(p, "wt") as f:
        f.write(">NC_0001.1 desc\nACGT\n>NC_0002.1\nTTTT\n")
    assert FastaTranslator().sample_names(p) == ["NC_0001.1", "NC_0002.1"]

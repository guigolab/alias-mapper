"""Tests for the TSV-to-SQLite builder (_explode_row and build_db)."""

import csv
import sqlite3

import pytest

from alias_mapper import build_alias_db as bdb
from conftest import HEADER, ROW_A, ROW_B


def row_dict(tsv_line):
    """Parse one merged TSV line into the dict _explode_row expects."""
    return next(csv.DictReader([HEADER, tsv_line], delimiter="\t"))


# --- _explode_row --------------------------------------------------------

def test_explode_row_a_full_coverage():
    asm, mols = bdb._explode_row(row_dict(ROW_A))
    assert asm["accession"] == "GCA_000001.1"
    assert asm["paired_refseq_acc"] == "GCF_000001.1"
    assert asm["genome_coverage_pct"] == 99.5
    assert asm["genome_coverage_ungapped_pct"] == 99.8
    assert len(mols) == 6
    # Tuple order: (accession, pos, sequence_name, assigned_molecule,
    #               genbank_acc, refseq_acc, ucsc_name, length)
    assert mols[0] == (
        "GCA_000001.1", 0, "1", "1", "CM0001.1", "NC_0001.1", "chr1", 6000,
    )
    assert mols[5][1] == 5 and mols[5][6] == "chr6"


def test_explode_row_b_sparse_columns():
    asm, mols = bdb._explode_row(row_dict(ROW_B))
    assert len(mols) == 1
    mol = mols[0]
    assert mol[4] == "CM9999.1"     # genbank_acc present
    assert mol[5] is None           # refseq_acc empty -> None
    assert mol[6] is None           # ucsc_name empty -> None
    assert mol[7] == 500
    assert asm["paired_refseq_acc"] is None
    assert asm["genome_coverage_ungapped_pct"] is None


def test_explode_row_misaligned_lists_raise():
    bad = dict(row_dict(ROW_A))
    bad["sequence_names"] = "1|2"      # length 2
    bad["genbank_seq_accs"] = "X"      # length 1
    bad["refseq_seq_accs"] = ""
    bad["ucsc_names"] = ""
    bad["assigned_molecules"] = ""
    bad["lengths"] = ""
    with pytest.raises(ValueError):
        bdb._explode_row(bad)


def test_explode_row_missing_genbank_acc_raises():
    miss = dict(row_dict(ROW_A))
    miss["genbank_acc"] = ""
    with pytest.raises(ValueError):
        bdb._explode_row(miss)


# --- build_db ------------------------------------------------------------

def test_build_db_creates_expected_rows(built_db):
    conn = sqlite3.connect(built_db)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM _meta WHERE key='schema_version'")
        assert cur.fetchone()[0] == "3"
        cur.execute("SELECT COUNT(*) FROM aliases")
        assert cur.fetchone()[0] == 7        # 6 from A + 1 from B
        cur.execute("SELECT COUNT(*) FROM assemblies")
        assert cur.fetchone()[0] == 2
    finally:
        conn.close()

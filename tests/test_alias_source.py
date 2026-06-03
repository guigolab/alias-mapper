"""Tests for SqliteAliasSource: lookups, detection, and schema checks."""

import shutil
import sqlite3

import pytest

from alias_mapper.alias_source import (
    SqliteAliasSource,
    AliasNotFoundError,
    AssemblyNotFoundError,
    StaleSchemaError,
    LowConfidenceDetection,
    verify_schema_version,
    _is_confident,
)

# Six RefSeq names that all belong to GCA_000001.1. Six clears the
# absolute-match threshold (5), so detection is confident.
REFSEQ_SAMPLE = [f"NC_000{i}.1" for i in range(1, 7)]


# --- get_map -------------------------------------------------------------

def test_get_map_refseq_to_ucsc(built_db):
    src = SqliteAliasSource(built_db)
    m = src.get_map("GCA_000001.1", "refseq_acc", "ucsc_name")
    assert m == {f"NC_000{i}.1": f"chr{i}" for i in range(1, 7)}


def test_get_map_b_genbank_to_sequence_name(built_db):
    src = SqliteAliasSource(built_db)
    assert src.get_map("GCA_000002.1", "genbank_acc", "sequence_name") == {"CM9999.1": "I"}


def test_get_map_skips_null_pairs(built_db):
    # GCA_000002.1's only molecule has neither refseq nor ucsc, so there
    # are no rows with both populated -> AliasNotFoundError.
    src = SqliteAliasSource(built_db)
    with pytest.raises(AliasNotFoundError):
        src.get_map("GCA_000002.1", "refseq_acc", "ucsc_name")


def test_get_map_unknown_assembly_raises(built_db):
    src = SqliteAliasSource(built_db)
    with pytest.raises(AssemblyNotFoundError):
        src.get_map("GCA_999999.9", "refseq_acc", "ucsc_name")


def test_assembly_exists(built_db):
    src = SqliteAliasSource(built_db)
    assert src.assembly_exists("GCA_000001.1") is True
    assert src.assembly_exists("GCA_999999.9") is False


# --- detection -----------------------------------------------------------

def test_detect_convention_picks_refseq(built_db):
    src = SqliteAliasSource(built_db)
    result = src.detect_convention(REFSEQ_SAMPLE)
    assert result.winner == "refseq_acc"
    assert result.winner_score == 6


def test_detect_assembly_picks_correct_assembly(built_db):
    src = SqliteAliasSource(built_db)
    result = src.detect_assembly(REFSEQ_SAMPLE)
    assert result.winner == "GCA_000001.1"
    assert result.winner_score == 6


def test_detect_convention_low_confidence_raises(built_db):
    src = SqliteAliasSource(built_db)
    # A single matching name is below the absolute-match threshold.
    with pytest.raises(LowConfidenceDetection):
        src.detect_convention(["NC_0001.1"])


# --- confidence rule -----------------------------------------------------

def test_is_confident_rule():
    assert _is_confident(6, 0) is True     # clear winner, no runner-up
    assert _is_confident(4, 0) is False    # below absolute minimum (5)
    assert _is_confident(6, 3) is True     # ratio exactly 2.0
    assert _is_confident(6, 4) is False    # ratio 1.5, too close


# --- schema verification -------------------------------------------------

def test_verify_schema_version_accepts_fresh_db(built_db):
    # Should not raise on a freshly built v3 DB.
    verify_schema_version(built_db)


def test_verify_schema_version_rejects_stale(built_db, tmp_path):
    stale = tmp_path / "stale.db"
    shutil.copy(built_db, stale)
    conn = sqlite3.connect(stale)
    conn.execute("UPDATE _meta SET value='2' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(StaleSchemaError) as exc:
        verify_schema_version(stale)
    assert exc.value.found == 2


def test_verify_schema_version_rejects_db_without_meta(tmp_path):
    bare = tmp_path / "bare.db"
    conn = sqlite3.connect(bare)
    conn.execute("CREATE TABLE other (a)")   # no _meta table
    conn.commit()
    conn.close()
    with pytest.raises(StaleSchemaError) as exc:
        verify_schema_version(bare)
    assert exc.value.found is None

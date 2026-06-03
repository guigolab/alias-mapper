"""
Shared pytest fixtures.

Builds a tiny but realistic alias database from an in-test TSV so the
suite never touches the network or the real (large) data release. Two
assemblies:

  - GCA_000001.1: 6 molecules, full convention coverage (used for the
    detection and happy-path lookup tests; 6 matches clears the
    confidence threshold of 5).
  - GCA_000002.1: 1 molecule, no RefSeq/UCSC (used to exercise NULL
    handling and the empty-list-column path).
"""

import gzip

import pytest

from alias_mapper.build_alias_db import build_db, EXPECTED_TSV_COLS

HEADER = "\t".join(EXPECTED_TSV_COLS)

ROW_A = "\t".join([
    "GCA_000001.1", "GCF_000001.1", "asmA", "9606", "Homo sapiens",
    "vertebrate_mammalian", "Chromosome",
    "1|2|3|4|5|6", "CM0001.1|CM0002.1|CM0003.1|CM0004.1|CM0005.1|CM0006.1",
    "NC_0001.1|NC_0002.1|NC_0003.1|NC_0004.1|NC_0005.1|NC_0006.1",
    "chr1|chr2|chr3|chr4|chr5|chr6", "1|2|3|4|5|6",
    "6000|5000|4000|3000|2000|1000", "99.5", "99.8",
])

# No paired RefSeq, no per-molecule RefSeq, no UCSC, no ungapped coverage.
ROW_B = "\t".join([
    "GCA_000002.1", "", "asmB", "4932", "Saccharomyces cerevisiae",
    "fungi", "Scaffold",
    "I", "CM9999.1", "", "", "I", "500", "80.0", "",
])


@pytest.fixture
def tiny_tsv(tmp_path):
    """Write the two-assembly fixture TSV (gzipped) and return its path."""
    path = tmp_path / "aliases.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(HEADER + "\n" + ROW_A + "\n" + ROW_B + "\n")
    return path


@pytest.fixture
def built_db(tmp_path, tiny_tsv):
    """Build the SQLite DB from the fixture TSV and return its path."""
    db = tmp_path / "aliases.db"
    build_db(tiny_tsv, db)
    return db

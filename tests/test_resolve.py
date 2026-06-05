"""
Tests for the fallback name resolver (formats/_resolve.py) and its
integration through the GFF and FASTA translators.

The resolver tries an exact map hit first, then conservative
normalizations (ENA prefix strip, .N/vN version-separator swap) only on
a miss. These are surface-form normalizations of the same accession, so
a fallback hit is never a fuzzy guess.
"""

from alias_mapper.formats import resolve_alias, GffTranslator, FastaTranslator


def fresh_stats():
    return {"mapped": 0, "unmapped": 0, "unmapped_examples": set()}


# --- exact path ----------------------------------------------------------

def test_exact_hit():
    assert resolve_alias("chr1", {"chr1": "1"}) == "1"


def test_exact_hit_takes_priority_over_fallback():
    # Both the exact name and a swapped form are in the map; the exact
    # name must win.
    m = {"NW_1.1": "exact", "NW_1v1": "swapped"}
    assert resolve_alias("NW_1.1", m) == "exact"


def test_genuine_miss_returns_none():
    assert resolve_alias("scaffold_internal_42", {"chr1": "1"}) is None


# --- version-separator swap (.N <-> vN) ----------------------------------

def test_ucsc_v_form_resolves_to_dot_form():
    assert resolve_alias("NW_013982187v1", {"NW_013982187.1": "scafX"}) == "scafX"


def test_dot_form_resolves_to_ucsc_v_form():
    assert resolve_alias("NW_013982187.1", {"NW_013982187v1": "scafX"}) == "scafX"


def test_swap_does_not_mismap_when_target_form_absent():
    # vN swaps to a dot form that is NOT in the map -> must miss, never
    # silently map to an unrelated entry.
    assert resolve_alias("NW_999v1", {"chr1": "1"}) is None


def test_name_without_version_is_unaffected():
    assert resolve_alias("chrMT", {"chrMT": "MT"}) == "MT"


# --- ENA prefix strip ----------------------------------------------------

def test_ena_strips_to_versioned_accession():
    m = {"CAJVCV010000001.1": "chrA"}
    assert resolve_alias("ENA|CAJVCV010000001|CAJVCV010000001.1", m) == "chrA"


def test_ena_falls_back_to_unversioned_accession():
    m = {"CAJVCV010000001": "chrA"}
    assert resolve_alias("ENA|CAJVCV010000001|CAJVCV010000001.1", m) == "chrA"


def test_ena_prefix_is_case_insensitive():
    assert resolve_alias("ena|X|X.1", {"X.1": "y"}) == "y"


def test_ena_wrapped_v_form_needs_strip_and_swap():
    # ENA wrapper around a vN accession, map stores the dot form.
    m = {"KI270742.1": "chrUn"}
    assert resolve_alias("ENA|KI270742|KI270742v1", m) == "chrUn"


def test_non_ena_pipe_name_is_not_unwrapped():
    # A pipe that is not an ENA wrapper should not be treated as one.
    assert resolve_alias("foo|bar", {"bar": "z"}) is None


# --- integration through the translators ---------------------------------

def test_gff_fallback_maps_column_one():
    g = GffTranslator()
    st = fresh_stats()
    line = "NW_013982187v1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1\n"
    out = g.translate_line(line, {"NW_013982187.1": "chrUn_x"}, st)
    assert out.split("\t")[0] == "chrUn_x"
    assert st["mapped"] == 1 and st["unmapped"] == 0


def test_fasta_fallback_strips_ena_and_preserves_description():
    fa = FastaTranslator()
    st = fresh_stats()
    line = ">ENA|KI270742|KI270742.1 unplaced scaffold\n"
    out = fa.translate_line(line, {"KI270742.1": "chrUn_KI270742v1"}, st)
    assert out == ">chrUn_KI270742v1 unplaced scaffold\n"
    assert st["mapped"] == 1

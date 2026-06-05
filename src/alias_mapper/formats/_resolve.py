"""
Fallback name resolution for alias lookups.

The primary lookup is an exact dict hit: ``alias_map[name]``. When that
misses, real-world inputs often carry the *same* identifier in a
different surface form. Rather than fail those outright, we try a small,
conservative set of normalizations and retry the lookup. The fallbacks
only run on a miss, so the common path stays a single dict lookup.

Two variant classes are handled, both low-risk normalizations of the
same underlying accession (not fuzzy matching):

  Version separator (.N <-> vN)
    UCSC writes unplaced/unlocalized scaffolds with a 'v' version
    separator (NW_013982187v1) where GenBank/RefSeq use a dot
    (NW_013982187.1). If a name in one form misses, try the other.

  ENA pipe-prefixed headers (ENA|<unversioned>|<versioned>)
    ENA FASTA headers wrap the accession as 'ENA|ACC|ACC.v'. The bare
    accession is what matches our columns, so on a miss we peel the ENA
    wrapper and retry on the inner accession(s), most-likely first.

Resolution order: exact name, then ENA-unwrapped accession(s), with a
version-separator swap tried on each. First hit wins. None means the
name is genuinely unmapped and the caller passes the line through
unchanged, exactly as before this fallback existed.
"""

import re

# A trailing version suffix in either surface form: ".1" or "v1".
# Non-greedy with an end anchor so the LAST separator is the split point
# (e.g. "GL000.2.1" -> base "GL000.2", version "1").
_DOT_VERSION = re.compile(r"^(.*?)\.(\d+)$")
_V_VERSION = re.compile(r"^(.*?)v(\d+)$")


def _swap_version_suffix(name):
    """
    Yield the alternate version-suffix form of ``name``, if one applies.

    "NW_013982187.1" -> "NW_013982187v1" and vice versa. Yields nothing
    when the name has no trailing ".N" or "vN", so callers can iterate
    unconditionally.
    """
    m = _DOT_VERSION.match(name)
    if m:
        yield f"{m.group(1)}v{m.group(2)}"
        return
    m = _V_VERSION.match(name)
    if m:
        yield f"{m.group(1)}.{m.group(2)}"


def _strip_ena_prefix(name):
    """
    Yield the bare accession(s) from an 'ENA|...|...' header.

    ENA|CAJVCV010000001|CAJVCV010000001.1 yields "CAJVCV010000001.1"
    (versioned, the form that matches our columns) then
    "CAJVCV010000001" (unversioned) as a secondary try. Yields nothing
    when the name is not ENA-prefixed.
    """
    if "|" not in name:
        return
    fields = name.split("|")
    if not fields or fields[0].upper() != "ENA":
        return
    rest = [f for f in fields[1:] if f]
    # The versioned accession is usually the last field and most likely
    # to match; the unversioned field is a secondary candidate.
    for f in reversed(rest):
        yield f


def _candidates(name):
    """
    Generate lookup candidates for ``name`` in priority order.

    The exact name is always yielded first, so a normal hit costs a
    single dict lookup. Candidates are deduplicated while preserving
    order, so each surface form is tried at most once.
    """
    seen = set()

    def add(candidate):
        if candidate and candidate not in seen:
            seen.add(candidate)
            return True
        return False

    # ENA-unwrapped accessions become additional base forms that the
    # version-swap step can also act on (handles ENA-wrapped vN names).
    bases = [name]
    bases.extend(_strip_ena_prefix(name))

    for base in bases:
        if add(base):
            yield base
        for swapped in _swap_version_suffix(base):
            if add(swapped):
                yield swapped


def resolve_alias(name: str, alias_map: dict) -> str | None:
    """
    Look up ``name`` in ``alias_map``, trying conservative fallbacks on a miss.

    Returns the mapped target name, or None if no candidate form of the
    name is present in the map. The first candidate is always the exact
    name, so an ordinary hit is a single dict lookup with no overhead.
    """
    for candidate in _candidates(name):
        hit = alias_map.get(candidate)
        if hit is not None:
            return hit
    return None

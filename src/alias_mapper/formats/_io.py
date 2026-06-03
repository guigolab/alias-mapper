"""
Transparent gzip handling for input and output files.

Genomics files (FASTA, GFF, GTF) are very often distributed gzipped,
sometimes without a telltale .gz suffix (e.g. a browser "download"
endpoint). These helpers let the rest of the package open a path
without caring whether it's compressed:

  - reads sniff the gzip magic bytes, so a gzipped file works even if
    its name doesn't end in .gz;
  - writes compress when the chosen output path ends in .gz;
  - format detection ignores a trailing .gz so `genome.fa.gz` still
    resolves to the FASTA translator.

This is the only place in the package that decides "is this gzip?",
so the rule lives in one spot.
"""

import gzip
import io
from pathlib import Path

# gzip streams begin with these two magic bytes (RFC 1952).
_GZIP_MAGIC = b"\x1f\x8b"


def is_gzip(path) -> bool:
    """
    Return True if `path` is gzip-compressed, decided by content not name.

    Reads the first two bytes and checks for the gzip magic number, so a
    gzipped file is handled regardless of whether it ends in .gz. Returns
    False for a missing or unreadable file; the caller surfaces a clearer
    "not found" error downstream.
    """
    try:
        with open(path, "rb") as f:
            return f.read(2) == _GZIP_MAGIC
    except OSError:
        return False


def open_text_read(path, encoding: str = "utf-8") -> io.TextIOBase:
    """Open `path` for text reading, decompressing if it is gzipped."""
    if is_gzip(path):
        return gzip.open(path, "rt", encoding=encoding)
    return open(path, "r", encoding=encoding)


def open_text_write(path, encoding: str = "utf-8") -> io.TextIOBase:
    """
    Open `path` for text writing, compressing if the name ends in .gz.

    Output compression keys off the extension rather than content (there
    is no content yet): `out.gff.gz` is written gzipped, `out.gff` plain.
    """
    if Path(path).suffix.lower() == ".gz":
        return gzip.open(path, "wt", encoding=encoding)
    return open(path, "w", encoding=encoding)


def effective_suffix(path) -> str:
    """
    The format-relevant suffix, ignoring a trailing .gz.

    `genome.fa.gz` -> `.fa`, `genome.gff` -> `.gff`, `genome.gz` -> ``.
    Lower-cased. Used so extension-based format detection works on
    compressed files.
    """
    p = Path(path)
    if p.suffix.lower() == ".gz":
        return Path(p.stem).suffix.lower()
    return p.suffix.lower()

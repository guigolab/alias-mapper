"""
File format translators.

Each translator class handles one file format's specifics — which lines
contain sequence names, how to extract and rewrite them. The CLI
dispatches to the right translator by file extension via TRANSLATORS.

Input and output may be gzipped transparently; see _io for the rules.
Format detection ignores a trailing .gz, so `genome.fa.gz` resolves to
the FASTA translator.

Adding a new format: write a class in a new module, import it here,
add an entry to TRANSLATORS.
"""

from .base import FileTranslator
from .gff import GffTranslator
from .fasta import FastaTranslator
from ._io import open_text_read, open_text_write, is_gzip, effective_suffix

# Extension → translator class. The CLI uses this to pick a translator
# based on the input file's suffix (a trailing .gz is stripped first).
TRANSLATORS: dict[str, type[FileTranslator]] = {
    ".gff":   GffTranslator,
    ".gff3":  GffTranslator,
    ".gtf":   GffTranslator,
    ".fa":    FastaTranslator,
    ".fasta": FastaTranslator,
    ".fna":   FastaTranslator,
}


def translator_for(path) -> FileTranslator:
    """
    Pick a translator based on the file's extension.

    A trailing .gz is ignored, so `genome.gff.gz` uses the GFF
    translator. Raises ValueError if no translator is registered for
    the (de-gzipped) extension.
    """
    ext = effective_suffix(path)
    cls = TRANSLATORS.get(ext)
    if cls is None:
        supported = sorted(TRANSLATORS.keys())
        raise ValueError(
            f"no translator registered for {path} (extension {ext!r}). "
            f"Supported: {supported} (optionally .gz-compressed)"
        )
    return cls()


__all__ = [
    "FileTranslator",
    "GffTranslator",
    "FastaTranslator",
    "TRANSLATORS",
    "translator_for",
    "open_text_read",
    "open_text_write",
    "is_gzip",
    "effective_suffix",
]

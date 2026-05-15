"""
File format translators.

Each translator class handles one file format's specifics — which lines
contain sequence names, how to extract and rewrite them. The CLI
dispatches to the right translator by file extension via TRANSLATORS.

Adding a new format: write a class in a new module, import it here,
add an entry to TRANSLATORS.
"""

from pathlib import Path

from .base import FileTranslator
from .gff import GffTranslator
from .fasta import FastaTranslator

# Extension → translator class. The CLI uses this to pick a translator
# based on the input file's suffix.
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

    Raises ValueError if no translator is registered for the extension.
    """
    ext = Path(path).suffix.lower()
    cls = TRANSLATORS.get(ext)
    if cls is None:
        supported = sorted(TRANSLATORS.keys())
        raise ValueError(
            f"no translator registered for extension {ext!r}. "
            f"Supported: {supported}"
        )
    return cls()


__all__ = [
    "FileTranslator",
    "GffTranslator",
    "FastaTranslator",
    "TRANSLATORS",
    "translator_for",
]

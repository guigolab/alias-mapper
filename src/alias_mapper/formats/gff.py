"""GFF / GTF translator. Both formats put the sequence name in column 1."""

from pathlib import Path

from .base import FileTranslator
from ._io import open_text_read
from ._resolve import resolve_alias


class GffTranslator(FileTranslator):
    """
    Translator for GFF, GFF3, and GTF files.

    All three are tab-separated with the sequence name in column 1.
    Lines starting with '#' are comments/headers and pass through
    unchanged.

    Name lookup goes through resolve_alias, so an exact map hit is used
    when present and a small set of conservative fallbacks (ENA prefix
    strip, .N/vN version-separator swap) is tried only when the exact
    name misses.

    Known limitation: '##sequence-region <name> ...' metadata lines
    contain a sequence name that v0.2 does not translate. The design
    doc flags this as a v1 follow-up.
    """

    def translate_line(self, line: str, alias_map: dict, stats: dict) -> str:
        if not line or line.startswith("#"):
            return line

        parts = line.rstrip("\n").split("\t")
        if len(parts) < 1:
            return line

        seq_name = parts[0]
        new_name = resolve_alias(seq_name, alias_map)
        if new_name is None:
            stats["unmapped"] += 1
            stats["unmapped_examples"].add(seq_name)
            return line

        parts[0] = new_name
        stats["mapped"] += 1
        return "\t".join(parts) + "\n"

    def sample_names(self, path: Path, limit: int = 50) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        with open_text_read(path) as f:
            for line in f:
                if not line or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if not parts:
                    continue
                name = parts[0]
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
                    if len(names) >= limit:
                        break
        return names

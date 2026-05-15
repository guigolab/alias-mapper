"""GFF / GTF translator. Both formats put the sequence name in column 1."""

from pathlib import Path

from .base import FileTranslator


class GffTranslator(FileTranslator):
    """
    Translator for GFF, GFF3, and GTF files.

    All three are tab-separated with the sequence name in column 1.
    Lines starting with '#' are comments/headers and pass through
    unchanged.

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
        new_name = alias_map.get(seq_name)
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
        with open(path, "r", encoding="utf-8") as f:
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

"""FASTA translator. Sequence name lives in the header line, after the '>'."""

from pathlib import Path

from .base import FileTranslator
from ._io import open_text_read
from ._resolve import resolve_alias


class FastaTranslator(FileTranslator):
    """
    Translator for FASTA files.

    FASTA structure:
      - Header lines start with '>'. Format: '>NAME [WHITESPACE DESCRIPTION]'.
        Only the NAME (first whitespace-separated token after '>') is the
        sequence identifier. The description, if present, is preserved
        verbatim including the exact whitespace between name and description.
      - Sequence lines (the ACGT content) pass through unchanged.
      - Blank lines pass through unchanged.

    Translation rule:
      - If the header's name resolves in the alias map, replace the name
        with the target. Description is preserved exactly.
      - If the name doesn't resolve, pass the whole line through
        unchanged and count it as unmapped (same warn-and-pass-through
        behavior as the GFF translator).

    Name lookup goes through resolve_alias: an exact map hit is used when
    present, and conservative fallbacks (ENA prefix strip, .N/vN
    version-separator swap) are tried only when the exact name misses.
    """

    def translate_line(self, line: str, alias_map: dict, stats: dict) -> str:
        if not line.startswith(">"):
            # Sequence line, blank line, or anything else non-header.
            return line

        # Strip the trailing newline so we can reattach it exactly at the
        # end. We don't use rstrip() generally — only the newline — to
        # preserve any trailing whitespace inside the description.
        if line.endswith("\n"):
            body = line[1:-1]
            newline = "\n"
        else:
            body = line[1:]
            newline = ""

        # Find the first whitespace character after '>'. Everything before
        # it is the name; everything from there on (including the
        # whitespace itself) is preserved as-is.
        i = 0
        while i < len(body) and not body[i].isspace():
            i += 1
        name = body[:i]
        rest = body[i:]

        if not name:
            # Malformed header like '>' or '> description'. Nothing to
            # translate; pass through unchanged.
            return line

        new_name = resolve_alias(name, alias_map)
        if new_name is None:
            stats["unmapped"] += 1
            stats["unmapped_examples"].add(name)
            return line

        stats["mapped"] += 1
        return f">{new_name}{rest}{newline}"

    def sample_names(self, path: Path, limit: int = 50) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        with open_text_read(path) as f:
            for line in f:
                if not line.startswith(">"):
                    continue
                # Parse header the same way translate_line does, so a name
                # collected here is the same string that would be looked up.
                body = line[1:].rstrip("\n")
                i = 0
                while i < len(body) and not body[i].isspace():
                    i += 1
                name = body[:i]
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
                    if len(names) >= limit:
                        break
        return names

"""FASTA translator. Sequence name lives in the header line, after the '>'."""

from .base import FileTranslator


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
      - If the header's name is in the alias map, replace the name with
        the target. Description is preserved exactly.
      - If the name isn't in the alias map, pass the whole line through
        unchanged and count it as unmapped (same warn-and-pass-through
        behavior as the GFF translator).
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

        new_name = alias_map.get(name)
        if new_name is None:
            stats["unmapped"] += 1
            stats["unmapped_examples"].add(name)
            return line

        stats["mapped"] += 1
        return f">{new_name}{rest}{newline}"

"""Abstract base class for file format translators."""

from abc import ABC, abstractmethod
from pathlib import Path


class FileTranslator(ABC):
    """
    Translates sequence names in one file format.

    Subclasses know which lines in their format contain sequence names
    and how to rewrite them. Comments, headers, and blank lines should
    pass through unchanged.
    """

    @abstractmethod
    def translate_line(self, line: str, alias_map: dict, stats: dict) -> str:
        """
        Translate one line of input.

        Args:
            line:      One line from the input file (with trailing newline).
            alias_map: {source_name -> target_name} dict from AliasSource.
            stats:     Mutable dict with keys 'mapped' (int), 'unmapped'
                       (int), and 'unmapped_examples' (set). The translator
                       updates these in place.

        Returns:
            The translated line (with trailing newline). Lines that
            don't contain a translatable sequence name are returned
            unchanged.
        """

    @abstractmethod
    def sample_names(self, path: Path, limit: int = 50) -> list[str]:
        """
        Read up to `limit` unique sequence names from the start of the file.

        Used by auto-detection to decide which convention and assembly
        the input file is using. Stops once `limit` unique names have
        been collected, so this is O(limit) regardless of file size.

        Args:
            path:  Path to the input file.
            limit: Maximum number of unique names to return.

        Returns:
            List of unique sequence names, preserving the order they
            appeared in the file. May contain fewer than `limit` if
            the file has fewer unique names.
        """

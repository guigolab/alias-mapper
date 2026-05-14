"""Abstract base class for file format translators."""

from abc import ABC, abstractmethod


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

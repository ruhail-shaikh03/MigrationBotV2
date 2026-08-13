"""Collapsing the spellings of a name into one person.

After shared cells are split, the reference tracker still shows one person under several
spellings — "Madiha" and "Madiha Shah Bukhari", "Babar" and "Babar Ali". `normalise_person`
folds case and whitespace and stops there, on purpose: merging two colleagues is silent
data corruption, and far worse than showing one twice. This module is where an admin's
explicit decisions get applied — never a guess.
"""

from typing import Any, Dict, Iterable, List, Optional

from app.core.people import display_person, normalise_person, split_cell


class PersonResolver:
    """Applies a project's alias map to a raw people-cell.

    Construct once per request and reuse; the lookup is a dict, and a 400-row tab with
    two people-columns performs ~800 of them.
    """

    def __init__(self, alias_map: Optional[Dict[str, List[str]]] = None) -> None:
        # Keys are already normalised; values are display-form canonical names.
        self._aliases = alias_map or {}

    @classmethod
    def from_rows(cls, rows: Iterable[Any]) -> "PersonResolver":
        """Build from PersonAlias rows for a single project."""
        alias_map: Dict[str, List[str]] = {}
        for row in rows:
            key = normalise_person(row.alias)
            canonical = display_person(row.canonical)
            if not key or not canonical:
                continue
            bucket = alias_map.setdefault(key, [])
            if canonical not in bucket:
                bucket.append(canonical)
        return cls(alias_map)

    def _lookup(self, name: str) -> List[str]:
        """One hop, never chained: a canonical name is never itself looked up.

        That makes an alias cycle impossible by construction rather than by validation,
        and leaves no chain for a stray edit to corrupt.
        """
        return self._aliases.get(normalise_person(name)) or [name]

    def resolve_cell(self, raw: Any) -> List[str]:
        """Every person a cell names, deduplicated, in order of first appearance.

        The whole-cell lookup comes first and beats the splitter outright. That is what
        makes automatic splitting safe: a team named "R&D", or a genuine name containing
        a slash, is corrected by adding one alias row. Every automatic decision has a
        manual answer.
        """
        text = display_person(raw)
        if not text:
            return []

        whole = self._aliases.get(normalise_person(text))
        candidates = list(whole) if whole else [
            name for part in split_cell(text) for name in self._lookup(part)
        ]

        out: List[str] = []
        seen = set()
        for name in candidates:
            key = normalise_person(name)
            if key and key not in seen:
                seen.add(key)
                out.append(name)
        return out

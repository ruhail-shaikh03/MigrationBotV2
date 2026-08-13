"""Collapsing the spellings of a name into one person.

After shared cells are split, the reference tracker still shows one person under several
spellings — "Madiha" and "Madiha Shah Bukhari", "Babar" and "Babar Ali". `normalise_person`
folds case and whitespace and stops there, on purpose: merging two colleagues is silent
data corruption, and far worse than showing one twice. This module is where an admin's
explicit decisions get applied — never a guess.
"""

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class MergeSuggestion:
    """A variant spelling and the fuller names it might belong to.

    `candidates` is a list, never a single value, because ambiguity is the normal case:
    on the reference tracker "Abdullah" matches three different people. Showing one and
    hiding the others would be the app making exactly the judgement it must not make.
    """
    name: str
    candidates: List[str] = field(default_factory=list)
    reason: str = ""


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance, iterative two-row form."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
        previous = current
    return previous[-1]


def _is_typo_of(a_tokens: List[str], b_tokens: List[str]) -> bool:
    """Same token count, exactly one token differing by at most two edits.

    This is what catches "Abdullah Azfar" / "Abdullah Azfer", which token-subset matching
    misses completely — the tokens are not nested, they are misspelled.
    """
    if len(a_tokens) != len(b_tokens):
        return False
    differing = [(x, y) for x, y in zip(a_tokens, b_tokens) if x != y]
    if len(differing) != 1:
        return False
    x, y = differing[0]
    return _edit_distance(x, y) <= 2 and min(len(x), len(y)) >= 3


def suggest_merges(counts: Dict[str, int]) -> List[MergeSuggestion]:
    """Variant spellings that may be the same person, ranked by how often each occurs.

    Three cheap signals, never applied on their own. Case-only variants are excluded
    because `normalise_person` already folds them, so suggesting one would be noise.

    Direction is decided per-signal, not globally. Subset and prefix are *inherently*
    directional — "Abdullah" is contained in "Abdullah Azfer" and never the reverse — so
    they need no tie-break and every containing name is listed however rare it is. Only
    the typo signal is symmetric, and there the commoner spelling wins, since the sheet's
    dominant spelling is the one worth keeping.

    Applying the frequency rule to all three signals (an earlier draft) dropped
    "Abdullah Azfer" from "Abdullah"'s candidates purely because it occurs twice rather
    than three times — hiding a person the admin might well have meant, which is exactly
    the narrowing this whole module refuses to do.
    """
    normalised = {name: normalise_person(name) for name in counts}
    tokens = {name: key.split(" ") for name, key in normalised.items()}

    out: List[MergeSuggestion] = []
    for name, key in normalised.items():
        candidates: List[str] = []
        reason = ""
        for other, other_key in normalised.items():
            if other == name or key == other_key:
                continue
            if set(tokens[name]) < set(tokens[other]):
                candidates.append(other)
                reason = reason or "subset"
            elif other_key.startswith(key + " "):
                candidates.append(other)
                reason = reason or "prefix"
            elif _is_typo_of(tokens[name], tokens[other]):
                # Symmetric: without a tie-break this emits both directions, which is
                # noise at best and a two-row cycle if an admin accepted both.
                if counts[other] < counts[name] or (
                    counts[other] == counts[name] and other < name
                ):
                    continue
                candidates.append(other)
                reason = reason or "typo"
        if candidates:
            out.append(MergeSuggestion(name=name, candidates=candidates, reason=reason))

    out.sort(key=lambda s: (-counts[s.name], s.name))
    return out

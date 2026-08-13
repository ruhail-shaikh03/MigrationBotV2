# People Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the workload dashboard tell the truth about who is assigned to what, by detecting people columns from their values rather than their header wording, crediting every person named in a shared cell, and letting an admin declare that two spellings are one person.

**Architecture:** A pure statistical profiler (`core/column_profile.py`) classifies a column from its values alone — no column names, no vocabulary. Detection consumes it alongside the existing LLM call; a column becomes a role if *either* signal positively says people. A structural splitter breaks `A & B` into two assignments. A `person_aliases` table plus a one-hop resolver collapses spelling variants, applied in `collect_assignments` so the dashboard and the agent cannot diverge. Nothing writes to the spreadsheet.

**Tech Stack:** FastAPI (async), SQLAlchemy 2.0 + asyncpg, Pydantic v2, pytest; Next.js 16 App Router, React 19, Tailwind v4.

**Spec:** `docs/superpowers/specs/2026-08-13-people-accuracy-design.md`

## Global Constraints

- **Branch from `main` only after `fix/scroll-tabs-overdue` is merged.** That branch changes `collect_assignments`' neighbours in `dashboard.py` and `read.py`; building on top of an unmerged version guarantees conflicts.
- **One branch for this plan: `feat/people-accuracy`.** Update `TDD.md` **before** each commit, not after — it is the authoritative as-built doc.
- **Async everywhere.** `asyncpg` + `AsyncSessionLocal` + `AsyncOpenAI` + `redis.asyncio`. Never call a sync client on the event loop.
- **Never `.strip()` a header used as a lookup key.** Trailing whitespace is real data (`"Technical Resource "`). Resolve through `core/column_mapper.py:resolve_column()`.
- **No vocabulary in code.** `column_profile.py` must never see a column name. The splitter knows delimiters only — no honorifics, no titles, no language-specific words. All such mappings are alias rows.
- **Nothing in this plan writes to a spreadsheet.** No `enqueue_write_job`, no Sheets write API.
- **RBAC:** every alias endpoint carries `dependencies=[Depends(require_admin)]`, matching the existing `/permissions` handlers in `api/admin.py`.
- **No migration tooling exists.** `main.py:lifespan` → `db/engine.py:init_db` calls `Base.metadata.create_all`, which creates new *tables* but never alters existing ones. Add new tables only; do not add a column to an existing model.
- **Tests run from `backend/`.** Locally there is no Docker, so run only `pytest tests/test_core tests/test_sheets` with CI env vars inline:
  ```bash
  cd backend && DATABASE_URL="postgresql+asyncpg://u:p@localhost/db" REDIS_URL="redis://localhost:6379" \
    DEEPSEEK_API_KEY=x JWT_SECRET=x GOOGLE_CLIENT_ID=x GOOGLE_CLIENT_SECRET=x DB_PASSWORD=x REDIS_PASSWORD=x \
    CORS_ORIGINS=http://localhost:3000 ADMIN_EMAILS=a@b.com DEFAULT_SPREADSHEET_ID=x \
    python -m pytest tests/test_core tests/test_sheets -q
  ```
- **Frontend lint baseline is 51 problems / 27 errors, all pre-existing.** The bar is "no new ones", not zero.
- **Test fixtures are named by shape, not by origin.** `test_short_uppercase_codes_are_not_people`, never `test_university_is_not_people`. No customer vocabulary in the suite.

---

## File Structure

**Create**
| Path | Responsibility |
|---|---|
| `backend/app/core/column_profile.py` | Statistics over a column's values, and a people/not/abstain verdict. Pure, no I/O, no column names. |
| `backend/app/core/aliases.py` | `PersonResolver` (one-hop lookup + splitting pipeline) and `suggest_merges`. No DB access — takes plain dicts. |
| `backend/app/models/person_alias.py` | The `person_aliases` table. |
| `backend/app/api/aliases.py` | Admin-only CRUD + suggestions endpoints. Separate from `admin.py`, which is already ~400 lines. |
| `frontend/src/app/admin/people/page.tsx` | Alias triage screen. |
| `backend/tests/test_core/test_column_profile.py` | Profiler tests, parametrised by shape. |
| `backend/tests/test_core/test_aliases.py` | Splitting, resolution, suggestions. |

**Modify**
| Path | Change |
|---|---|
| `backend/app/core/people.py` | Add `split_cell`; `collect_assignments` gains a `resolver` parameter. |
| `backend/app/core/schema_detect.py` | Profiles + sample values into the prompt; `_structural_fallback` uses `people_confidence`. |
| `backend/app/api/dashboard.py` | Build a `PersonResolver` per request; pass to `collect_assignments`. |
| `backend/app/sheets/read.py` | `summarize(count_by_field)` on a people column resolves through the same pipeline. |
| `backend/app/models/__init__.py` | Register `PersonAlias` so `create_all` sees it. |
| `backend/app/main.py` | Mount the aliases router. |
| `TDD.md` | §7 (schema), §10 (REST), §14 (frontend). |

---

## Task 1: Column profiler

**Files:**
- Create: `backend/app/core/column_profile.py`
- Test: `backend/tests/test_core/test_column_profile.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ColumnProfile` dataclass with fields `n: int`, `cardinality: float`, `mean_tokens: float`, `mean_length: float`, `alpha_ratio: float`, `title_case_ratio: float`, `repeat_ratio: float`; `profile_column(values: Iterable[Any]) -> ColumnProfile`; `people_confidence(profile: ColumnProfile) -> str` returning `"likely"`, `"abstain"` or `"unlikely"`; constant `MIN_SAMPLE = 20`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_core/test_column_profile.py`:

```python
"""Classifying a column as holding people, from its values alone.

The bug this guards: detection reasoned about header wording, so "Consultant" was
missed because the word reads like a job title in English. Header wording is the least
generic signal available — the next sheet says Owner, PIC, or a header in another
language. Value shape is the same on every sheet.

Fixtures are named by shape, never by the sheet they came from: the profiler must not
know that any customer has a "University" column, and neither must its tests.
"""

import pytest

from app.core.column_profile import MIN_SAMPLE, people_confidence, profile_column


def _rep(values, times):
    """Repeat a small value set up to a realistic row count."""
    return [values[i % len(values)] for i in range(times)]


PEOPLE = ["Muhammad Zeshan Ayub", "Madiha Shah Bukhari", "Huzaima Ather", "Taymoor Ahmed",
          "Neeha Nehal", "Amna Lateef Korejo", "Arjmand Bano", "Babar Ali"]
UPPER_CODES = ["AROR", "AWKUM", "BNWU", "CUVAS", "EUM", "GUDGK", "HSA"]
CATEGORY_LABELS = ["Fiori", "Report", "Form", "Interface"]
ENUM_LABELS = ["Completed", "Not Started", "In Testing", "Hold"]
FREE_TEXT = ["URL Update (Transport, Hostel, Complaint and etc)",
             "Student fee challan generation for semester 2",
             "Bulk upload of transcript records from legacy",
             "Hostel allocation report with room mapping"]


def test_multi_token_titlecase_names_are_people():
    assert people_confidence(profile_column(_rep(PEOPLE, 400))) == "likely"


def test_short_uppercase_codes_are_not_people():
    """Mid-cardinality and highly repeated, like names — but 1 token, ~5 chars, not title-case."""
    assert people_confidence(profile_column(_rep(UPPER_CODES, 400))) == "unlikely"


def test_single_token_category_labels_are_not_people():
    assert people_confidence(profile_column(_rep(CATEGORY_LABELS, 160))) == "unlikely"


def test_free_text_is_not_people():
    assert people_confidence(profile_column(FREE_TEXT * 100)) == "unlikely"


def test_unique_identifiers_are_not_people():
    assert people_confidence(profile_column([f"SLCM-{i:04d}" for i in range(400)])) == "unlikely"


def test_low_cardinality_enum_abstains_rather_than_being_rejected():
    """Documents the known limit: a team of eight and a workflow of eight states are
    statistically identical. Only meaning separates them, which is why the LLM stays in
    the loop. Abstaining is not a vote — it neither applies a role nor blocks one."""
    assert people_confidence(profile_column(_rep(ENUM_LABELS, 400))) == "abstain"


def test_small_samples_abstain():
    assert people_confidence(profile_column(PEOPLE[:5])) == "abstain"
    assert people_confidence(profile_column(PEOPLE[:MIN_SAMPLE - 1])) == "abstain"


def test_blank_and_whitespace_values_are_ignored():
    profile = profile_column(["Babar Ali", "", "   ", None, "Neeha Nehal"])
    assert profile.n == 2


def test_empty_column_abstains():
    assert people_confidence(profile_column([])) == "abstain"


@pytest.mark.parametrize("field", [
    "cardinality", "mean_tokens", "mean_length", "alpha_ratio", "title_case_ratio", "repeat_ratio",
])
def test_profile_fields_are_computed_independently(field):
    """Signals stay separable so a non-Latin sheet can lean on the script-agnostic ones."""
    assert isinstance(getattr(profile_column(_rep(PEOPLE, 100)), field), float)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && DATABASE_URL="postgresql+asyncpg://u:p@localhost/db" REDIS_URL="redis://localhost:6379" \
  DEEPSEEK_API_KEY=x JWT_SECRET=x GOOGLE_CLIENT_ID=x GOOGLE_CLIENT_SECRET=x DB_PASSWORD=x REDIS_PASSWORD=x \
  CORS_ORIGINS=http://localhost:3000 ADMIN_EMAILS=a@b.com DEFAULT_SPREADSHEET_ID=x \
  python -m pytest tests/test_core/test_column_profile.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'app.core.column_profile'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/core/column_profile.py`:

```python
"""Deciding whether a column holds people, from its values and nothing else.

Schema detection used to reason about header wording. That is the least generic signal
a tracking sheet offers: "Consultant" reads like a job title in English and was skipped,
while the next sheet will say Owner, Assigned To, Resource, PIC, or a header in another
language entirely. Value *shape* is the same on every sheet in every domain.

Nothing here ever sees a column name. It takes a list of strings and returns statistics,
which is exactly what lets it work on a sheet this code has never been shown.

Thresholds were fitted against every populated column of two real trackers. An earlier
draft used cardinality and token-count alone and misclassified a column of short
uppercase site codes as people; mean_length and title_case_ratio are what separate a
person's name from a category code.
"""

import re
from dataclasses import dataclass
from typing import Any, Iterable, List

# Below this many non-blank values the statistics are noise, not evidence. One real tab
# has a people column with five entries; classifying from that is guessing.
MIN_SAMPLE = 20

# Letters, spaces and the punctuation that legitimately appears inside names. Digits,
# URLs, dates and code identifiers all fail it.
_ALPHA_ONLY = re.compile(r"^[^\W\d_][\w\s.'\-/&]*$", re.UNICODE)
_TITLE_START = re.compile(r"^[^\W\d_]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ColumnProfile:
    """Shape statistics for one column's non-blank values.

    Fields are deliberately independent rather than pre-combined into a single score.
    title_case_ratio and mean_tokens carry Latin-script assumptions; keeping them
    separate means a CJK or Arabic sheet can be handled by re-weighting rather than by
    rewriting, when one turns up.
    """
    n: int
    cardinality: float
    mean_tokens: float
    mean_length: float
    alpha_ratio: float
    title_case_ratio: float
    repeat_ratio: float


def _clean(values: Iterable[Any]) -> List[str]:
    out = []
    for value in values or []:
        if value is None:
            continue
        text = _WHITESPACE.sub(" ", str(value).strip())
        if text:
            out.append(text)
    return out


def _is_title_case(text: str) -> bool:
    """First character is an upper-case letter and the value is not shouted.

    A name is "Babar Ali"; a site code is "AWKUM". Comparing against .upper() catches the
    all-caps case without assuming anything about length.
    """
    if not _TITLE_START.match(text):
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return letters[0].isupper() and text != text.upper()


def profile_column(values: Iterable[Any]) -> ColumnProfile:
    """Shape statistics over the non-blank values of one column."""
    cleaned = _clean(values)
    n = len(cleaned)
    if n == 0:
        return ColumnProfile(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    folded = [v.casefold() for v in cleaned]
    counts: dict = {}
    for value in folded:
        counts[value] = counts.get(value, 0) + 1

    return ColumnProfile(
        n=n,
        cardinality=len(counts) / n,
        mean_tokens=sum(len(v.split(" ")) for v in cleaned) / n,
        mean_length=sum(len(v) for v in cleaned) / n,
        alpha_ratio=sum(1 for v in cleaned if _ALPHA_ONLY.match(v)) / n,
        title_case_ratio=sum(1 for v in cleaned if _is_title_case(v)) / n,
        repeat_ratio=sum(1 for v in folded if counts[v] > 1) / n,
    )


def _criteria(profile: ColumnProfile) -> List[bool]:
    """Each criterion, evaluated separately so near-misses can be told from rejections."""
    return [
        0.02 <= profile.cardinality <= 0.6,   # not a status enum; not a unique-per-row id
        1.5 <= profile.mean_tokens <= 4.0,    # names are multi-token; category codes are not
        8.0 <= profile.mean_length <= 30.0,   # longer than a code, shorter than free text
        profile.alpha_ratio >= 0.8,           # no digits, URLs or dates
        profile.title_case_ratio >= 0.6,      # consistently capitalised, not shouted
        profile.repeat_ratio >= 0.3,          # people recur across rows
    ]


def people_confidence(profile: ColumnProfile) -> str:
    """"likely", "abstain" or "unlikely" — never a bare boolean.

    "abstain" is not a vote. Callers must treat it as neither applying a role nor
    blocking one; only "likely" is a positive claim. It covers two cases: too little
    data to judge, and a column that misses by exactly one criterion — which is where a
    small team and a short status enum become indistinguishable, and where the LLM's
    reading of meaning has to decide instead.
    """
    if profile.n < MIN_SAMPLE:
        return "abstain"
    failed = _criteria(profile).count(False)
    if failed == 0:
        return "likely"
    if failed == 1:
        return "abstain"
    return "unlikely"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the same pytest command as Step 2. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/column_profile.py backend/tests/test_core/test_column_profile.py
git commit -m "feat(schema): classify people columns from value shape, not header wording"
```

---

## Task 2: Structural cell splitting

**Files:**
- Modify: `backend/app/core/people.py`
- Test: `backend/tests/test_core/test_aliases.py` (create)

**Interfaces:**
- Consumes: `people.py:display_person`.
- Produces: `split_cell(raw: Any) -> List[str]` — display-form fragments, order preserved, blanks dropped.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_core/test_aliases.py`:

```python
"""Turning one people-cell into the people it names.

60 of 257 assigned cells on the reference tracker name two people
("Minhaj Alam & Dawood", "Ahmed Qamar/Asif"). Each became its own entry in the workload
panel, so a quarter of assignments were credited to a composite that does not exist
while the two real people got nothing.
"""

import pytest

from app.core.people import split_cell


@pytest.mark.parametrize("raw,expected", [
    ("Umair Ziad/Saba Haleem", ["Umair Ziad", "Saba Haleem"]),
    ("Minhaj Alam & Dawood", ["Minhaj Alam", "Dawood"]),
    ("Syed Ali Haider/ Muhammad Ashar Mateen", ["Syed Ali Haider", "Muhammad Ashar Mateen"]),
    ("Ahmed Qamar/Asif", ["Ahmed Qamar", "Asif"]),
])
def test_slash_and_ampersand_split_into_separate_people(raw, expected):
    assert split_cell(raw) == expected


def test_a_comma_is_never_a_delimiter():
    """"Shaikh, Rohail" is plausibly one person written surname-first. Inventing a
    colleague is a worse failure than under-splitting a shared assignment."""
    assert split_cell("Shaikh, Rohail") == ["Shaikh, Rohail"]


def test_a_plain_name_is_returned_unchanged():
    assert split_cell("Madiha Shah Bukhari") == ["Madiha Shah Bukhari"]


def test_honorifics_are_left_alone():
    """Stripping "Mr." would be English vocabulary hardcoded into the engine. The
    splitter knows delimiters and nothing else; honorifics are an alias-map concern."""
    assert split_cell("Mr. Minhaj Alam/ Dawood") == ["Mr. Minhaj Alam", "Dawood"]


def test_blank_fragments_are_dropped():
    assert split_cell("Babar Ali //& ") == ["Babar Ali"]


def test_blank_and_none_yield_nothing():
    assert split_cell("") == []
    assert split_cell(None) == []


def test_internal_whitespace_is_collapsed():
    assert split_cell("Amna   Lateef  Korejo") == ["Amna Lateef Korejo"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && DATABASE_URL="postgresql+asyncpg://u:p@localhost/db" REDIS_URL="redis://localhost:6379" \
  DEEPSEEK_API_KEY=x JWT_SECRET=x GOOGLE_CLIENT_ID=x GOOGLE_CLIENT_SECRET=x DB_PASSWORD=x REDIS_PASSWORD=x \
  CORS_ORIGINS=http://localhost:3000 ADMIN_EMAILS=a@b.com DEFAULT_SPREADSHEET_ID=x \
  python -m pytest tests/test_core/test_aliases.py -q
```

Expected: `ImportError: cannot import name 'split_cell' from 'app.core.people'`.

- [ ] **Step 3: Write the implementation**

In `backend/app/core/people.py`, add next to the other module-level regexes:

```python
# Delimiters that unambiguously separate two people. A comma is deliberately absent:
# "Shaikh, Rohail" is plausibly one person written surname-first, and inventing a
# colleague is a worse failure than under-splitting a shared assignment. A slash or an
# ampersand has no such reading.
_CELL_DELIMITERS = re.compile(r"[/&]+")
```

and add the function immediately after `display_person`:

```python
def split_cell(raw: Any) -> List[str]:
    """Every person named by one people-cell, in the order the sheet names them.

    Structural only: this knows delimiters and nothing else. It deliberately does not
    strip honorifics, titles or any other word — that would be one language's vocabulary
    compiled into the engine, on an app whose first constraint is working with any sheet.
    "Mr. Minhaj Alam" -> "Minhaj Alam" is expressible as an alias row instead, which is
    data an admin controls rather than code nobody can see.

    On the reference tracker 60 of 257 assigned cells name two people; before this each
    became its own workload entry, crediting a composite who does not exist.
    """
    text = display_person(raw)
    if not text:
        return []
    return [part for part in (display_person(p) for p in _CELL_DELIMITERS.split(text)) if part]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the same pytest command as Step 2. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/people.py backend/tests/test_core/test_aliases.py
git commit -m "feat(people): credit every person named in a shared cell"
```

---

## Task 3: Alias table and resolver

**Files:**
- Create: `backend/app/models/person_alias.py`, `backend/app/core/aliases.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/test_core/test_aliases.py`

**Interfaces:**
- Consumes: `people.py:split_cell`, `people.py:normalise_person`, `people.py:display_person`.
- Produces: `PersonAlias` model (table `person_aliases`); `PersonResolver(alias_map: Dict[str, List[str]] | None)` with method `resolve_cell(raw: Any) -> List[str]` and classmethod `from_rows(rows: Iterable[PersonAlias]) -> PersonResolver`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_core/test_aliases.py`:

```python
from app.core.aliases import PersonResolver


def _resolver(**pairs):
    """alias -> one-or-more canonical names, keyed as normalise_person would key it."""
    return PersonResolver({k: (v if isinstance(v, list) else [v]) for k, v in pairs.items()})


def test_an_unknown_name_passes_through_unchanged():
    assert _resolver().resolve_cell("Babar Ali") == ["Babar Ali"]


def test_an_alias_resolves_to_its_canonical_name():
    r = _resolver(**{"madiha": "Madiha Shah Bukhari"})
    assert r.resolve_cell("Madiha") == ["Madiha Shah Bukhari"]


def test_alias_lookup_is_case_and_whitespace_insensitive():
    r = _resolver(**{"babar": "Babar Ali"})
    assert r.resolve_cell("  BABAR  ") == ["Babar Ali"]


def test_each_fragment_of_a_shared_cell_is_resolved():
    r = _resolver(**{"dawood": "Muhammad Dawood Umer"})
    assert r.resolve_cell("Minhaj Alam & Dawood") == ["Minhaj Alam", "Muhammad Dawood Umer"]


def test_a_whole_cell_alias_beats_the_splitter():
    """The escape hatch that makes automatic splitting safe: any cell the delimiter rule
    gets wrong — a team called "R&D", a name containing a slash — is fixed with one row."""
    r = _resolver(**{"r&d": "R&D Team"})
    assert r.resolve_cell("R&D") == ["R&D Team"]


def test_one_alias_may_name_several_people():
    """How a comma-separated shared cell is expressed without teaching the splitter
    about commas."""
    r = _resolver(**{"shaikh, rohail": ["Shaikh", "Rohail"]})
    assert r.resolve_cell("Shaikh, Rohail") == ["Shaikh", "Rohail"]


def test_resolution_is_exactly_one_hop():
    """A canonical name is never looked up again, so a cycle cannot form."""
    r = _resolver(**{"a": "B", "b": "A"})
    assert r.resolve_cell("A") == ["B"]


def test_a_person_named_twice_in_one_cell_is_credited_once():
    r = _resolver(**{"umair": "Umair Ziad"})
    assert r.resolve_cell("Umair Ziad / Umair") == ["Umair Ziad"]


def test_order_of_first_appearance_is_preserved():
    assert _resolver().resolve_cell("Zara/Ali/Zara") == ["Zara", "Ali"]


def test_an_empty_resolver_still_splits():
    assert PersonResolver().resolve_cell("Ahmed Qamar/Asif") == ["Ahmed Qamar", "Asif"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run the pytest command from Task 2 Step 2. Expected: `ModuleNotFoundError: No module named 'app.core.aliases'`.

- [ ] **Step 3: Write the model**

Create `backend/app/models/person_alias.py`:

```python
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.engine import Base


class PersonAlias(Base):
    """One spelling of a person's name, and who it actually is.

    Per-project, because two organisations share no staff. Deliberately dumb: one row per
    (alias, canonical) pair, so a single alias may name several people — which is how a
    comma-separated shared cell is expressed without teaching the splitter about commas.

    Aliases are only ever applied, never inferred. Merging two names hides one inside the
    other and the error is silent — nobody notices a colleague who stopped existing —
    whereas a wrongly split or wrongly detected column is visible immediately. On the
    reference tracker "Abdullah" plausibly matches three different people, and nothing in
    the sheet can say which; any rule confident enough to resolve that is confident
    enough to be wrong.
    """

    __tablename__ = "person_aliases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Stored already normalised (people.py:normalise_person) so lookup is a plain dict hit.
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    # Display form, spelled as the reader should see it.
    canonical: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "alias", "canonical", name="uq_project_alias_canonical"),
    )

    def __repr__(self) -> str:
        return f"<PersonAlias project={self.project_id} {self.alias!r} -> {self.canonical!r}>"
```

Update `backend/app/models/__init__.py` so `create_all` sees the table:

```python
from app.db.engine import Base
from app.models.user import User
from app.models.project import Project
from app.models.permission import Permission
from app.models.audit_log import AuditLog
from app.models.session import Session
from app.models.person_alias import PersonAlias

__all__ = ["Base", "User", "Project", "Permission", "AuditLog", "Session", "PersonAlias"]
```

- [ ] **Step 4: Write the resolver**

Create `backend/app/core/aliases.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run the pytest command from Task 2 Step 2. Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/person_alias.py backend/app/models/__init__.py \
        backend/app/core/aliases.py backend/tests/test_core/test_aliases.py
git commit -m "feat(people): per-project alias map with one-hop resolution"
```

---

## Task 4: Route assignments through the resolver

**Files:**
- Modify: `backend/app/core/people.py:collect_assignments`
- Test: `backend/tests/test_core/test_aliases.py`

**Interfaces:**
- Consumes: `PersonResolver.resolve_cell`.
- Produces: `collect_assignments(row: Dict[str, Any], people_columns: List[Dict[str, Any]], resolver: Optional[PersonResolver] = None) -> List[Dict[str, str]]`. Output entries keep their existing keys — `role_key`, `role_label`, `person`, `person_key` — so `dashboard.py` needs no shape change.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_core/test_aliases.py`:

```python
from app.core.people import collect_assignments

DEV = {"key": "developer_name", "label": "Developer Name", "header": "Developer Name"}
CONSULTANT = {"key": "consultant", "label": "Consultant", "header": "Consultant"}


def test_a_shared_cell_becomes_one_assignment_per_person():
    out = collect_assignments({"Developer Name": "Minhaj Alam & Dawood"}, [DEV])
    assert [a["person"] for a in out] == ["Minhaj Alam", "Dawood"]
    assert {a["role_label"] for a in out} == {"Developer Name"}


def test_aliases_apply_when_a_resolver_is_supplied():
    r = _resolver(**{"dawood": "Muhammad Dawood Umer"})
    out = collect_assignments({"Developer Name": "Minhaj Alam & Dawood"}, [DEV], r)
    assert [a["person"] for a in out] == ["Minhaj Alam", "Muhammad Dawood Umer"]


def test_splitting_happens_without_a_resolver():
    """Attribution must not depend on the database being reachable."""
    out = collect_assignments({"Developer Name": "Ahmed Qamar/Asif"}, [DEV], None)
    assert len(out) == 2


def test_one_person_in_two_roles_yields_two_assignments():
    """Existing behaviour, preserved: the workload panel counts per person per role."""
    r = _resolver(**{"madiha": "Madiha Shah Bukhari"})
    out = collect_assignments(
        {"Consultant": "Madiha", "Developer Name": "Madiha Shah Bukhari"}, [CONSULTANT, DEV], r
    )
    assert [a["role_key"] for a in out] == ["consultant", "developer_name"]
    assert {a["person_key"] for a in out} == {"madiha shah bukhari"}


def test_person_key_is_derived_from_the_resolved_name():
    """Otherwise the alias would show a merged label over unmerged totals."""
    r = _resolver(**{"babar": "Babar Ali"})
    out = collect_assignments({"Developer Name": "BABAR"}, [DEV], r)
    assert out[0]["person"] == "Babar Ali"
    assert out[0]["person_key"] == "babar ali"


def test_a_blank_cell_yields_nothing():
    assert collect_assignments({"Developer Name": "  "}, [DEV]) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run the pytest command from Task 2 Step 2. Expected: FAIL — `test_a_shared_cell_becomes_one_assignment_per_person` gets `["Minhaj Alam & Dawood"]`.

- [ ] **Step 3: Rewrite `collect_assignments`**

Replace the existing function in `backend/app/core/people.py`:

```python
def collect_assignments(
    row: Dict[str, Any],
    people_columns: List[Dict[str, Any]],
    resolver: Optional["PersonResolver"] = None,
) -> List[Dict[str, str]]:
    """Every (role, person) pair named by this row.

    One cell can name several people and one person can hold several roles; each pairing
    is returned separately so callers can show a per-role split as well as a total.

    A cell used to be treated as a single value on the reasoning that splitting
    "Shaikh, Rohail" would invent two people who do not exist. That reasoning is about
    *commas*; it was over-applied to slashes and ampersands, and on the reference tracker
    60 of 257 assigned cells consequently credited a composite like "Minhaj Alam & Dawood"
    while the two real people got nothing. `split_cell` splits on / and & only.

    `resolver` applies the project's admin-confirmed alias map. It is optional and
    defaults to splitting alone, so attribution keeps working when the database is
    unreachable — degrading to unmerged names rather than to an error.
    """
    from app.core.aliases import PersonResolver  # local: aliases.py imports this module

    resolver = resolver or PersonResolver()
    out: List[Dict[str, str]] = []
    for col in people_columns or []:
        header = col.get("header")
        if not header:
            continue
        for name in resolver.resolve_cell(row.get(header)):
            out.append({
                "role_key": col.get("key") or slugify_role(col.get("label") or header),
                "role_label": col.get("label") or str(header).strip(),
                "person": name,
                # Keyed off the *resolved* name, or an alias would show a merged label
                # over totals that were never actually merged.
                "person_key": normalise_person(name),
            })
    return out
```

- [ ] **Step 4: Run the full core suite to verify nothing regressed**

```bash
cd backend && DATABASE_URL="postgresql+asyncpg://u:p@localhost/db" REDIS_URL="redis://localhost:6379" \
  DEEPSEEK_API_KEY=x JWT_SECRET=x GOOGLE_CLIENT_ID=x GOOGLE_CLIENT_SECRET=x DB_PASSWORD=x REDIS_PASSWORD=x \
  CORS_ORIGINS=http://localhost:3000 ADMIN_EMAILS=a@b.com DEFAULT_SPREADSHEET_ID=x \
  python -m pytest tests/test_core tests/test_sheets -q
```

Expected: all PASS, including `test_people_schema.py`'s existing
`test_multi_name_cell_is_one_value_not_two_invented_people` — **unchanged**. An earlier
draft of this plan warned that test would break. It does not: it asserts on
`"Shaikh, Rohail"`, a *comma* cell, which `split_cell` deliberately leaves whole. That
test is in fact the regression guard for the delimiter choice, so leave it exactly as it
is. Verified during implementation.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/people.py backend/tests/test_core/
git commit -m "feat(people): resolve aliases and shared cells in collect_assignments"
```

---

## Task 5: Merge suggestions

**Files:**
- Modify: `backend/app/core/aliases.py`
- Test: `backend/tests/test_core/test_aliases.py`

**Interfaces:**
- Consumes: `people.py:normalise_person`.
- Produces: `MergeSuggestion` dataclass with fields `name: str`, `candidates: List[str]`, `reason: str`; `suggest_merges(counts: Dict[str, int]) -> List[MergeSuggestion]`, sorted by descending occurrence of `name`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_core/test_aliases.py`:

```python
from app.core.aliases import MergeSuggestion, suggest_merges


def _by_name(suggestions):
    return {s.name: s for s in suggestions}


def test_a_shorter_name_is_suggested_against_its_longer_form():
    s = _by_name(suggest_merges({"Madiha": 4, "Madiha Shah Bukhari": 30}))
    assert s["Madiha"].candidates == ["Madiha Shah Bukhari"]
    assert s["Madiha"].reason == "subset"


def test_a_single_token_name_matches_a_full_name_containing_it():
    s = _by_name(suggest_merges({"Haider": 6, "Syed Ali Haider": 12}))
    assert s["Haider"].candidates == ["Syed Ali Haider"]


def test_a_one_character_typo_is_suggested():
    """The subset rule misses this entirely — the tokens differ, they are not nested."""
    s = _by_name(suggest_merges({"Abdullah Azfar": 9, "Abdullah Azfer": 2}))
    assert s["Abdullah Azfer"].candidates == ["Abdullah Azfar"]
    assert s["Abdullah Azfer"].reason == "typo"


def test_an_ambiguous_name_lists_every_candidate_and_picks_none():
    """"Abdullah" plausibly matches three people. Nothing in the sheet says which, so the
    screen must show all three rather than default to one."""
    s = _by_name(suggest_merges(
        {"Abdullah": 3, "Abdullah Ali": 8, "Abdullah Azfar": 9, "Abdullah Azfer": 2}
    ))
    assert sorted(s["Abdullah"].candidates) == ["Abdullah Ali", "Abdullah Azfar", "Abdullah Azfer"]


def test_unrelated_names_produce_no_suggestion():
    assert suggest_merges({"Babar Ali": 5, "Neeha Nehal": 7}) == []


def test_case_only_variants_are_not_suggested():
    """normalise_person already folds them; suggesting a merge would be noise."""
    assert suggest_merges({"Mashal Fida": 3, "mashal Fida": 2}) == []


def test_the_more_common_spelling_is_the_candidate_not_the_name():
    """The rare spelling is what gets merged away."""
    s = suggest_merges({"Babar": 2, "Babar Ali": 20})
    assert s[0].name == "Babar"
    assert s[0].candidates == ["Babar Ali"]


def test_suggestions_are_ordered_by_how_often_the_variant_occurs():
    out = suggest_merges({"Madiha": 9, "Madiha Shah Bukhari": 30, "Umair": 2, "Umair Ziad": 15})
    assert [s.name for s in out] == ["Madiha", "Umair"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run the pytest command from Task 2 Step 2. Expected: `ImportError: cannot import name 'MergeSuggestion'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/app/core/aliases.py`:

```python
from dataclasses import dataclass, field


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

    The rarer spelling is always the `name` and the commoner one the candidate: the
    sheet's dominant spelling is the one worth keeping.
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
            # Only ever suggest merging the rarer spelling into the commoner one.
            if counts[other] < counts[name] or (counts[other] == counts[name] and other < name):
                continue
            if set(tokens[name]) < set(tokens[other]):
                candidates.append(other)
                reason = reason or "subset"
            elif other_key.startswith(key + " "):
                candidates.append(other)
                reason = reason or "prefix"
            elif _is_typo_of(tokens[name], tokens[other]):
                candidates.append(other)
                reason = reason or "typo"
        if candidates:
            out.append(MergeSuggestion(name=name, candidates=candidates, reason=reason))

    out.sort(key=lambda s: (-counts[s.name], s.name))
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the pytest command from Task 2 Step 2. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/aliases.py backend/tests/test_core/test_aliases.py
git commit -m "feat(people): suggest merges from subset, prefix and typo signals"
```

---

## Task 6: Alias API

**Files:**
- Create: `backend/app/api/aliases.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_core/test_aliases.py`

**Interfaces:**
- Consumes: `PersonResolver`, `suggest_merges`, `PersonAlias`, `deps.py:require_admin`, `deps.py:get_current_user`, `deps.py:get_google_auth`, `sheets/rows_cache.py:get_tab_matrix`, `schema.py:get_people_columns`, `schema.py:get_tab_schema`, `api/dashboard.py:_row_dicts`.
- Produces: router with `GET /api/projects/{project_id}/people`, `POST /api/projects/{project_id}/aliases`, `DELETE /api/projects/{project_id}/aliases/{alias_id}`. The GET returns `{"names": [{"name": str, "count": int}], "suggestions": [{"name": str, "candidates": [str], "reason": str}], "aliases": [{"id": int, "alias": str, "canonical": str}]}`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_core/test_aliases.py`:

```python
def test_observed_name_counts_feed_the_suggestion_engine():
    """The shape api/aliases.py:list_people builds before calling suggest_merges: raw
    names counted after splitting, before aliasing."""
    from app.core.people import split_cell

    rows = [
        {"Developer Name": "Minhaj Alam & Dawood"},
        {"Developer Name": "Muhammad Dawood Umer"},
        {"Developer Name": "Muhammad Dawood Umer"},
        {"Developer Name": "Muhammad Dawood Umer"},
        {"Developer Name": "Mr. Minhaj Alam/ Dawood"},
    ]
    counts: dict = {}
    for row in rows:
        for name in split_cell(row["Developer Name"]):
            counts[name] = counts.get(name, 0) + 1

    assert counts["Dawood"] == 2
    assert counts["Muhammad Dawood Umer"] == 3
    # The rarer spelling is the one merged away, so the direction depends on the counts.
    suggestions = {s.name: s.candidates for s in suggest_merges(counts)}
    assert "Muhammad Dawood Umer" in suggestions["Dawood"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run the pytest command from Task 2 Step 2. Expected: FAIL — `NameError: name 'suggest_merges' is not defined` if Task 5 has not been completed; otherwise this test passes immediately and simply pins the shape `list_people` builds. That is intentional: it is a contract test for the next step, not a driver for new logic.

- [ ] **Step 3: Write the router**

Create `backend/app/api/aliases.py`:

```python
"""Admin endpoints for the per-project person alias map.

Separate from api/admin.py, which is already long enough that adding a fourth resource
to it would make the file hard to hold in context.

Everything here is admin-only. Applying an alias changes how every user's dashboard reads
the sheet, which is a configuration decision, not a per-user preference.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dashboard import _resolve_project, _row_dicts, _tab_for
from app.core.aliases import suggest_merges
from app.core.people import normalise_person, split_cell
from app.core.schema import get_people_columns, get_tab_schema
from app.db.engine import get_db
from app.deps import get_current_user, get_google_auth, require_admin
from app.models.person_alias import PersonAlias
from app.models.user import User
from app.sheets.client import build_sheets_service
from app.sheets.rows_cache import get_tab_matrix

logger = logging.getLogger("aliases")

router = APIRouter()


class AliasCreate(BaseModel):
    alias: str
    canonical: str


@router.get("/projects/{project_id}/people", dependencies=[Depends(require_admin)])
async def list_people(
    project_id: int,
    tab: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """Names observed on the tab, merge suggestions, and the aliases already recorded.

    Counts are taken *after* splitting and *before* aliasing: the admin needs to see what
    the sheet actually says, including the variants they are about to merge away.
    """
    project = await _resolve_project(db, current_user, project_id)
    active_tab = _tab_for(project, tab)
    tab_schema = get_tab_schema(project.schema_config or {}, active_tab)

    service = build_sheets_service(
        access_token=google_auth["access_token"],
        refresh_token=google_auth.get("refresh_token"),
    )
    headers, raw_rows, _ = await get_tab_matrix(
        service, project.spreadsheet_id, active_tab, tab_schema.get("data_start_row", 3)
    )
    rows = _row_dicts(headers, raw_rows)

    counts: Dict[str, int] = {}
    for row in rows:
        for col in get_people_columns(tab_schema):
            for name in split_cell(row.get(col["header"])):
                counts[name] = counts.get(name, 0) + 1

    existing = await db.execute(select(PersonAlias).where(PersonAlias.project_id == project_id))
    aliases = existing.scalars().all()

    return {
        "tab": active_tab,
        "names": [
            {"name": n, "count": c}
            for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        ],
        "suggestions": [
            {"name": s.name, "candidates": s.candidates, "reason": s.reason}
            for s in suggest_merges(counts)
        ],
        "aliases": [{"id": a.id, "alias": a.alias, "canonical": a.canonical} for a in aliases],
    }


@router.post("/projects/{project_id}/aliases", dependencies=[Depends(require_admin)])
async def create_alias(
    project_id: int,
    payload: AliasCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Record that one spelling means one person. Idempotent on (project, alias, canonical)."""
    await _resolve_project(db, current_user, project_id)

    alias_key = normalise_person(payload.alias)
    canonical = payload.canonical.strip()
    if not alias_key or not canonical:
        raise HTTPException(status_code=400, detail="Both alias and canonical are required.")
    if alias_key == normalise_person(canonical):
        raise HTTPException(status_code=400, detail="An alias cannot point at itself.")

    dup = await db.execute(
        select(PersonAlias).where(
            PersonAlias.project_id == project_id,
            PersonAlias.alias == alias_key,
            PersonAlias.canonical == canonical,
        )
    )
    row = dup.scalar()
    if row:
        return {"id": row.id, "status": "unchanged"}

    row = PersonAlias(
        project_id=project_id, alias=alias_key, canonical=canonical, created_by=current_user.id
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "status": "created"}


@router.delete("/projects/{project_id}/aliases/{alias_id}", dependencies=[Depends(require_admin)])
async def delete_alias(
    project_id: int,
    alias_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Undo a merge. Every alias decision has to be reversible — some of them will be wrong."""
    await _resolve_project(db, current_user, project_id)
    result = await db.execute(
        select(PersonAlias).where(PersonAlias.id == alias_id, PersonAlias.project_id == project_id)
    )
    row = result.scalar()
    if not row:
        raise HTTPException(status_code=404, detail="Alias not found.")
    await db.delete(row)
    await db.commit()
    return {"id": alias_id, "status": "deleted"}
```

Mount it in `backend/app/main.py`, beside the existing routers:

```python
from app.api.aliases import router as aliases_router
...
app.include_router(aliases_router, prefix="/api")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the pytest command from Task 2 Step 2. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/aliases.py backend/app/main.py backend/tests/test_core/test_aliases.py
git commit -m "feat(api): admin endpoints for observed names, suggestions and aliases"
```

---

## Task 7: Detection consumes the profiler

**Files:**
- Modify: `backend/app/core/schema_detect.py`
- Test: `backend/tests/test_core/test_column_profile.py`

**Interfaces:**
- Consumes: `column_profile.profile_column`, `column_profile.people_confidence`.
- Produces: `_profile_columns(raw_rows, header_idx) -> Dict[str, ColumnProfile]`; `_structural_fallback` unchanged in signature but its `people_columns` now unions keyword matches with profiler verdicts.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_core/test_column_profile.py`:

```python
from app.core.schema_detect import _structural_fallback


def _sheet(header, values):
    return [[header]] + [[v] for v in values]


def test_fallback_detects_a_people_column_a_keyword_list_would_miss():
    """The motivating bug: a header whose wording reads like a job title rather than an
    owner field. Value shape does not care what the column is called."""
    rows = _sheet("Focal Point", _rep(PEOPLE, 100))
    people = [c["header"] for c in _structural_fallback(rows)["people_columns"]]
    assert people == ["Focal Point"]


def test_fallback_does_not_promote_short_code_columns():
    rows = _sheet("Site", _rep(UPPER_CODES, 100))
    assert _structural_fallback(rows)["people_columns"] == []


def test_fallback_keeps_keyword_matches_the_profiler_abstains_on():
    """Union, not intersection: a sparsely-filled "Owner" column abstains statistically
    but is still obviously a role by its name."""
    rows = _sheet("Owner", PEOPLE[:4])
    people = [c["header"] for c in _structural_fallback(rows)["people_columns"]]
    assert people == ["Owner"]


def test_fallback_preserves_the_verbatim_header():
    rows = _sheet("Focal Point ", _rep(PEOPLE, 100))
    assert _structural_fallback(rows)["people_columns"][0]["header"] == "Focal Point "


def test_a_column_is_never_listed_twice():
    """A header matching a keyword AND profiling as people must yield one entry."""
    rows = _sheet("Resource Owner", _rep(PEOPLE, 100))
    assert len(_structural_fallback(rows)["people_columns"]) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run the pytest command from Task 1 Step 2. Expected: FAIL — `test_fallback_detects_a_people_column_a_keyword_list_would_miss` gets `[]`.

- [ ] **Step 3: Add profiling to `schema_detect.py`**

Add the import at the top:

```python
from app.core.column_profile import people_confidence, profile_column
```

Add these two helpers above `_structural_fallback`. `_header_row_index` is the header-row
heuristic already inlined in `_structural_fallback` — extract it so profiling and the
fallback cannot disagree about which row is the header:

```python
def _header_row_index(raw_rows: List[List[Any]]) -> int:
    """Earliest row with the most non-empty cells.

    Trackers routinely put a title or a blank spacer above the real header row, which is
    why this is not simply row 0. Extracted so that _profile_columns and
    _structural_fallback cannot pick different header rows for the same sheet — profiling
    one row off would fold the header text into the value sample.
    """
    header_idx = 0
    best_filled = -1
    for idx, row in enumerate((raw_rows or [])[:10]):
        filled = sum(1 for cell in row if str(cell).strip())
        if filled > best_filled:
            best_filled = filled
            header_idx = idx
    return header_idx


def _profile_columns(raw_rows: List[List[Any]], header_idx: Optional[int] = None) -> Dict[str, Any]:
    """Value-shape statistics per verbatim header, over the rows below the header row."""
    rows = raw_rows or []
    if header_idx is None:
        header_idx = _header_row_index(rows)
    header_row = rows[header_idx] if header_idx < len(rows) else []
    data_rows = rows[header_idx + 1:]
    profiles = {}
    for col_idx, header in enumerate(str(h) for h in header_row):
        if not header.strip():
            continue
        values = [row[col_idx] for row in data_rows if col_idx < len(row)]
        profiles[header] = profile_column(values)
    return profiles
```

Then replace the inlined header-row loop at the top of `_structural_fallback` with
`header_idx = _header_row_index(rows)`.

In `_structural_fallback`, replace the `people_columns` construction with:

```python
    # Two signals, unioned, because they fail differently. The keyword list reads header
    # wording and is blind to any vocabulary it was not given — it is what missed a
    # "Consultant" column naming 33 people. The profiler reads value shape and is blind
    # to meaning — it cannot separate a small team from a short status enum. Requiring
    # both to agree would have left the motivating case undetected, so either suffices;
    # a false positive is a visible, removable role and can never reach the spreadsheet.
    profiles = _profile_columns(rows, header_idx)
    profiled_headers = [
        h for h in non_empty
        if people_confidence(profiles.get(verbatim.get(h, h)) or profile_column([])) == "likely"
    ]
    people_headers = list(dict.fromkeys(people_headers + profiled_headers))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the pytest command from Task 1 Step 2, then the full suite from Task 4 Step 4. Expected: all PASS.

- [ ] **Step 5: Give the LLM the same evidence**

In `SCHEMA_DETECTION_PROMPT`, replace the `people_columns` bullet's trailing sentence with:

```
     Judge from the VALUES, not the header wording: a column whose values are personal
     names is a people column whatever it is called, and a column named like a role but
     holding categories or codes is not. Header wording differs by team and by language;
     the values do not.
```

and in `detect_schema_config`, pass the profiles alongside the rows so the model has them:

```python
        profiles = _profile_columns(raw_rows)
        profile_json = json.dumps(
            {h: {"distinct_ratio": round(p.cardinality, 3), "mean_tokens": round(p.mean_tokens, 2),
                 "mean_length": round(p.mean_length, 1), "title_case": round(p.title_case_ratio, 2)}
             for h, p in profiles.items()},
            ensure_ascii=False,
        )
```

Append `\n\nColumn value statistics:\n{profile_json}` to the formatted prompt string, and add `profile_json=profile_json` to the `.format(...)` call. Add `{profile_json}` as a placeholder at the end of `SCHEMA_DETECTION_PROMPT`.

- [ ] **Step 6: Run the full suite and commit**

```bash
cd backend && ... python -m pytest tests/test_core tests/test_sheets -q
git add backend/app/core/schema_detect.py backend/tests/test_core/test_column_profile.py
git commit -m "feat(schema): detect people columns from value shape as well as wording"
```

---

## Task 8: Dashboard and summarize use the resolver

**Files:**
- Modify: `backend/app/api/dashboard.py`, `backend/app/sheets/read.py`, `backend/app/core/tool_dispatch.py`
- Test: manual, against the deploy (these paths need Postgres, Redis and Sheets)

**Interfaces:**
- Consumes: `PersonResolver.from_rows`, `PersonAlias`.
- Produces: no new public functions. `summarize` gains an optional `resolver` keyword.

- [ ] **Step 1: Add a resolver helper to `dashboard.py`**

```python
from app.core.aliases import PersonResolver
from app.models.person_alias import PersonAlias


async def _resolver_for(db: AsyncSession, project_id: int) -> PersonResolver:
    """The project's alias map, built once per request.

    Failure degrades to unmerged names rather than to an error, matching how the row
    cache treats an unreachable Redis: a dashboard showing one person twice is worth far
    more than a dashboard showing an exception.
    """
    try:
        result = await db.execute(select(PersonAlias).where(PersonAlias.project_id == project_id))
        return PersonResolver.from_rows(result.scalars().all())
    except Exception as e:
        logger.warning(f"Alias map unavailable for project {project_id}: {e}")
        return PersonResolver()
```

- [ ] **Step 2: Use it in both endpoints**

In `project_analytics`, after `people = get_people_columns(tab_schema)`:

```python
    resolver = await _resolver_for(db, project_id)
```

and change the assignment loop to `for assignment in collect_assignments(row, people, resolver):`.

In `list_rows`, replace the person-filter block so the filter compares against resolved names:

```python
        if person_norm:
            names = {
                normalise_person(n)
                for p in scoped_people
                for n in resolver.resolve_cell(row.get(p["header"]))
            }
            if person_norm not in names:
                continue
```

adding `resolver = await _resolver_for(db, project_id)` above the loop. Without this the filter dropdown lists merged names while the grid still matches raw ones, and selecting a merged person returns nothing.

- [ ] **Step 3: Resolve in `summarize(count_by_field)` when grouping on a people column**

In `backend/app/sheets/read.py`, change the `summarize` signature to accept `resolver: Any = None`, and inside the `count_by_field` branch:

```python
        # When the grouping column is one of this tab's people columns, count resolved
        # people rather than raw cells — otherwise chat reports "Minhaj Alam & Dawood" as
        # a person while the dashboard beside it reports two, from the same sheet.
        people_headers = {p["header"].lower().strip() for p in get_people_columns(schema_config)}
        is_people_column = canonical.lower().strip() in people_headers

        counts = {}
        for r in rows:
            raw = str(r[idx]).strip()
            if is_people_column and resolver is not None:
                for name in resolver.resolve_cell(raw) or ["(blank)"]:
                    counts[name] = counts.get(name, 0) + 1
            else:
                val = raw or "(blank)"
                counts[val] = counts.get(val, 0) + 1
```

- [ ] **Step 4: Pass a resolver from the dispatcher**

In `backend/app/core/tool_dispatch.py`, in the `summarize` branch, build the resolver from `db_session` and the active project. The project id is not currently in scope there — resolve it from `spreadsheet_id`:

```python
            elif tool_name == "summarize":
                from app.sheets.read import summarize
                from app.core.aliases import PersonResolver
                from app.models.person_alias import PersonAlias
                from app.models.project import Project

                resolver = PersonResolver()
                try:
                    proj = (await db_session.execute(
                        select(Project).where(Project.spreadsheet_id == spreadsheet_id)
                    )).scalar()
                    if proj:
                        rows_ = (await db_session.execute(
                            select(PersonAlias).where(PersonAlias.project_id == proj.id)
                        )).scalars().all()
                        resolver = PersonResolver.from_rows(rows_)
                except Exception as e:
                    logger.warning(f"Alias map unavailable for summarize: {e}")

                return await summarize(
                    spreadsheet_id, active_tab, args, schema_config, column_map, service,
                    resolver=resolver,
                )
```

Add `from sqlalchemy import select` at the top of `tool_dispatch.py` if not already present.

- [ ] **Step 5: Run the full suite and commit**

```bash
cd backend && ... python -m pytest tests/test_core tests/test_sheets -q
git add backend/app/api/dashboard.py backend/app/sheets/read.py backend/app/core/tool_dispatch.py
git commit -m "feat: resolve people through the alias map on both dashboard and agent paths"
```

---

## Task 9: Admin triage screen

**Files:**
- Create: `frontend/src/app/admin/people/page.tsx`
- Modify: `frontend/src/app/admin/page.tsx` (add a link)

**Interfaces:**
- Consumes: `GET /api/projects/{id}/people`, `POST /api/projects/{id}/aliases`, `DELETE /api/projects/{id}/aliases/{id}`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Build the page**

Create `frontend/src/app/admin/people/page.tsx`:

```tsx
"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import { useSession } from "next-auth/react"
import { X } from "lucide-react"

/**
 * Triage screen for a project's person aliases.
 *
 * After shared cells are split, one person still appears under several spellings —
 * "Madiha" and "Madiha Shah Bukhari", "Abdullah Azfar" and "Abdullah Azfer". The app
 * never merges these on its own: merging hides one person inside another and the error
 * is silent, unlike a wrong split or a wrong role, which are visible immediately.
 *
 * So this screen suggests and a human decides. Where a name matches several people —
 * "Abdullah" matches three — every candidate is rendered identically with no default,
 * because presenting one as the obvious answer would be the app making exactly the
 * judgement it is not entitled to make.
 */

interface Project { id: number; project_name: string; schema_config?: { tabs?: Record<string, unknown> } }
interface ObservedName { name: string; count: number }
interface Suggestion { name: string; candidates: string[]; reason: string }
interface AliasRow { id: number; alias: string; canonical: string }
interface PeopleResponse {
  tab: string
  names: ObservedName[]
  suggestions: Suggestion[]
  aliases: AliasRow[]
}

export default function AdminPeoplePage() {
  const { data: session } = useSession()
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState<number | null>(null)
  const [tab, setTab] = useState("")
  const [data, setData] = useState<PeopleResponse | null>(null)
  const [dismissed, setDismissed] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const headers = useMemo(
    () => ({
      Authorization: `Bearer ${session?.apiToken || ""}`,
      "X-Google-Access-Token": session?.googleAccessToken || "",
    }),
    [session]
  )

  useEffect(() => {
    if (!session?.apiToken) return
    fetch("/api/projects", { headers })
      .then((r) => r.json())
      .then((rows: Project[]) => {
        setProjects(rows)
        if (rows.length && projectId === null) setProjectId(rows[0].id)
      })
      .catch(() => setError("Could not load projects."))
  }, [session, headers, projectId])

  const tabs = useMemo(() => {
    const p = projects.find((x) => x.id === projectId)
    return Object.keys(p?.schema_config?.tabs || {})
  }, [projects, projectId])

  const load = useCallback(async () => {
    if (!projectId || !session?.apiToken) return
    setError("")
    try {
      const q = tab ? `?tab=${encodeURIComponent(tab)}` : ""
      const res = await fetch(`/api/projects/${projectId}/people${q}`, { headers })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Failed to load.")
      setData(await res.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load.")
      setData(null)
    }
  }, [projectId, tab, headers, session])

  useEffect(() => { load() }, [load])

  const merge = async (alias: string, canonical: string) => {
    setBusy(true)
    try {
      const res = await fetch(`/api/projects/${projectId}/aliases`, {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify({ alias, canonical }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Refused.")
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save that merge.")
    } finally {
      setBusy(false)
    }
  }

  const removeAlias = async (id: number) => {
    setBusy(true)
    try {
      await fetch(`/api/projects/${projectId}/aliases/${id}`, { method: "DELETE", headers })
      await load()
    } finally {
      setBusy(false)
    }
  }

  const visible = (data?.suggestions || []).filter((s) => !dismissed.has(s.name))

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="display-md">People</h1>
        <p className="mt-1 text-[13px] text-ink-400">
          Names as this sheet spells them. Merging changes how the app reads the sheet —
          the spreadsheet itself is never modified.
        </p>
      </div>

      <div className="flex flex-wrap gap-3">
        <select
          aria-label="Project"
          value={projectId ?? ""}
          onChange={(e) => { setProjectId(Number(e.target.value)); setTab(""); setDismissed(new Set()) }}
          className="field w-auto cursor-pointer py-1.5 text-[13px]"
        >
          {projects.map((p) => <option key={p.id} value={p.id}>{p.project_name}</option>)}
        </select>
        {tabs.length > 1 && (
          <select
            aria-label="Tab"
            value={tab}
            onChange={(e) => { setTab(e.target.value); setDismissed(new Set()) }}
            className="field w-auto cursor-pointer py-1.5 text-[13px]"
          >
            <option value="">Default tab</option>
            {tabs.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        )}
      </div>

      {error && <p className="text-sm text-failed">{error}</p>}

      <section className="space-y-3">
        <h2 className="label-micro">Suggested merges</h2>
        {visible.length === 0 && (
          <p className="text-[12px] text-ink-500 italic">
            {data ? "Nothing looks like a duplicate." : "Loading…"}
          </p>
        )}
        {visible.map((s) => {
          const count = data?.names.find((n) => n.name === s.name)?.count ?? 0
          return (
            <div key={s.name} className="card space-y-2 rounded-lg px-3 py-2.5">
              <div className="flex items-baseline gap-2">
                <span className="text-sm font-medium text-ink-100">{s.name}</span>
                <span className="text-[11px] text-ink-500">
                  {count} {count === 1 ? "row" : "rows"} · {s.reason}
                </span>
              </div>
              {s.candidates.length > 1 && (
                <p className="text-[11px] text-ink-500">
                  Matches {s.candidates.length} people — pick one, or leave separate.
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                {s.candidates.map((c) => (
                  <button
                    key={c}
                    disabled={busy}
                    onClick={() => merge(s.name, c)}
                    className="btn btn-secondary text-[12px] disabled:opacity-40"
                  >
                    Merge into &ldquo;{c}&rdquo;
                  </button>
                ))}
                <button
                  onClick={() => setDismissed((d) => new Set(d).add(s.name))}
                  className="btn btn-ghost text-[12px]"
                >
                  Leave separate
                </button>
              </div>
            </div>
          )
        })}
      </section>

      <section className="space-y-2">
        <h2 className="label-micro">Recorded merges</h2>
        {(data?.aliases || []).length === 0 && (
          <p className="text-[12px] text-ink-500 italic">None yet.</p>
        )}
        {(data?.aliases || []).map((a) => (
          <div key={a.id} className="flex items-center gap-2 text-[12.5px] text-ink-300">
            <code className="font-mono text-brass-300">{a.alias}</code>
            <span className="text-ink-500">→</span>
            <span>{a.canonical}</span>
            <button
              onClick={() => removeAlias(a.id)}
              disabled={busy}
              aria-label={`Remove merge of ${a.alias}`}
              className="text-ink-500 transition hover:text-failed disabled:opacity-40"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </section>

      <section className="space-y-2">
        <h2 className="label-micro">All names on this tab</h2>
        <div className="flex flex-wrap gap-1.5">
          {(data?.names || []).map((n) => (
            <span
              key={n.name}
              className="rounded border border-[var(--color-rule)] px-2 py-0.5 text-[12px] text-ink-300"
            >
              {n.name} <span className="text-ink-500 tabular-nums">{n.count}</span>
            </span>
          ))}
        </div>
      </section>
    </div>
  )
}
```

In `frontend/src/app/admin/page.tsx`, add a link to `/admin/people` alongside the existing
admin section links, labelled **People** with the description "Merge duplicate names".

- [ ] **Step 2: Verify the build and lint**

```bash
cd frontend && npm run build && npm run lint
```

Expected: build succeeds; lint reports **51 problems (27 errors, 24 warnings)** — the pre-existing baseline, unchanged.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/admin/people/page.tsx frontend/src/app/admin/page.tsx
git commit -m "feat(admin): person alias triage screen"
```

---

## Task 10: Documentation

**Files:**
- Modify: `TDD.md`

- [ ] **Step 1: Write the sections**

- **§7.5 (new), "Who a row is assigned to"** — the profiler, its measured thresholds, the union rule between profiler and LLM, `abstain` not being a vote, the `Status` limit, the Latin-script limit, the splitter's delimiter set and why a comma is excluded, and the one-hop alias map.
- **§10.3 (new)** — the three alias endpoints and their admin-only gating.
- **§14.7 (new)** — the triage screen, and why ambiguous suggestions show every candidate with no default.
- **§16.3** — trim: people-column detection no longer depends solely on header wording.

- [ ] **Step 2: Commit**

```bash
git add TDD.md
git commit -m "docs(tdd): record the people-accuracy design as built"
```

---

## Verification against the deploy

No Docker locally, so these run against the deployed instance after merge.

- [ ] Re-run detection on the HEDP project from `/admin/projects`. `Consultant` appears in `people_columns` with its label intact, alongside `Developer Name`.
- [ ] Load the project dashboard → Workload. Both roles appear. `Minhaj Alam & Dawood` is **gone** as a person; Minhaj and Dawood each carry those rows.
- [ ] `/admin/people` lists ~63 observed names and ~13 suggestions. Confirm the `Madiha` → `Madiha Shah Bukhari` merge; the two entries collapse into one and the dashboard's "Assigned to" filter shrinks by one.
- [ ] `Abdullah` shows three candidates with no default selected.
- [ ] Remove that alias; the two entries reappear. Every merge is reversible.
- [ ] Ask chat "who has the most items?" and check the answer matches the dashboard, since both now resolve through `collect_assignments`.
- [ ] Open the sheet in Google Sheets and confirm **no cell changed**.
- [ ] Load the Fauji project: its three existing roles are unchanged, and `Business Owner` (5 values, below `MIN_SAMPLE`) is not reclassified.

---

## Self-review notes

- **Spec coverage:** profiler §1 → Task 1; detection §2 → Task 7; splitting §3 → Task 2; alias map §4 → Tasks 3, 4, 6; admin screen §5 → Tasks 5, 9; failure modes → Tasks 3, 4, 8; testing → Tasks 1–5.
- **Known gap, accepted:** the spec's "editor shows columns where the signals disagreed" is not implemented. Under the union rule nothing waits on the admin, so this is presentational. The existing `RoleColumnsEditor` already lists and removes roles, which covers the corrective need. Revisit if false positives prove common.
- **Interface consistency:** `resolve_cell` (not `resolve`) throughout; `people_confidence` returns strings, never booleans; `collect_assignments`' third parameter is `resolver` in every call site.

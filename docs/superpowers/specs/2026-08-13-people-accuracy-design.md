# People Accuracy — design

**Status:** approved design, not yet implemented
**Date:** 2026-08-13

## Context

The project dashboard (PRs #30–#34) shipped a workload panel that reports who is carrying
what. On the live trackers it is wrong, in two compounding ways, and a PM reading it today
draws false conclusions.

**Half the assignments are invisible.** The HEDP tracker has two columns naming people —
`Consultant` (401 non-blank values, 33 distinct) and `Developer Name` (257 non-blank, 37
distinct). Schema detection found only the second. It missed `Consultant` because detection
reasons about *header wording*, and "Consultant" reads like a job title rather than an owner
field in English.

That failure mode is the important part. The next sheet will say `Owner`, `Assigned To`,
`Resource`, `PIC`, or a header in another language entirely, and header-semantics detection
will keep failing one sheet at a time. The product's primary constraint is that it works on
*any* tracking sheet; a detector that depends on recognising English job titles does not
meet it.

**A quarter of assignments are credited to people who do not exist.** 60 of 257
`Developer Name` cells name two people — `Minhaj Alam & Dawood`,
`Syed Ali Haider/ Muhammad Ashar Mateen`, `Ahmed Qamar/Asif`. Each becomes its own entry in
the workload panel, so the work is attributed to a fictional composite and the two real
people get no credit for it.

**And the survivors don't deduplicate.** After splitting those cells, 13 groups remain where
one person appears under several spellings: `madiha` / `madiha shah bukhari`,
`babar` / `babar ali`, `dawood` / `muhammad dawood umer` / `muhmmad dawood umer`.

## Goals

1. Detect people columns from evidence that is not language- or vocabulary-specific.
2. Attribute a shared cell to each person named in it.
3. Let an admin declare that two spellings are one person, without the app ever guessing.
4. Change nothing in the spreadsheet.

## Non-goals

- **Writing to the sheet.** Nothing in this design mutates a cell. The alias map is a read
  layer; the sheet keeps saying whatever it says. Cleanup-by-write was considered and
  rejected: it bakes a judgement call into production data, and anyone typing a variant
  tomorrow reintroduces the problem.
- **Automatic merging of names.** See "the principle" below.
- **A Postgres row store for sheet data.** Worth doing — it would remove the full-tab rescan
  on every agent tool call — but it is an architectural change with real staleness
  questions, and it is deliberately deferred to its own spec.
- Detection of anything other than people columns.

## The principle

> **Automatic when it reveals. Confirmed when it conflates.**

Detecting a people column reveals data that was hidden — apply it automatically; a wrong
guess is visible as a junk role and removable in one click. Splitting `A & B` reveals two
people — apply it automatically. Merging two names *hides* one inside the other, and the
error is silent: nobody notices a person who stopped existing. That always requires a human.

The `abdullah` case in the live data is the proof. It plausibly matches `abdullah ali`,
`abdullah azfar` and `abdullah azfer`, and nothing in the sheet can say which. Any
auto-merge rule confident enough to resolve it is confident enough to be wrong.

A second rule falls out of the genericness constraint and is worth stating because it
overrode an earlier draft of this design: **vocabulary lives in data, never in code.** The
splitter was going to strip honorifics (`Mr.`, `Dr.`); that is English hardcoded into the
engine. Instead the splitter knows only delimiters, and `mr. minhaj alam → Minhaj Alam`
becomes an ordinary alias row.

## Architecture

One new primitive, three consumers:

```
core/column_profile.py                    NEW — pure, no I/O, fully unit-testable
  profile_column(values) -> ColumnProfile
  people_confidence(profile) -> "likely" | "abstain" | "unlikely"
        │
        ├──> core/schema_detect.py        profile + sample values into the LLM prompt;
        │                                 also drives _structural_fallback
        ├──> admin role editor            evidence beside every candidate column
        └──> (no other consumer)

core/people.py                            EXTENDED
  split_cell(raw) -> list[str]            structural only: / and & , never comma
  PersonResolver(aliases)                 one-hop alias lookup
        └──> collect_assignments()        used by dashboard AND summarize alike

models/person_alias.py                    NEW table, created by init_db on deploy
api/admin.py                              NEW alias CRUD + suggestions, admin-only
```

`column_profile.py` never sees a column name — it takes a list of strings and returns
statistics. That is what makes it work on a sheet in any language or domain.

### 1. The column profiler

Per column, over the tab's scanned values:

| field | meaning |
|---|---|
| `n` | non-blank values |
| `cardinality` | distinct / non-blank |
| `mean_tokens` | whitespace-separated tokens per value |
| `mean_length` | characters per value |
| `alpha_ratio` | share matching `^[\p{L}\s.'\-/&]+$` |
| `title_case_ratio` | share starting upper-then-lower |
| `repeat_ratio` | share of values occurring more than once |

`people_confidence` returns `likely` when every criterion below holds, `abstain` when
exactly one fails or `n < 20`, and `unlikely` otherwise:

```
0.02 <= cardinality <= 0.6      not a status enum; not a unique-per-row id or description
1.5  <= mean_tokens <= 4        names are multi-token; category codes are not
8    <= mean_length <= 30       names are longer than codes, shorter than free text
        alpha_ratio >= 0.8      no digits, no URLs, no dates
        title_case_ratio >= 0.6 consistently capitalised
        repeat_ratio >= 0.3     people recur across rows
```

**Thresholds validated against every populated column of both live trackers**, which is how
the first draft's false positives were found and eliminated:

```
likely    Consultant          card=0.077 tok=2.10 len=12.0 title=0.83  <- detection missed this
likely    Developer Name      card=0.132 tok=2.94 len=18.6 title=0.96
unlikely  University          card=0.071 tok=1.12 len=4.8  title=0.03  short uppercase codes
unlikely  Development Tyoe    card=0.063 tok=1.00 len=5.2  title=0.52  single-token labels
unlikely  Development         card=0.788 tok=3.79 len=27.4 title=0.80  free text
unlikely  FSD Sheet link      card=0.788 tok=3.88 len=60.6 title=0.33  URLs
unlikely  WRICEF No.          card=0.965 tok=1.00 len=9.0  title=0.00  unique ids
abstain   Status              card=0.015 tok=1.58 len=10.0 title=0.92  fails cardinality by 0.005
```

`mean_tokens`, `mean_length` and `title_case_ratio` are what separate a person from a
category code; the first draft used only cardinality and misclassified `University`.

**Known limit, deliberately not engineered around.** `Status` clears every test but the
cardinality floor, by 0.005. A sheet with twelve statuses over 400 rows would be classified
as people. This is not tunable: a team of eight and a workflow of eight states are
statistically identical, and only meaning separates them. That is precisely why the LLM
stays in the loop.

**Script limit.** `title_case_ratio` and token counting are Latin-script assumptions. The
fields are computed and thresholded independently so a CJK or Arabic sheet can lean on
cardinality, length and repeat; the shape of that adjustment is left until a sheet needs it,
rather than guessed at now.

### 2. Detection

The LLM and the profiler fail differently, so both run:

- The **LLM** reads meaning and is biased by vocabulary — it skipped `Consultant`.
- The **profiler** reads shape and is blind to meaning — it cannot separate a small team
  from a status enum.

A column becomes a role if **either** signal positively says people. This follows the
"reveal automatically" principle and is the decision that actually fixes `Consultant` with
no human step — an intersection rule would have left the motivating case needing a click,
since the LLM is the signal that got it wrong. False positives are the accepted cost: they
are visible, reversible, and cannot reach the spreadsheet.

`abstain` is not a vote. A profiler result of `abstain` neither applies a role nor blocks
one; only `likely` applies. So `Status` (which abstains, failing cardinality by 0.005) does
not become a role, because the LLM correctly reads it as a status column and the profiler
declines to disagree.

`SCHEMA_DETECTION_PROMPT` gains each column's profile and a few real sample values, so the
model reasons from evidence rather than from the header alone. `_structural_fallback` uses
`people_confidence` directly, which finally gives that path a testable basis.

Existing projects are unaffected until an admin re-runs detection. That flow is already
`POST /projects/detect-metadata` (which *returns* a config and never writes) followed by an
explicit `PUT /projects/{id}` — a human confirmation gate that already exists and needs no
new machinery.

### 3. Splitting shared cells

```python
split_cell("Mr. Minhaj Alam/ Dawood") -> ["Mr. Minhaj Alam", "Dawood"]
```

Delimiters are `/` and `&` only. **Never comma** — `Shaikh, Rohail` is plausibly one person
written surname-first, and inventing a colleague is worse than under-splitting. Slash and
ampersand have no such reading.

Splitting is about attribution, not deduplication: it moves the distinct-name count only
70 → 63, because the fragments are mostly names that already existed. Its value is that 60
rows start crediting the right two people.

### 4. The alias map

```
person_aliases                            (models/person_alias.py)
  id            pk
  project_id    fk -> projects.id           aliases are per-project; two orgs share no staff
  alias         text, normalised            what the sheet says
  canonical     text                        how it should read
  created_by    fk -> users.id
  created_at    timestamptz
  UNIQUE(project_id, alias, canonical)
```

The composite unique key lets one alias map to several canonical people, which expresses a
comma-separated shared cell without teaching the splitter about commas.

**Resolution is one hop.** `alias → canonical`, and a canonical is never itself looked up.
Cycles become impossible by construction rather than by validation.

Order of operations in `collect_assignments`, which is the single place this happens so the
dashboard and `summarize` cannot diverge:

```
raw cell
  1. whole-cell alias lookup      <- escape hatch: beats the splitter entirely
  2. split_cell                   <- structural, no vocabulary
  3. per-fragment alias lookup
  4. dedupe within the row        <- one credit per person per role per row
```

Step 1 is what makes automatic splitting safe. A team named `R&D`, or a genuine name
containing a slash, is fixed by adding one alias row. Every automatic decision has a manual
answer.

### 5. Admin screen

Lists observed names for a project with occurrence counts, and ranked **suggestions** from
three cheap signals — token-subset (`madiha` ⊂ `madiha shah bukhari`), prefix, and
edit-distance ≤ 2 on a single token. That last one catches `abdullah azfar` /
`abdullah azfer`, which subset matching misses entirely.

Suggestions are never applied on their own. Where a name matches several candidates —
`abdullah` matches three — all are shown, none is defaulted, and "leave separate" is a
first-class choice.

The same screen lists every applied role with the evidence behind it — which signal claimed
it, and the profile that supports or contradicts it — so **removing** a wrongly-applied role
is an informed decision rather than a guess. Under the union rule the admin's job here is
rejection, not approval; nothing waits on them to be useful.

Volume is manageable: 13 groups on HEDP, a five-minute triage.

## Failure modes

| condition | behaviour |
|---|---|
| DB unreachable | resolution falls back to raw names — today's behaviour, never an error |
| `n < 20` | profiler abstains; Fauji's 5-value `Business Owner` is not classified from noise |
| profiler and LLM disagree | role still applied (union), surfaced in the editor with evidence |
| wrong role auto-applied | junk row in the workload panel, removable in one click, sheet untouched |
| alias points at an alias | resolves one hop and stops; no chain, no cycle |
| splitter wrong for a cell | whole-cell alias overrides it |

Alias lookups build one dict per request; 412 rows × 2 columns is not a performance concern.

## Testing

Following `tests/test_core/test_due_column.py` — parametrised over shapes measured on the
real trackers, since that is the pattern that caught the `Completed`/`Complete` divergence.

Fixtures are named by **shape, not by origin**, so no customer's vocabulary enters the suite:

```python
def test_multi_token_titlecase_names_are_people()        # card .08 tok 2.1 len 12  title .83
def test_short_uppercase_codes_are_not_people()          # card .07 tok 1.1 len 4.8 title .03
def test_single_token_category_labels_are_not_people()   # card .06 tok 1.0 len 5.2
def test_free_text_is_not_people()                       # card .79 len 27.4
def test_unique_identifiers_are_not_people()             # card .97 alpha 0
def test_low_cardinality_enum_is_not_people()            # documents the Status limit
def test_small_samples_abstain()                         # n < 20
```

Then: `split_cell` (`/`, `&`, mixed, never comma, whole-cell override); `PersonResolver`
(one hop, unknown names pass through, per-row dedupe); `collect_assignments` end-to-end on
the real messy examples; suggestions (subset / prefix / edit-distance, and `abdullah`
yielding three candidates rather than a merge); and `require_admin` on every alias endpoint.

## Verification against the deploy

No Docker locally, so end-to-end checks run against the deployed instance:

1. Re-run detection on HEDP; `Consultant` appears as a role with its label intact.
2. The workload panel shows both roles, and `Minhaj Alam & Dawood` is gone — Minhaj and
   Dawood each carry those rows.
3. Confirm the `madiha` suggestion; the two entries collapse into one and the "Assigned to"
   filter shrinks by one.
4. Confirm nothing in the spreadsheet changed — compare a reassigned cell before and after.
5. Ask chat "who has the most items?" and check it agrees with the dashboard, since both now
   resolve through `collect_assignments`.
6. Load Fauji; its three existing roles are unchanged and `Business Owner` is not
   reclassified from its 5 values.

## Follow-ups, explicitly not in this spec

- Postgres row store for sheet data (removes the full-tab rescan per agent tool call).
- Chat persistence; undo from `audit_logs`; streamed replies; CSV export; scheduled digests.
- Non-Latin script handling in the profiler, once a sheet needs it.

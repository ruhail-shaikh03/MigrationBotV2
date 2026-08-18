"""What is wrong with a tab's data, expressed only in terms the tab itself declares.

The dashboard already answers "what is the state of the work". This answers the question
underneath it: how much of that state is actually recorded. On the reference tracker 362
of 412 rows carry no deadline, which means the overdue count on the analytics panel — a
confident, precise number — is computed over an eighth of the sheet. A reader has no way
to know that from the analytics panel alone.

`data_quality.py:DataQualityChecker` was the obvious thing to reuse and is deliberately
not reused here. Its checks are written against one customer's WRICEF tracker: it falls
back to literal "RICEFW ID", "Dev Status", "Required" and "Sign-Off Date" headers, and on
a sheet without them `consistency_checks` returns an empty list. Empty reads as "no
problems found", so the panel would have been most reassuring exactly where it knew least
— and the app's first requirement is to work on any tracking sheet, not on that one. It
still serves the agent, where a human reads its output in context; this module is what the
dashboard reports from.

Two rules follow from that and shape everything below:

* **A check that could not run is not a check that passed.** Anything whose column the
  schema does not name is reported in `skipped`, with the reason, never as a zero.
* **Vocabulary comes from schema_config.** No header string is spelled in this file.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.aliases import suggest_merges
from app.core.people import count_observed_names, parse_date
from app.core.schema import bind_columns, get_due_column, get_people_columns, resolve_header

# suggest_merges compares every distinct name against every other. That is nothing at the
# 26 names a real tracker has, and 25 million comparisons on a 5000-name tab. Past this
# the check is skipped rather than allowed to hold the request open — a slow health panel
# would simply stop being opened.
MAX_NAMES_FOR_MERGE_SCAN = 1000

# Enough to recognise the shape of the problem — "ah, it is the 17/0/2026 rows" — without
# turning a summary into an export. The count is the number that matters; the samples
# exist so the reader knows where to look.
MAX_SAMPLES = 5


@dataclass
class Check:
    key: str
    label: str
    count: int
    total: int
    detail: str
    samples: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def severity(self) -> str:
        """Graded by share of the tab, because a count alone does not say how bad it is.

        Three rows without a deadline is a footnote; 362 means the deadline column is not
        being used and every date-based number on the dashboard is quietly partial.
        """
        if self.count == 0:
            return "ok"
        share = self.count / self.total if self.total else 1.0
        if share >= 0.5:
            return "error"
        if share >= 0.1:
            return "warning"
        return "info"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "count": self.count,
            "total": self.total,
            "severity": self.severity,
            "detail": self.detail,
            "samples": self.samples,
        }


def _blank(value: Any) -> bool:
    return not str(value or "").strip()


def assess_tab(
    headers: List[str],
    rows: List[Dict[str, str]],
    tab_schema: Dict[str, Any],
    data_start_row: int = 3,
) -> Dict[str, Any]:
    """Run every check the tab's schema makes meaningful, and report what it does not.

    `rows` are the dicts `dashboard._row_dicts` produces — keyed by the sheet's own
    headers. `data_start_row` only converts an index into the row number a reader would
    see in Google Sheets, so a sample can be looked up.
    """
    total = len(rows)
    checks: List[Check] = []
    skipped: List[Dict[str, str]] = []

    id_header = resolve_header(headers, tab_schema.get("primary_id_column"))
    status_header = resolve_header(headers, tab_schema.get("status_column"))
    due_header = resolve_header(headers, get_due_column(tab_schema, headers))
    people = bind_columns(get_people_columns(tab_schema), headers)

    def row_ref(index: int, row: Dict[str, str], column: str = "", value: Any = "") -> Dict[str, Any]:
        return {
            "row": data_start_row + index,
            "id": str(row.get(id_header, "")).strip() if id_header else "",
            "column": column,
            "value": str(value or "").strip(),
        }

    # --- identity -----------------------------------------------------------------
    if id_header:
        missing = [row_ref(i, r) for i, r in enumerate(rows) if _blank(r.get(id_header))]
        checks.append(Check(
            key="no_id",
            label=f"Rows with no {id_header}",
            count=len(missing),
            total=total,
            # Not a cosmetic gap: every write this app performs addresses a row by this
            # value, so a row without one cannot be edited from chat or the grid at all.
            detail="These rows cannot be updated — every write addresses a row by this column.",
            samples=missing[:MAX_SAMPLES],
        ))

        seen: Dict[str, List[int]] = {}
        for i, row in enumerate(rows):
            value = str(row.get(id_header, "")).strip()
            if value:
                seen.setdefault(value.casefold(), []).append(i)
        dupes = {v: idx for v, idx in seen.items() if len(idx) > 1}
        checks.append(Check(
            key="duplicate_id",
            label=f"Duplicated {id_header} values",
            count=sum(len(idx) for idx in dupes.values()),
            total=total,
            # A lookup by ID returns one row. Which one is an accident of sheet order, so
            # a write aimed at a duplicated ID may land on the wrong row silently.
            detail="An ID matching several rows resolves to whichever comes first — a write may hit the wrong one.",
            samples=[
                row_ref(idx[0], rows[idx[0]], id_header, f"{len(idx)} rows share this ID")
                for idx in list(dupes.values())[:MAX_SAMPLES]
            ],
        ))
    else:
        skipped.append({
            "key": "no_id",
            "label": "Row identity",
            "reason": "This tab's schema names no primary ID column.",
        })

    # --- deadlines ----------------------------------------------------------------
    if due_header:
        missing = [
            row_ref(i, r, due_header) for i, r in enumerate(rows) if _blank(r.get(due_header))
        ]
        checks.append(Check(
            key="no_deadline",
            label=f"Rows with no {due_header}",
            count=len(missing),
            total=total,
            detail="Overdue counts are computed only over rows that have a deadline.",
            samples=missing[:MAX_SAMPLES],
        ))
    else:
        skipped.append({
            "key": "no_deadline",
            "label": "Deadlines",
            "reason": "No column on this tab reads as a deadline, so nothing can be overdue.",
        })

    # --- dates that look like data and are not -------------------------------------
    date_headers: List[str] = []
    for value in (tab_schema.get("date_columns") or {}).values():
        header = resolve_header(headers, value)
        if header and header not in date_headers:
            date_headers.append(header)
    if due_header and due_header not in date_headers:
        date_headers.append(due_header)

    if date_headers:
        unreadable: List[Dict[str, Any]] = []
        for i, row in enumerate(rows):
            for header in date_headers:
                value = row.get(header)
                if not _blank(value) and parse_date(value) is None:
                    unreadable.append(row_ref(i, row, header, value))
        checks.append(Check(
            key="unreadable_date",
            label="Date cells that cannot be read",
            count=len(unreadable),
            total=total,
            # Worse than a blank, which is at least visibly absent. "17/0/2026" reads as a
            # filled-in date to a human scanning the sheet and as nothing at all to every
            # calculation, so the row is silently dropped from exactly the reports it
            # looks like it belongs in.
            detail='A value like "17/0/2026" looks filled in but counts as missing everywhere.',
            samples=unreadable[:MAX_SAMPLES],
        ))
    else:
        skipped.append({
            "key": "unreadable_date",
            "label": "Date formats",
            "reason": "This tab's schema names no date columns.",
        })

    # --- ownership ------------------------------------------------------------------
    if people:
        unassigned = [
            row_ref(i, r)
            for i, r in enumerate(rows)
            if all(_blank(r.get(col["header"])) for col in people)
        ]
        role_names = ", ".join(col["label"] for col in people)
        checks.append(Check(
            key="unassigned",
            label="Rows with nobody named",
            count=len(unassigned),
            total=total,
            detail=f"Blank in every people column ({role_names}), so no workload includes them.",
            samples=unassigned[:MAX_SAMPLES],
        ))

        counts = count_observed_names(rows, people)
        if len(counts) <= MAX_NAMES_FOR_MERGE_SCAN:
            suggestions = suggest_merges(counts)
            checks.append(Check(
                key="duplicate_people",
                label="Names that may be the same person",
                count=len(suggestions),
                total=max(len(counts), 1),
                # The one check that is a prompt rather than a defect — the app will not
                # merge these on its own, and some of them are genuinely two people.
                detail="Reviewed and merged from the People screen; nothing is merged automatically.",
                samples=[
                    {"row": 0, "id": "", "column": s.name, "value": ", ".join(s.candidates[:3])}
                    for s in suggestions[:MAX_SAMPLES]
                ],
            ))
        else:
            skipped.append({
                "key": "duplicate_people",
                "label": "Near-duplicate names",
                "reason": f"{len(counts)} distinct names is too many to compare pairwise.",
            })
    else:
        skipped.append({
            "key": "unassigned",
            "label": "Ownership",
            "reason": "This tab's schema names no people columns.",
        })

    # --- status ---------------------------------------------------------------------
    if status_header:
        blank_status = [
            row_ref(i, r, status_header)
            for i, r in enumerate(rows)
            if _blank(r.get(status_header))
        ]
        checks.append(Check(
            key="no_status",
            label=f"Rows with no {status_header}",
            count=len(blank_status),
            total=total,
            detail='These rows are counted under "(blank)" in the status breakdown.',
            samples=blank_status[:MAX_SAMPLES],
        ))
    else:
        skipped.append({
            "key": "no_status",
            "label": "Status",
            "reason": "This tab's schema names no status column.",
        })

    # Worst first: a reader who only looks at the top of the panel should be looking at
    # the biggest problem, not at whichever check happens to be written first here.
    order = {"error": 0, "warning": 1, "info": 2, "ok": 3}
    ranked = sorted(checks, key=lambda c: (order[c.severity], -c.count, c.key))

    return {
        "total_rows": total,
        "checks": [c.as_dict() for c in ranked],
        "skipped": skipped,
        "completeness": _completeness(headers, rows, tab_schema),
    }


def _completeness(
    headers: List[str], rows: List[Dict[str, str]], tab_schema: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Fill rate over the columns this tab calls critical, or None if it names none.

    None rather than 100.0. `DataQualityChecker.completeness_score` returns 100.0 when it
    can resolve no critical column at all, which reports a sheet nobody has described as
    a perfect one. A score with nothing behind it is worse than no score.
    """
    declared = tab_schema.get("critical_fields") or []
    resolved = [h for h in (resolve_header(headers, f) for f in declared) if h]
    if not resolved or not rows:
        return None

    filled = sum(1 for row in rows for h in resolved if not _blank(row.get(h)))
    total_cells = len(rows) * len(resolved)
    return {
        "score": round(filled / total_cells * 100.0, 1),
        "fields": resolved,
        "filled": filled,
        "cells": total_cells,
    }

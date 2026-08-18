"""What each person is late on, and what is about to be late.

The reporting question this answers — "what is overdue this week, per person" — is the one
the whole app gets asked most, and until now it could only be answered by a human reading
the dashboard. This module computes the answer; nothing here sends it anywhere.

**No email is sent, scheduled, or configured by this code, and that is deliberate.** A
digest is outward-facing: it leaves the system and arrives in the inbox of someone who
never agreed to receive it, naming them and what they are late on. Getting the recipient
list or the wording wrong is not a bug you fix quietly in the next deploy — it has already
been read. So the computation ships and can be inspected through a preview endpoint
(§10.6), and the decision to mail it to anyone stays with a person.

It also depends on the people work being right. A digest that lists "Madiha Shah Bukhari"
and "Madiha" as two people with separate overdue lists is worse than no digest, because
mailing it makes a data-quality problem public and attributes real work to a person who
does not exist. Hence the `PersonResolver` throughout: the same alias map the dashboard
and the agent use.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.core.overdue import is_finished_status
from app.core.people import collect_assignments, normalise_person, parse_date
from app.core.schema import (
    bind_columns, get_due_column, get_people_columns, resolve_header,
)

# How far ahead "about to be late" reaches. A week, because that is the horizon the
# question is always asked over and the cadence any digest would run at — anything landing
# further out is not actionable yet and only makes the list long enough to stop being read.
DEFAULT_LOOKAHEAD_DAYS = 7


@dataclass(frozen=True)
class Item:
    """One row a person needs to do something about."""

    ricefw_id: str
    title: str
    due: str
    days: int
    status: str
    role: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ricefw_id": self.ricefw_id,
            "title": self.title,
            "due": self.due,
            # Negative means still to come; positive means this many days late. One number
            # rather than two fields, so a template can sort by urgency without branching.
            "days": self.days,
            "status": self.status,
            "role": self.role,
        }


def build_digest(
    headers: List[str],
    rows: List[Dict[str, str]],
    tab_schema: Dict[str, Any],
    resolver: Optional[Any] = None,
    today: Optional[date] = None,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
) -> Dict[str, Any]:
    """Group unfinished, dated work by the people responsible for it.

    Pure: no I/O, no database, no clock unless one is passed. `today` is a parameter so the
    result is reproducible — a digest that cannot be regenerated identically cannot be
    checked against what was sent.
    """
    today = today or date.today()
    horizon = today + timedelta(days=lookahead_days)

    due_header = resolve_header(headers, get_due_column(tab_schema, headers))
    status_header = resolve_header(headers, tab_schema.get("status_column"))
    id_header = resolve_header(headers, tab_schema.get("primary_id_column"))
    title_header = resolve_header(headers, tab_schema.get("description_column"))
    people = bind_columns(get_people_columns(tab_schema), headers)

    if not due_header:
        # Not an empty digest — an impossible one. A tab with no deadline column cannot
        # have anything overdue on it, and reporting "0 overdue" would read as good news.
        return _empty(today, lookahead_days, reason="This tab has no deadline column.")
    if not people:
        return _empty(today, lookahead_days, reason="This tab names no people columns.")

    by_person: Dict[str, Dict[str, Any]] = {}
    unassigned: Dict[str, List[Item]] = {"overdue": [], "due_soon": []}
    counted_rows = 0

    for row in rows:
        due = parse_date(row.get(due_header))
        if not due:
            continue
        status = str(row.get(status_header, "")).strip() if status_header else ""
        if is_finished_status(status):
            continue
        if due > horizon:
            continue

        counted_rows += 1
        bucket = "overdue" if due < today else "due_soon"
        base = {
            "ricefw_id": str(row.get(id_header, "")).strip() if id_header else "",
            "title": str(row.get(title_header, "")).strip() if title_header else "",
            "due": due.isoformat(),
            "days": (today - due).days,
            "status": status,
        }

        assignments = collect_assignments(row, people, resolver)
        if not assignments:
            # Surfaced rather than dropped. An overdue row nobody owns is the one most
            # likely to stay overdue, precisely because no digest would otherwise mention
            # it to anyone.
            unassigned[bucket].append(Item(**base, role=""))
            continue

        for assignment in assignments:
            entry = by_person.setdefault(assignment["person_key"], {
                "person": assignment["person"],
                "overdue": [],
                "due_soon": [],
            })
            entry[bucket].append(Item(**base, role=assignment["role_label"]))

    people_out = []
    for entry in by_person.values():
        overdue = sorted(entry["overdue"], key=lambda i: (-i.days, i.ricefw_id))
        due_soon = sorted(entry["due_soon"], key=lambda i: (i.days, i.ricefw_id))
        people_out.append({
            "person": entry["person"],
            "overdue_count": len(overdue),
            "due_soon_count": len(due_soon),
            "overdue": [i.as_dict() for i in overdue],
            "due_soon": [i.as_dict() for i in due_soon],
        })

    # Most overdue first: the digest's whole job is to put the worst thing at the top.
    people_out.sort(key=lambda p: (-p["overdue_count"], -p["due_soon_count"], p["person"].lower()))

    return {
        "generated_for": today.isoformat(),
        "window_days": lookahead_days,
        "reason": None,
        "people": people_out,
        "unassigned": {
            "overdue_count": len(unassigned["overdue"]),
            "due_soon_count": len(unassigned["due_soon"]),
            "overdue": [i.as_dict() for i in sorted(unassigned["overdue"], key=lambda i: -i.days)],
            "due_soon": [i.as_dict() for i in sorted(unassigned["due_soon"], key=lambda i: i.days)],
        },
        "totals": {
            "people": len(people_out),
            "rows": counted_rows,
            "overdue": sum(p["overdue_count"] for p in people_out) + len(unassigned["overdue"]),
            "due_soon": sum(p["due_soon_count"] for p in people_out) + len(unassigned["due_soon"]),
        },
    }


def _empty(today: date, lookahead_days: int, reason: str) -> Dict[str, Any]:
    """A digest that could not be computed, saying so rather than reporting zeroes."""
    return {
        "generated_for": today.isoformat(),
        "window_days": lookahead_days,
        "reason": reason,
        "people": [],
        "unassigned": {"overdue_count": 0, "due_soon_count": 0, "overdue": [], "due_soon": []},
        "totals": {"people": 0, "rows": 0, "overdue": 0, "due_soon": 0},
    }


def render_text(digest: Dict[str, Any], project_name: str, tab: str) -> str:
    """A plain-text rendering, for eyeballing what a digest would actually say.

    Text rather than HTML because the point of the preview is to read the *content* — who
    is named, what is attributed to them, how it is worded — and judge whether it is fit to
    send. Styling it would invite reviewing the wrong thing.
    """
    if digest.get("reason"):
        return f"{project_name} · {tab}\n\nNo digest: {digest['reason']}"

    lines = [
        f"{project_name} · {tab}",
        f"Overdue and due within {digest['window_days']} days, as of {digest['generated_for']}",
        "",
        f"{digest['totals']['overdue']} overdue, {digest['totals']['due_soon']} due soon, "
        f"across {digest['totals']['people']} people.",
        "",
    ]

    for person in digest["people"]:
        lines.append(f"{person['person']} — {person['overdue_count']} overdue, "
                     f"{person['due_soon_count']} due soon")
        for item in person["overdue"]:
            lines.append(f"    {item['days']}d late  {item['ricefw_id']}  {item['title'][:60]}")
        for item in person["due_soon"]:
            lines.append(f"    in {-item['days']}d    {item['ricefw_id']}  {item['title'][:60]}")
        lines.append("")

    if digest["unassigned"]["overdue_count"] or digest["unassigned"]["due_soon_count"]:
        lines.append(f"Nobody assigned — {digest['unassigned']['overdue_count']} overdue, "
                     f"{digest['unassigned']['due_soon_count']} due soon")
        for item in digest["unassigned"]["overdue"]:
            lines.append(f"    {item['days']}d late  {item['ricefw_id']}  {item['title'][:60]}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = ["DEFAULT_LOOKAHEAD_DAYS", "Item", "build_digest", "render_text", "normalise_person"]

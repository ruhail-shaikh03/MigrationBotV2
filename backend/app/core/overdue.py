"""What "overdue" means, defined once for every surface that reports it.

A row is overdue when its deadline has passed and its status does not read as finished.
The deadline column is resolved by `schema.py:get_due_column`; this module owns the
second half — deciding whether a status value means the work is done.

It exists because the two surfaces disagreed. `read.py:summarize` compared the status to
its exclusion list with `in` on a *set*, i.e. exact equality, while
`dashboard.py:_is_overdue` used substring matching against a different word list. On the
reference tracker the status is "Completed" and the exclusion list said "Complete", so
chat reported all 171 finished rows as overdue while the dashboard did not. Two answers
to one question, from one sheet, is worse than either answer alone.
"""

from typing import Iterable, Optional

# Generic completion words, matched as substrings so ordinary inflections ("Complete",
# "Completed", "Completion") and decorated values ("Dev Done", "Closed - Won't Fix") all
# read as finished. No sheet declares which of its statuses mean done, so this is a
# heuristic — and it is applied only to reports and filters, never to a write.
#
# The list is kept narrow deliberately. Hiding a genuinely overdue item is the worse
# error, because nobody chases what they cannot see, while a spurious extra row in an
# overdue list is self-correcting the moment a human reads it. "live" is therefore
# absent: it would clear "Go-Live Pending", which is precisely an unfinished state.
FINISHED_WORDS = ("done", "complete", "closed", "cancel", "deployed", "retired")


def is_finished_status(value: Optional[str], exclusions: Optional[Iterable[str]] = None) -> bool:
    """Whether this status value means the work is finished.

    `exclusions` overrides the defaults with the sheet's own wording — the
    `overdue_status_exclusions` tool argument. Matching stays substring-based either way,
    so a caller passing "Complete" still excludes a row reading "Completed"; requiring
    them to predict the exact inflection is how the original bug survived.
    """
    text = str(value or "").strip().casefold()
    if not text:
        return False
    words = [str(w).strip().casefold() for w in (exclusions or FINISHED_WORDS)]
    return any(word and word in text for word in words)

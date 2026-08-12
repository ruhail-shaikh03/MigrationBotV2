"use client"

import { Plus, X } from "lucide-react"

/**
 * Editor for a tab's people-columns and effort-columns.
 *
 * Detection is an LLM call over ten raw rows, so it will sometimes miss a resource
 * column or promote something that only looks like one ("Lead Time"). Those mistakes are
 * invisible until a dashboard renders the wrong people, and the only previous way to
 * correct them was hand-editing a raw JSON textarea validated by nothing but JSON.parse.
 *
 * `header` is never edited here — it is the verbatim sheet header used to address the
 * column, trailing spaces and typos included ("Functinal Resource "). Admins pick a
 * header from the ones the sheet actually has and may relabel it for display only.
 */

export interface RoleColumn {
  key?: string
  label: string
  header: string
  unit?: string
}

/** Mirrors backend core/people.py:slugify_role. The server re-derives keys on save
 *  (api/admin.py -> normalise_schema_config), so this is only for immediate UI feedback. */
function slugifyRole(label: string): string {
  return label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "")
}

interface Props {
  /** Verbatim headers this tab actually has, for the add-menu. */
  availableHeaders: string[]
  peopleColumns: RoleColumn[]
  effortColumns: RoleColumn[]
  onChange: (next: { people_columns: RoleColumn[]; effort_columns: RoleColumn[] }) => void
}

export default function RoleColumnsEditor({
  availableHeaders,
  peopleColumns,
  effortColumns,
  onChange,
}: Props) {
  const emit = (people: RoleColumn[], effort: RoleColumn[]) =>
    onChange({ people_columns: people, effort_columns: effort })

  const unusedHeaders = (used: RoleColumn[]) =>
    availableHeaders.filter((h) => !used.some((c) => c.header === h))

  const addColumn = (kind: "people" | "effort", header: string) => {
    if (!header) return
    const entry: RoleColumn = {
      key: slugifyRole(header),
      label: header.trim(),
      header,
      ...(kind === "effort" ? { unit: "days" } : {}),
    }
    if (kind === "people") emit([...peopleColumns, entry], effortColumns)
    else emit(peopleColumns, [...effortColumns, entry])
  }

  const removeColumn = (kind: "people" | "effort", header: string) => {
    if (kind === "people") emit(peopleColumns.filter((c) => c.header !== header), effortColumns)
    else emit(peopleColumns, effortColumns.filter((c) => c.header !== header))
  }

  const relabel = (kind: "people" | "effort", header: string, label: string) => {
    const apply = (cols: RoleColumn[]) =>
      cols.map((c) => (c.header === header ? { ...c, label, key: slugifyRole(label) } : c))
    if (kind === "people") emit(apply(peopleColumns), effortColumns)
    else emit(peopleColumns, apply(effortColumns))
  }

  const setUnit = (header: string, unit: string) => {
    emit(peopleColumns, effortColumns.map((c) => (c.header === header ? { ...c, unit } : c)))
  }

  const renderSection = (
    kind: "people" | "effort",
    title: string,
    hint: string,
    columns: RoleColumn[],
  ) => {
    const remaining = unusedHeaders(columns)
    return (
      <div className="space-y-2">
        <div>
          <span className="label-micro block">{title}</span>
          <span className="text-[11px] text-ink-500">{hint}</span>
        </div>

        {columns.length === 0 && (
          <p className="text-[11px] text-ink-500 italic">
            None detected. {kind === "effort"
              ? "Workload will be shown as item counts rather than days."
              : "Assignment views will be empty for this tab."}
          </p>
        )}

        <div className="space-y-2">
          {columns.map((col) => (
            <div
              key={col.header}
              className="flex items-center gap-2 bg-ink-950 border border-[var(--color-rule-strong)] rounded-lg px-2.5 py-2"
            >
              <input
                type="text"
                value={col.label}
                onChange={(e) => relabel(kind, col.header, e.target.value)}
                aria-label={`Display label for ${col.header}`}
                className="flex-1 min-w-0 bg-transparent text-sm text-ink-100 focus:outline-none"
              />
              <code
                className="shrink-0 text-[11px] text-brass-300 font-mono bg-ink-900 px-1.5 py-0.5 rounded border border-[var(--color-rule)]"
                title="Verbatim sheet header — whitespace and spelling preserved"
              >
                {col.header.replace(/ $/, "␣")}
              </code>
              {kind === "effort" && (
                <select
                  value={col.unit || "days"}
                  onChange={(e) => setUnit(col.header, e.target.value)}
                  aria-label={`Unit for ${col.header}`}
                  className="shrink-0 bg-ink-900 border border-[var(--color-rule)] rounded text-[11px] text-ink-300 px-1 py-0.5 cursor-pointer"
                >
                  <option value="days">days</option>
                  <option value="hours">hours</option>
                  <option value="points">points</option>
                </select>
              )}
              <button
                type="button"
                onClick={() => removeColumn(kind, col.header)}
                aria-label={`Remove ${col.label}`}
                className="shrink-0 text-ink-500 hover:text-failed transition"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>

        {remaining.length > 0 && (
          <div className="flex items-center gap-1.5">
            <Plus className="w-3 h-3 text-ink-500 shrink-0" />
            <select
              value=""
              onChange={(e) => addColumn(kind, e.target.value)}
              aria-label={`Add a ${kind === "people" ? "role" : "effort"} column`}
              className="flex-1 bg-ink-950 border border-[var(--color-rule)] rounded-lg px-2 py-1.5 text-xs text-ink-300 focus:outline-none focus:border-brass-500 cursor-pointer"
            >
              <option value="">Add a column…</option>
              {remaining.map((h) => (
                <option key={h} value={h}>{h.trim() || h}</option>
              ))}
            </select>
          </div>
        )}
      </div>
    )
  }

  if (availableHeaders.length === 0) {
    return (
      <p className="text-[11px] text-ink-500 italic">
        No headers known for this tab yet — run detection first.
      </p>
    )
  }

  return (
    <div className="space-y-4">
      {renderSection(
        "people",
        "Assigned Roles",
        "Every column naming someone responsible. The sheet's own wording is used as the display label.",
        peopleColumns,
      )}
      {renderSection(
        "effort",
        "Effort Columns",
        "Optional. Used for workload totals; without one, views fall back to item counts.",
        effortColumns,
      )}
    </div>
  )
}

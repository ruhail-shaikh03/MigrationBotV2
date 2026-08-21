"use client"

import { useState } from "react"

import { ChevronDown, ChevronRight, Pencil } from "lucide-react"

/**
 * Shared display primitives for anything that draws sheet data.
 *
 * Extracted from ToolResultCard when the project dashboard needed the same table, tiles
 * and chart frame. Duplicating them would have let the two surfaces drift into looking
 * like different products while showing the same numbers from the same sheet.
 *
 * SERIES_HUE stays blue deliberately: it is none of the four status colours and not the
 * brass interactive accent, so a bar can never be mistaken for a state. It was validated
 * against the previous darker ground (#030014); the current ground (#0a0e0d) is
 * marginally lighter, so contrast is essentially unchanged. Re-run the palette validator
 * before swapping it for a themed accent.
 */

export const SERIES_HUE = "#3987e5"
export const MAX_BARS = 8

export function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-[var(--color-rule-strong)] bg-white/[0.03] px-3 py-2.5">
      <div className="text-[11px] uppercase tracking-wider text-ink-500">{label}</div>
      <div className="mt-0.5 text-xl font-semibold text-ink-100 tabular-nums">{value}</div>
      {sub && <div className="text-[11px] text-ink-500">{sub}</div>}
    </div>
  )
}

export function ChartFrame({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: string
  children: React.ReactNode
}) {
  return (
    <div className="rounded-xl border border-[var(--color-rule-strong)] bg-ink-850 p-4">
      <div className="mb-3">
        <div className="text-[13px] font-semibold text-ink-200">{title}</div>
        {subtitle && <div className="text-[11px] text-ink-500">{subtitle}</div>}
      </div>
      {children}
    </div>
  )
}

/** Shared tooltip. An HTML chart is interactive by default; hover is not optional.
 *  `unit` exists because the dashboard plots days as well as row counts, and a bar
 *  labelled "12 rows" when it means twelve days is a wrong statement, not a cosmetic one. */
export function HoverTip({
  active,
  payload,
  label,
  unit,
}: {
  active?: boolean
  payload?: { value?: number }[]
  label?: string
  unit?: string
}) {
  if (!active || !payload?.length) return null
  const value = payload[0].value
  const noun = unit || (value === 1 ? "row" : "rows")
  return (
    <div className="rounded-lg border border-white/15 bg-[#0b0726] px-3 py-2 text-xs shadow-xl">
      <div className="font-medium text-ink-200">{label}</div>
      <div className="text-ink-400 tabular-nums">
        {value} {noun}
      </div>
    </div>
  )
}

export function DataTable({
  rows,
  caption,
  columns: explicitColumns,
  renderCell,
  maxHeight,
  clampCells,
}: {
  rows: Record<string, string | number>[]
  caption?: string
  /** Render exactly these columns in this order. The dashboard passes the sheet's own
   *  column order; without it the union below sorts by first appearance, which scrambles
   *  a grid the user expects to mirror their spreadsheet. */
  columns?: string[]
  /** Replace a cell's contents. The dashboard uses this to make people-columns editable
   *  without forking the table: a second table implementation would drift from this one
   *  in styling and in the ragged-row handling above. Return undefined to fall through to
   *  the default text rendering. */
  renderCell?: (column: string, value: string, rowIndex: number) => React.ReactNode | undefined
  /** Cap the table's height and scroll inside it, keeping the header row pinned. */
  maxHeight?: number | string
  /** Clamp long cell text to two lines. Free-text description columns otherwise wrap to
   *  five or six lines, which drops a 400-row grid to four visible rows. */
  clampCells?: boolean
}) {
  if (!rows.length) return <div className="text-xs text-ink-500">No rows.</div>
  // Union of keys across rows, not just the first: search_rows omits columns that were
  // blank for a given row, so a first-row-only header would silently drop fields.
  const columns =
    explicitColumns ??
    Array.from(
      rows.reduce<Set<string>>((set, r) => {
        Object.keys(r).forEach((k) => set.add(k))
        return set
      }, new Set<string>())
    )

  return (
    <div>
      {caption && <div className="mb-2 text-[11px] text-ink-500">{caption}</div>}
      {/* maxHeight + sticky header: a 400-row tracker scrolled past its own column names
          within one screen, leaving the reader guessing which column they were in. */}
      <div
        className="overflow-auto rounded-lg border border-[var(--color-rule-strong)]"
        style={maxHeight ? { maxHeight } : undefined}
      >
        <table className="w-full border-collapse text-[12.5px]">
          <thead className="bg-ink-800">
            <tr>
              {columns.map((c) => (
                <th
                  key={c}
                  className="sticky top-0 z-10 whitespace-nowrap border-b border-[var(--color-rule-strong)] bg-ink-800 px-3 py-2 text-left font-semibold text-ink-200"
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="transition-colors hover:bg-ink-850">
                {columns.map((c) => {
                  const value = String(r[c] ?? "")
                  const custom = renderCell?.(c, value, i)
                  return (
                    <td
                      key={c}
                      className="border-b border-[var(--color-rule)] px-3 py-2 align-top text-ink-300"
                    >
                      {custom !== undefined ? (
                        custom
                      ) : clampCells ? (
                        // Full text stays reachable on hover rather than being lost.
                        <span className="line-clamp-2 max-w-[26ch]" title={value}>
                          {value}
                        </span>
                      ) : (
                        value
                      )}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/**
 * One editable sheet cell, with its own queued/failed state.
 *
 * Extracted from the dashboard grid's `renderCell` closure so the timeline's row dialog
 * shows the identical control rather than a second implementation of it. The dotted
 * underline is the affordance and is not decorative: without it an editable cell is
 * visually identical to a read-only one, so nobody discovers the feature.
 */
export function EditableCell({
  label,
  value,
  edit,
  isEditing,
  onBeginEdit,
  onCancel,
  onSave,
  placeholder = "Unassigned",
  title = `Edit ${label}`,
}: {
  /** The column's display label, used in the accessible name and in the default title. */
  label: string
  value: string
  edit?: { value: string; state: "queued" | "failed"; error?: string }
  isEditing: boolean
  onBeginEdit: () => void
  onCancel: () => void
  onSave: (next: string) => void
  placeholder?: string
  /** Hover title for the read state. Defaults to `Edit {label}`; the dashboard grid passes
   *  "Reassign {label}" because it only makes people-columns editable, where that is the
   *  verb the user is looking for. A failed edit's error still takes precedence over it. */
  title?: string
}) {
  if (isEditing) {
    return (
      <input
        autoFocus
        // Seeded from the pending edit rather than from `value`, which is what the closure
        // this was extracted from used. The read state below already renders `edit.value`,
        // so opening the input on the stale sheet value would replace what the cell was
        // displaying a moment earlier with something else. A deliberate, reviewed
        // divergence from the pre-extraction behaviour; the rest of the extraction is a
        // no-op.
        defaultValue={edit ? edit.value : value}
        aria-label={label}
        onBlur={(e) => onSave(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur()
          if (e.key === "Escape") onCancel()
        }}
        className="w-full rounded border border-brass-500 bg-ink-950 px-1.5 py-0.5 text-[12.5px] text-ink-100 focus:outline-none"
      />
    )
  }

  return (
    <button
      onClick={onBeginEdit}
      title={edit?.error || title}
      className="group flex w-full cursor-pointer items-center gap-1 text-left"
    >
      <span
        className={`decoration-dotted underline-offset-4 group-hover:text-brass-300 group-hover:underline ${
          edit?.state === "failed" ? "text-failed" : "underline decoration-ink-600"
        }`}
      >
        {edit ? edit.value : value || <span className="text-ink-600">{placeholder}</span>}
      </span>
      <Pencil className="h-3 w-3 shrink-0 text-ink-600 opacity-0 transition group-hover:opacity-100" />
      {edit?.state === "queued" && <span className="status status-queued ml-1">queued</span>}
      {edit?.state === "failed" && <span className="status status-failed ml-1">failed</span>}
    </button>
  )
}

export interface TimelineItem {
  id: string
  label: string
  row_number?: number
  kind: "bar" | "milestone"
  start: string | null
  end: string | null
  milestone_of: "start" | "due" | null
  reversed: boolean
  status: string
  overdue: boolean
  people: { key: string; label: string; header: string; value: string }[]
  values: Record<string, string>
}

/** What identifies a timeline row across two reads of the same tab.
 *
 *  `TimelineChart` lists its rows under this key, and every consumer looks a row up by it
 *  — the overdue count, and the dialog re-resolving the open row out of a fresh payload.
 *  It lived in project/[id]/page.tsx and was spelled a second time inline here, the two
 *  kept in step by a comment rather than by structure; one written form is what stops a
 *  re-read reconnecting the dialog to the wrong row. It lives in this file rather than in
 *  the page because a shared component must not import from a route.
 *
 *  `row_number` is always present — api/timeline.py builds its rows with `data_start_row`
 *  precisely so a bar can address its own sheet row — so this is effectively the row
 *  number. The label is a fallback for a payload that somehow lacks one, and the ID alone
 *  is never enough, because 27 of 412 rows on the reference tracker share one. */
export function itemKey(item: TimelineItem): string {
  return `${item.id}-${item.row_number ?? item.label}`
}

export interface TimelineGroup {
  label: string
  count: number
  start: string | null
  end: string | null
  items: TimelineItem[]
  collapsed_groups?: number
}

const DAY_MS = 86_400_000
const LABEL_COL = 260

function toUTC(iso: string): number {
  return Date.parse(`${iso}T00:00:00Z`)
}

/** Ticks whose spacing suits the span, rather than a zoom control nobody asked for.
 *  The step is picked from the PADDED span, and the thresholds below read: up to ~45 days
 *  weeks, to ~200 days months, to ~800 days quarters, beyond that years. So a quarter-long
 *  plan gets weeks, a year-long one quarters, and a three-year one years — quarters serve
 *  roughly seven months to 2.2 years, not three-year spans.
 *
 *  Returns absolute timestamps rather than percentages, so the caller positions them with
 *  the same `pct` the bars use. Computing tick positions against their own span is how an
 *  axis ends up drifting a few pixels off the bars it is supposed to label. */
function axisTicks(startMs: number, endMs: number): { ms: number; label: string }[] {
  const days = Math.max(1, Math.round((endMs - startMs) / DAY_MS))
  const step = days <= 45 ? 7 : days <= 200 ? 30 : days <= 800 ? 91 : 365
  const fmt: Intl.DateTimeFormatOptions =
    step <= 7
      ? { day: "2-digit", month: "short" }
      // 91, not 30: this middle format serves BOTH the monthly and the quarterly step.
      // Matching the boundary to the step thresholds above would send the 91-day step to
      // year-only, drawing a one-year plan as 2025 / 2026 / 2026 / 2026 / 2026 -- five
      // ticks, four of them identical, on exactly the span quarterly ticks were chosen for.
      : step <= 91
        ? { month: "short", year: "2-digit" }
        : { year: "numeric" }

  const ticks: { ms: number; label: string }[] = []
  for (let t = startMs; t <= endMs; t += step * DAY_MS) {
    ticks.push({
      ms: t,
      label: new Date(t).toLocaleDateString(undefined, { ...fmt, timeZone: "UTC" }),
    })
  }
  return ticks
}

/**
 * A Gantt-style timeline.
 *
 * Hand-rolled rather than built on Recharts, which has no Gantt primitive — the shapes
 * commonly bolted onto its BarChart to imitate one break as soon as rows are grouped.
 *
 * One hue for every bar. The source template colours each phase differently; that is
 * rejected here because the group header and indentation already encode the grouping, and
 * because the four status colours fail all-pairs CVD separation as a set, which is why
 * they are reserved for icon-plus-label pairings. Overdue is therefore an outline plus the
 * bar's `title`, never a fill: colour alone would reintroduce exactly that problem.
 *
 * That `title` is a weak second channel — invisible to keyboard and touch users, for whom
 * overdue does degrade to outline colour alone. Carrying overdue as text belongs on the
 * coverage line and the row dialog, which Task 7 owns; it is not solved here.
 */
export function TimelineChart({
  groups,
  rangeStart,
  rangeEnd,
  today,
  onSelectItem,
  maxHeight,
}: {
  groups: TimelineGroup[]
  rangeStart: string
  rangeEnd: string
  today: string
  onSelectItem?: (item: TimelineItem) => void
  /** Cap the chart's height and scroll inside it, keeping the axis pinned — the same prop
   *  DataTable takes, for the same reason. `sticky` resolves against the nearest scrollport,
   *  so without a height cap this container's scrollport equals its content and the axis has
   *  nothing to stick within: it scrolls away with the page on a long tracker. Omitting it is
   *  therefore legal but leaves the axis unpinned. */
  maxHeight?: number | string
}) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})

  // A single-day span would divide by zero and a one-day plan would render as one pixel,
  // so the axis is padded to a minimum width either way.
  const rawStart = toUTC(rangeStart)
  const rawEnd = toUTC(rangeEnd)
  const pad = Math.max(DAY_MS, (rawEnd - rawStart) * 0.02)
  const startMs = rawStart - pad
  const endMs = rawEnd + pad
  const span = Math.max(1, endMs - startMs)

  const pctFromMs = (ms: number) => ((ms - startMs) / span) * 100
  const pct = (iso: string) => pctFromMs(toUTC(iso))
  const ticks = axisTicks(startMs, endMs)
  const todayPct = pct(today)

  return (
    <div className="rounded-xl border border-[var(--color-rule-strong)] bg-ink-850">
      {/* The time region scrolls inside its own container; the page body never scrolls
          sideways, the rule already applied to markdown tables. maxHeight makes this the
          vertical scrollport too, which is what lets the axis above actually stick. */}
      <div className="overflow-auto" style={maxHeight ? { maxHeight } : undefined}>
        <div className="min-w-[720px]">
          {/* Axis */}
          <div className="sticky top-0 z-20 flex border-b border-[var(--color-rule-strong)] bg-ink-850">
            <div className="shrink-0 px-3 py-2" style={{ width: LABEL_COL }}>
              <span className="label-micro">Task</span>
            </div>
            <div className="relative flex-1 py-2">
              {ticks.map((t, i) => (
                <span
                  key={t.ms}
                  // The end labels are anchored rather than centred. Tick 0 always sits at
                  // exactly 0% (the loop seeds at startMs), so centring it would push half the
                  // label back over the label column; and when the padded span is a whole
                  // multiple of the step the last tick lands on exactly 100%, where its right
                  // half is clipped by the scroll container. Only the interior ticks centre.
                  className={`absolute whitespace-nowrap font-mono text-[10px] text-ink-500 ${
                    i === 0
                      ? "translate-x-0"
                      : i === ticks.length - 1
                        ? "-translate-x-full"
                        : "-translate-x-1/2"
                  }`}
                  style={{ left: `${pctFromMs(t.ms)}%` }}
                >
                  {t.label}
                </span>
              ))}
            </div>
          </div>

          <div className="relative">
            {/* Today marker, spanning every row. z-10 paints it in FRONT of the bars, which
                are z-index:auto in this stacking context — deliberate, and the comment used to
                claim the opposite. A 1px 60%-alpha rule reads better crossing a bar than hidden
                under one, and pointer-events-none keeps it from stealing a row click. */}
            {todayPct >= 0 && todayPct <= 100 && (
              // Two nested elements, not one calc(): the marker must be a percentage of the
              // TIME region, and `calc(260px + 40% * ...)` cannot express that — mixing % and
              // px that way is invalid CSS and silently drops the whole declaration.
              <div
                className="pointer-events-none absolute inset-y-0 z-10"
                style={{ left: LABEL_COL, right: 0 }}
                aria-hidden
              >
                <div className="absolute inset-y-0 w-px bg-brass-400/60" style={{ left: `${todayPct}%` }} />
              </div>
            )}

            {groups.map((group) => {
              const isCollapsed = collapsed[group.label]
              return (
                <div key={group.label} className="border-b border-[var(--color-rule)] last:border-b-0">
                  {/* Group header — the rollup, derived per read and never written back. */}
                  <button
                    onClick={() => setCollapsed((p) => ({ ...p, [group.label]: !p[group.label] }))}
                    // The chevron swap is the only visual state; a screen reader cannot see it.
                    aria-expanded={!isCollapsed}
                    className="flex w-full items-center hover:bg-ink-800/60"
                  >
                    <div
                      className="flex shrink-0 items-center gap-1.5 px-3 py-2 text-left"
                      style={{ width: LABEL_COL }}
                    >
                      {isCollapsed ? (
                        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-ink-500" />
                      ) : (
                        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-ink-500" />
                      )}
                      <span className="truncate text-[12.5px] font-semibold text-ink-200" title={group.label}>
                        {group.label}
                      </span>
                      <span className="shrink-0 font-mono text-[10px] text-ink-500">{group.count}</span>
                    </div>
                    <div className="relative h-8 flex-1">
                      {group.start && group.end && (
                        <div
                          className="absolute top-1/2 h-2.5 -translate-y-1/2 rounded-sm"
                          style={{
                            left: `${pct(group.start)}%`,
                            width: `${Math.max(pct(group.end) - pct(group.start), 0.4)}%`,
                            backgroundColor: SERIES_HUE,
                            opacity: 0.5,
                          }}
                        />
                      )}
                    </div>
                  </button>

                  {!isCollapsed &&
                    group.items.map((item) => {
                      const left = item.start ? pct(item.start) : 0
                      const width = item.start && item.end ? Math.max(pct(item.end) - left, 0.4) : 0
                      return (
                        <button
                          key={itemKey(item)}
                          onClick={() => onSelectItem?.(item)}
                          className="flex w-full items-center text-left hover:bg-ink-800/60"
                        >
                          <div className="shrink-0 truncate py-1.5 pl-8 pr-3" style={{ width: LABEL_COL }}>
                            <span className="text-[12px] text-ink-300" title={item.label}>
                              {item.label}
                            </span>
                          </div>
                          <div className="relative h-7 flex-1">
                            {item.kind === "bar" ? (
                              <div
                                className="absolute top-1/2 h-3.5 -translate-y-1/2 rounded-sm"
                                style={{
                                  left: `${left}%`,
                                  width: `${width}%`,
                                  backgroundColor: SERIES_HUE,
                                  outline: item.overdue ? "1px solid var(--color-failed)" : undefined,
                                  outlineOffset: item.overdue ? "1px" : undefined,
                                }}
                                title={`${item.id} · ${item.start} → ${item.end}${
                                  item.overdue ? " · overdue" : ""
                                }${item.reversed ? " · dates are reversed on the sheet" : ""}`}
                              />
                            ) : (
                              <div
                                className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rotate-45"
                                style={{
                                  left: `${left}%`,
                                  backgroundColor: SERIES_HUE,
                                  outline: item.overdue ? "1px solid var(--color-failed)" : undefined,
                                  outlineOffset: item.overdue ? "1px" : undefined,
                                }}
                                title={`${item.id} · ${
                                  item.milestone_of === "start" ? "starts" : "due"
                                } ${item.start}${item.overdue ? " · overdue" : ""}`}
                              />
                            )}
                          </div>
                        </button>
                      )
                    })}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}

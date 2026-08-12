"use client"

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

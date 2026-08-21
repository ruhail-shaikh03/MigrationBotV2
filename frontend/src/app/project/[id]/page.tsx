"use client"

import { useSession } from "next-auth/react"
import { useParams, useRouter } from "next/navigation"
import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Bar, BarChart, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts"
import {
  AlertTriangle, ArrowLeft, Download, Grid3x3, RefreshCw, Search, Stethoscope, Users,
} from "lucide-react"

import {
  ChartFrame, DataTable, EditableCell, HoverTip, MAX_BARS, SERIES_HUE, StatTile,
} from "@/components/DataDisplay"
import { useRowEdits } from "@/hooks/useRowEdits"

/**
 * The project dashboard: the sheet itself, rather than answers about it.
 *
 * Chat could already report that eleven items are overdue; it could not show which, or
 * who holds them. Non-admins previously had a text box and nothing else — /admin was the
 * only other surface and it is gated.
 *
 * Everything about which columns exist, what they are called and which of them name
 * people comes from the API (dashboard.py:_column_descriptors). Nothing here knows the
 * word "technical" or "functional": a sheet with a QA Owner renders a QA Owner column.
 */

interface ColumnDescriptor {
  header: string
  label: string
  key: string | null
  kind: "person" | "effort" | "plain"
  /** False when the column is empty in every scanned row. Real trackers carry a long
   *  tail of untouched columns; showing them pushes the useful ones off-screen. */
  has_data: boolean
}

interface RoleColumn {
  key: string
  label: string
  header: string
}

interface RowsResponse {
  tab: string
  /** Every tab in the project. A multi-tab tracker was previously stuck on its default
   *  tab here: the endpoint has always accepted `?tab=`, but nothing told the client
   *  which other tabs existed, so nothing ever sent it. */
  tabs: string[]
  project_name: string
  columns: ColumnDescriptor[]
  people_columns: RoleColumn[]
  primary_id_column: string | null
  /* Cell values are strings, keyed by the sheet's own headers. `__row_number__` is the
   * one exception — the server attaches the sheet row each dict came from, so an inline
   * edit can name a specific row when the tracker's IDs are not unique. */
  rows: (Record<string, string> & { __row_number__?: number })[]
  total: number
  offset: number
  limit: number
  truncated: boolean
}

interface WorkloadEntry {
  person: string
  total_items: number
  overdue_items: number
  total_days: number
  roles: Record<string, { label: string; items: number }>
}

interface AnalyticsResponse {
  tab: string
  total_rows: number
  by_status: { label: string; count: number }[]
  workload: WorkloadEntry[]
  people_columns: RoleColumn[]
  days_source: "column" | "dates" | "mixed" | null
  /** Rows that actually produced a day figure — the denominator for days_source. */
  effort_rows: number
  has_due_dates: boolean
  truncated: boolean
}

interface HealthCheck {
  key: string
  label: string
  count: number
  total: number
  severity: "error" | "warning" | "info" | "ok"
  detail: string
  samples: { row: number; id: string; column: string; value: string }[]
}

interface HealthResponse {
  tab: string
  total_rows: number
  checks: HealthCheck[]
  /** Checks that could not run, and why. Never folded into `checks` as a zero: a check
   *  the schema does not support has not passed, and showing it as green is the one
   *  failure mode this panel exists to avoid. */
  skipped: { key: string; label: string; reason: string }[]
  completeness: { score: number; fields: string[]; filled: number; cells: number } | null
  truncated: boolean
}

const PAGE_SIZE = 50

export default function ProjectDashboard() {
  const { data: session, status: authStatus } = useSession()
  const router = useRouter()
  const params = useParams()
  const projectId = params?.id as string

  const [view, setView] = useState<"grid" | "workload" | "health">("grid")
  const [rowsData, setRowsData] = useState<RowsResponse | null>(null)
  const [analytics, setAnalytics] = useState<AnalyticsResponse | null>(null)
  const [health, setHealth] = useState<{ key: string; data: HealthResponse | null } | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [errorMsg, setErrorMsg] = useState("")

  const [showEmptyColumns, setShowEmptyColumns] = useState(false)
  const [exporting, setExporting] = useState(false)

  // "" means "whatever the project's default tab is" — the server decides, so the page
  // renders correctly on first paint without first fetching the tab list.
  const [tab, setTab] = useState("")

  // Filters
  const [q, setQ] = useState("")
  const [person, setPerson] = useState("")
  const [roleKey, setRoleKey] = useState("")
  const [overdueOnly, setOverdueOnly] = useState(false)
  const [offset, setOffset] = useState(0)

  const apiToken = session?.apiToken || ""
  const googleToken = session?.googleAccessToken || ""

  useEffect(() => {
    if (authStatus === "unauthenticated") router.push("/")
  }, [authStatus, router])

  const authHeaders = useMemo(
    () => ({
      Authorization: `Bearer ${apiToken}`,
      "X-Google-Access-Token": googleToken,
    }),
    [apiToken, googleToken]
  )

  const load = useCallback(async () => {
    if (!apiToken || !projectId) return
    setIsLoading(true)
    setErrorMsg("")
    try {
      const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) })
      if (tab) query.set("tab", tab)
      if (q.trim()) query.set("q", q.trim())
      if (person) query.set("person", person)
      if (roleKey) query.set("role_key", roleKey)
      if (overdueOnly) query.set("overdue", "true")

      // Analytics takes the tab too, or the workload panel would describe a different
      // tab than the grid beside it.
      const analyticsQuery = tab ? `?tab=${encodeURIComponent(tab)}` : ""

      const [rowsRes, analyticsRes] = await Promise.all([
        fetch(`/api/projects/${projectId}/rows?${query}`, { headers: authHeaders }),
        fetch(`/api/projects/${projectId}/analytics${analyticsQuery}`, { headers: authHeaders }),
      ])

      if (!rowsRes.ok) {
        const err = await rowsRes.json().catch(() => ({}))
        throw new Error(err.detail || `Could not load rows (${rowsRes.status}).`)
      }
      setRowsData(await rowsRes.json())
      // Analytics failing must not blank the grid — the grid is the primary view and is
      // useful on its own, so a failed panel degrades to "no data" instead of an error page.
      setAnalytics(analyticsRes.ok ? await analyticsRes.json() : null)
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to load this project.")
      setRowsData(null)
    } finally {
      setIsLoading(false)
    }
  }, [apiToken, projectId, tab, offset, q, person, roleKey, overdueOnly, authHeaders])

  const { pending, editing, setEditing, saveCell } = useRowEdits({
    projectId,
    tab: rowsData?.tab,
    authHeaders,
    apiToken,
    onApplied: load,
  })

  /**
   * Download the filtered tab.
   *
   * Fetched rather than linked, because the endpoint needs the bearer token and the
   * Google token — a bare <a href> sends neither and would download an HTML 401. The blob
   * round-trip is the cost of that.
   *
   * The query mirrors the grid's exactly, minus limit/offset: the file is the whole
   * filtered set, not the visible page. Exporting the page would produce a file that
   * disagrees with the row count printed directly above the button.
   */
  const exportCsv = useCallback(async () => {
    if (!apiToken || !projectId) return
    setExporting(true)
    try {
      const query = new URLSearchParams()
      if (tab) query.set("tab", tab)
      if (q.trim()) query.set("q", q.trim())
      if (person) query.set("person", person)
      if (roleKey) query.set("role_key", roleKey)
      if (overdueOnly) query.set("overdue", "true")
      if (showEmptyColumns) query.set("include_empty", "true")

      const res = await fetch(`/api/projects/${projectId}/rows.csv?${query}`, { headers: authHeaders })
      if (!res.ok) throw new Error(`Export failed (${res.status}).`)

      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url
      // The server names the file (project, tab and date); this is only the fallback for
      // the case where the header did not survive a proxy.
      link.download =
        res.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] || "rows.csv"
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Could not export these rows.")
    } finally {
      setExporting(false)
    }
  }, [apiToken, projectId, tab, q, person, roleKey, overdueOnly, showEmptyColumns, authHeaders])

  useEffect(() => {
    load()
  }, [load])

  // Health is fetched only when its panel is open, and only on tab change — not on every
  // filter keystroke like the grid. It deliberately reports the whole tab: "12 rows have
  // no deadline" is a fact about the sheet, and re-scoping it to the current search would
  // turn it into a fact about a search, which is not a thing anyone can act on.
  useEffect(() => {
    if (view !== "health" || !apiToken || !projectId) return
    let cancelled = false
    // Nothing is set before the first await, so the effect never triggers a synchronous
    // re-render — the same shape /admin/people uses.
    const run = async () => {
      const query = tab ? `?tab=${encodeURIComponent(tab)}` : ""
      try {
        const res = await fetch(`/api/projects/${projectId}/health${query}`, { headers: authHeaders })
        const body = res.ok ? await res.json() : null
        if (!cancelled) setHealth({ key: tab, data: body })
      } catch {
        if (!cancelled) setHealth({ key: tab, data: null })
      }
    }
    run()
    return () => { cancelled = true }
  }, [view, tab, apiToken, projectId, authHeaders])

  // Switching tab invalidates the people- and role-filters with it: the names and role
  // keys on one tab generally do not exist on another, so carrying them across would
  // land the user on an empty grid that looks like the tab has no rows.
  const changeTab = useCallback((next: string) => {
    setTab(next)
    setOffset(0)
    setPerson("")
    setRoleKey("")
  }, [])

  const tabs = rowsData?.tabs || []
  const peopleColumns = rowsData?.people_columns || analytics?.people_columns || []
  const everyone = useMemo(() => {
    const names = new Set<string>()
    analytics?.workload.forEach((w) => names.add(w.person))
    return Array.from(names).sort((a, b) => a.localeCompare(b))
  }, [analytics])

  // Columns that are empty in every row of the tab are hidden by default. The reference
  // sheet ends in a dozen untouched columns literally named "Column 15"…"Column 26";
  // rendering them buries the columns a PM came for behind a horizontal scrollbar.
  const emptyColumnCount = useMemo(
    () => (rowsData?.columns || []).filter((c) => !c.has_data).length,
    [rowsData]
  )

  const gridColumns = useMemo(
    () =>
      (rowsData?.columns || [])
        .filter((c) => showEmptyColumns || c.has_data)
        .map((c) => c.header),
    [rowsData, showEmptyColumns]
  )
  const columnLabels = useMemo(() => {
    const map: Record<string, string> = {}
    ;(rowsData?.columns || []).forEach((c) => {
      map[c.header] = c.label
    })
    return map
  }, [rowsData])

  // The table is keyed by display label, but a write must address the verbatim header
  // (trailing spaces and all), so the renderer needs the reverse map.
  const headerByLabel = useMemo(() => {
    const map: Record<string, string> = {}
    ;(rowsData?.columns || []).forEach((c) => {
      map[c.label || c.header] = c.header
    })
    return map
  }, [rowsData])

  const personHeaders = useMemo(
    () => new Set((rowsData?.columns || []).filter((c) => c.kind === "person").map((c) => c.header)),
    [rowsData]
  )

  // Relabel each row's keys to the sheet's display labels so DataTable renders the
  // sheet's own wording without needing to know about column descriptors.
  const labelledRows = useMemo(
    () =>
      (rowsData?.rows || []).map((row) => {
        const out: Record<string, string> = {}
        gridColumns.forEach((h) => {
          out[columnLabels[h] || h] = row[h] ?? ""
        })
        return out
      }),
    [rowsData, gridColumns, columnLabels]
  )

  const total = rowsData?.total ?? 0
  const showingTo = Math.min(offset + PAGE_SIZE, total)

  if (authStatus === "loading") {
    return <div className="p-8 text-sm text-ink-500">Loading…</div>
  }

  return (
    <div className="min-h-screen bg-ink-950 text-ink-100">
      <header className="border-b border-[var(--color-rule)] px-6 py-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <button
              onClick={() => router.push("/chat")}
              className="btn btn-ghost"
              aria-label="Back to chat"
            >
              <ArrowLeft className="h-4 w-4" />
              Chat
            </button>
            <div>
              {/* The sheet's own name, not a generic page label — a PM working three
                  trackers needs to know which one is on screen. */}
              <h1 className="display-md">{rowsData?.project_name || "Project"}</h1>
              {rowsData && (
                <p className="text-[11px] text-ink-500">
                  {rowsData.tab} · {total} {total === 1 ? "row" : "rows"}
                  {rowsData.truncated && " · partial (row ceiling reached)"}
                </p>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2">
            <div className="flex rounded-lg border border-[var(--color-rule-strong)] p-0.5">
              <button
                onClick={() => setView("grid")}
                className={`inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-xs transition cursor-pointer ${
                  view === "grid" ? "bg-brass-400/15 text-brass-300" : "text-ink-400 hover:text-ink-200"
                }`}
              >
                <Grid3x3 className="h-3.5 w-3.5" /> Grid
              </button>
              <button
                onClick={() => setView("workload")}
                className={`inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-xs transition cursor-pointer ${
                  view === "workload" ? "bg-brass-400/15 text-brass-300" : "text-ink-400 hover:text-ink-200"
                }`}
              >
                <Users className="h-3.5 w-3.5" /> Workload
              </button>
              <button
                onClick={() => setView("health")}
                className={`inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-xs transition cursor-pointer ${
                  view === "health" ? "bg-brass-400/15 text-brass-300" : "text-ink-400 hover:text-ink-200"
                }`}
              >
                <Stethoscope className="h-3.5 w-3.5" /> Health
              </button>
            </div>
            {/* Hidden on the health view, whose numbers are about the whole tab rather
                than the filtered selection this would export. */}
            {view !== "health" && (
              <button
                onClick={exportCsv}
                disabled={exporting}
                className="btn btn-ghost disabled:opacity-40"
                aria-label="Export these rows as CSV"
                title="Download the filtered rows as CSV"
              >
                <Download className="h-4 w-4" />
              </button>
            )}
            <button onClick={() => load()} className="btn btn-ghost" aria-label="Refresh">
              <RefreshCw className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`} />
            </button>
          </div>
        </div>

        {/* Tab switcher, styled to match the one in the chat header so the same sheet
            reads the same way on both surfaces. Scrolls sideways rather than wrapping —
            a 12-tab tracker would otherwise push the grid down a whole row. */}
        {tabs.length > 1 && (
          <div className="mt-3 flex max-w-full items-center gap-1 overflow-x-auto rounded-md border border-[var(--color-rule)] bg-ink-950 p-1">
            {tabs.map((name) => {
              const isActive = (rowsData?.tab || tab) === name
              return (
                <button
                  key={name}
                  onClick={() => changeTab(name)}
                  aria-current={isActive ? "true" : undefined}
                  className={`shrink-0 cursor-pointer rounded-sm px-3 py-1 font-mono text-[12px] font-medium transition ${
                    isActive
                      ? "bg-brass-400 text-ink-950"
                      : "text-ink-400 hover:bg-ink-800 hover:text-ink-200"
                  }`}
                >
                  {name}
                </button>
              )
            })}
          </div>
        )}
      </header>

      <main className="mx-auto max-w-[1400px] space-y-5 p-6">
        {errorMsg && (
          <div className="flex items-start gap-2 rounded-xl border border-[color-mix(in_srgb,var(--color-failed)_35%,transparent)] bg-[color-mix(in_srgb,var(--color-failed)_10%,transparent)] px-4 py-3 text-sm text-failed">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{errorMsg}</span>
          </div>
        )}

        {/* Filters apply to the grid and the workload chart: a chart that ignored the
            active filter would contradict the grid beside it. Health is the exception and
            hides them, because its counts are about the tab rather than about a selection
            — leaving the controls visible would imply they narrow numbers that they do
            not. */}
        <div className={`flex-wrap items-end gap-3 ${view === "health" ? "hidden" : "flex"}`}>
          <div className="min-w-[220px] flex-1 space-y-1.5">
            <label className="label-micro" htmlFor="dash-search">Search</label>
            <div className="relative">
              <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-500" />
              <input
                id="dash-search"
                value={q}
                onChange={(e) => { setQ(e.target.value); setOffset(0) }}
                placeholder="any cell…"
                className="w-full rounded-xl border border-[var(--color-rule-strong)] bg-ink-950 py-2.5 pl-9 pr-4 text-sm text-ink-100 placeholder-ink-500 focus:border-brass-500 focus:outline-none"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="label-micro" htmlFor="dash-person">Assigned to</label>
            <select
              id="dash-person"
              value={person}
              onChange={(e) => { setPerson(e.target.value); setOffset(0) }}
              className="min-w-[160px] cursor-pointer rounded-xl border border-[var(--color-rule-strong)] bg-ink-950 px-3 py-2.5 text-sm text-ink-100 focus:border-brass-500 focus:outline-none"
            >
              <option value="">Anyone</option>
              {everyone.map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
          </div>

          {peopleColumns.length > 1 && (
            <div className="space-y-1.5">
              <label className="label-micro" htmlFor="dash-role">In role</label>
              <select
                id="dash-role"
                value={roleKey}
                onChange={(e) => { setRoleKey(e.target.value); setOffset(0) }}
                className="min-w-[160px] cursor-pointer rounded-xl border border-[var(--color-rule-strong)] bg-ink-950 px-3 py-2.5 text-sm text-ink-100 focus:border-brass-500 focus:outline-none"
              >
                <option value="">Any role</option>
                {peopleColumns.map((p) => (
                  <option key={p.key} value={p.key}>{p.label}</option>
                ))}
              </select>
            </div>
          )}

          <label className="flex cursor-pointer select-none items-center gap-2 pb-2.5 text-sm text-ink-300">
            <input
              type="checkbox"
              aria-label="Show overdue rows only"
              checked={overdueOnly}
              onChange={(e) => { setOverdueOnly(e.target.checked); setOffset(0) }}
              className="h-4 w-4 cursor-pointer accent-[var(--color-brass-400)]"
            />
            Overdue only
          </label>
        </div>

        {view === "grid" ? (
          <section className="space-y-3">
            {isLoading && !rowsData ? (
              <p className="text-sm text-ink-500">Reading the sheet…</p>
            ) : labelledRows.length === 0 ? (
              <p className="text-sm text-ink-500">No rows match these filters.</p>
            ) : (
              <>
                {emptyColumnCount > 0 && (
                  <button
                    onClick={() => setShowEmptyColumns((s) => !s)}
                    className="text-[11px] text-ink-500 transition hover:text-ink-300 cursor-pointer"
                  >
                    {showEmptyColumns
                      ? `Hide ${emptyColumnCount} empty ${emptyColumnCount === 1 ? "column" : "columns"}`
                      : `${emptyColumnCount} empty ${emptyColumnCount === 1 ? "column is" : "columns are"} hidden — show`}
                  </button>
                )}
                <DataTable
                  rows={labelledRows}
                  columns={gridColumns.map((h) => columnLabels[h] || h)}
                  maxHeight="calc(100vh - 320px)"
                  clampCells
                  renderCell={(label, value, rowIndex) => {
                    const header = headerByLabel[label]
                    const isPerson = personHeaders.has(header)
                    const idHeader = rowsData?.primary_id_column
                    // Editing needs a person column and a way to address the row. Without
                    // an id column the sheet has no stable row key, so the cell stays
                    // read-only rather than guessing at row position, which reorders.
                    if (!isPerson || !idHeader) return undefined

                    const rowId = rowsData?.rows[rowIndex]?.[idHeader]
                    if (!rowId) return undefined

                    // The sheet row this cell was rendered from, so an edit to a
                    // duplicated ID lands on the row the user is looking at.
                    const rowNumber = rowsData?.rows[rowIndex]?.__row_number__
                    const cellId = `${rowId}::${header}::cell`

                    return (
                      <EditableCell
                        label={`${label} for ${rowId}`}
                        // The grid only makes people-columns editable, so "Reassign" is the
                        // verb here — EditableCell's generic default is for surfaces that
                        // also edit dates and status.
                        title={`Reassign ${label}`}
                        value={value}
                        edit={pending[`${rowId}::${header}`]}
                        isEditing={editing === cellId}
                        onBeginEdit={() => setEditing(cellId)}
                        onCancel={() => setEditing(null)}
                        onSave={(next) => saveCell(rowId, header, next, rowNumber)}
                      />
                    )
                  }}
                />
                {total > PAGE_SIZE && (
                  <div className="flex items-center justify-between text-xs text-ink-500">
                    <span>
                      Showing {offset + 1}–{showingTo} of {total}
                    </span>
                    <div className="flex gap-2">
                      <button
                        onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                        disabled={offset === 0}
                        className="btn btn-ghost disabled:opacity-40"
                      >
                        Previous
                      </button>
                      <button
                        onClick={() => setOffset(offset + PAGE_SIZE)}
                        disabled={showingTo >= total}
                        className="btn btn-ghost disabled:opacity-40"
                      >
                        Next
                      </button>
                    </div>
                  </div>
                )}
              </>
            )}
          </section>
        ) : view === "workload" ? (
          <WorkloadPanel analytics={analytics} isLoading={isLoading} />
        ) : (
          <HealthPanel health={health?.key === tab ? health.data : null} isLoading={health?.key !== tab} />
        )}
      </main>
    </div>
  )
}

function WorkloadPanel({
  analytics,
  isLoading,
}: {
  analytics: AnalyticsResponse | null
  isLoading: boolean
}) {
  if (isLoading && !analytics) return <p className="text-sm text-ink-500">Reading the sheet…</p>
  if (!analytics) return <p className="text-sm text-ink-500">Workload data is unavailable.</p>

  const { workload, by_status, days_source, has_due_dates, effort_rows, total_rows } = analytics

  // Day figures are only worth charting when most rows actually produce one. On a sheet
  // where 40 of 412 rows carry both dates, a "days" chart ranks three people above
  // thirty who show as 0 — which says they have no work rather than no dates. Items are
  // the honest measure below that threshold, and every row has one.
  const effortCoverage = total_rows > 0 ? (effort_rows ?? 0) / total_rows : 0
  const measuresDays = days_source !== null && effortCoverage >= 0.5
  const unit = measuresDays ? "days" : "items"

  // Rank by the value being drawn. Sorting by item count while plotting days put
  // zero-length bars above shorter non-zero ones, so the ordering contradicted the
  // lengths — the one thing a bar chart must not do.
  const bars = [...workload]
    .map((w) => ({
      person: w.person,
      value: measuresDays ? Math.round(w.total_days * 10) / 10 : w.total_items,
    }))
    .filter((b) => b.value > 0)
    .sort((a, b) => b.value - a.value)
    .slice(0, MAX_BARS)

  const totalOverdue = workload.reduce((sum, w) => sum + w.overdue_items, 0)

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Rows" value={String(analytics.total_rows)} />
        <StatTile label="People" value={String(workload.length)} />
        <StatTile
          label="Overdue"
          value={has_due_dates ? String(totalOverdue) : "—"}
          sub={has_due_dates ? undefined : "no due-date column"}
        />
        <StatTile
          label="Assignments"
          value={String(workload.reduce((s, w) => s + w.total_items, 0))}
          sub="one per person per role"
        />
      </div>

      {/* Days get their own tile only when the sheet records them, and always with the
          denominator: "525 days" over 40 of 412 rows is a tenth of the work, not a total. */}
      {days_source !== null && (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <StatTile
            label="Days recorded"
            value={String(Math.round(workload.reduce((s, w) => s + w.total_days, 0)))}
            sub={`across ${effort_rows ?? 0} of ${total_rows} rows${
              days_source === "dates"
                ? " — derived from start and due dates"
                : days_source === "mixed"
                  ? " — mixed sources"
                  : ""
            }`}
          />
          {!measuresDays && (
            <div className="rounded-lg border border-[var(--color-rule-strong)] bg-white/[0.03] px-3 py-2.5">
              <div className="text-[11px] uppercase tracking-wider text-ink-500">Why items, not days</div>
              <p className="mt-1 text-[12px] leading-relaxed text-ink-400">
                Too few rows carry the dates a day figure needs, so ranking by days would
                show most people as having no work rather than no dates.
              </p>
            </div>
          )}
        </div>
      )}

      {bars.length > 0 && (
        <ChartFrame
          title={measuresDays ? "Busiest by days" : "Busiest by assignment count"}
          subtitle={
            measuresDays
              ? days_source === "column"
                ? "From the sheet's effort column"
                : "Derived from start and due dates"
              : `Top ${bars.length} of ${workload.length} people, by number of assigned rows`
          }
        >
          {/* 40px per row, not 34: real names wrap to two lines at this width, and the
              tighter figure clipped the last bar out of the frame. */}
          <ResponsiveContainer width="100%" height={Math.max(140, bars.length * 40)}>
            <BarChart data={bars} layout="vertical" margin={{ top: 0, right: 48, bottom: 0, left: 0 }}>
              <XAxis type="number" hide />
              <YAxis
                type="category"
                dataKey="person"
                width={176}
                tick={{ fill: "#a1a1aa", fontSize: 12 }}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v: string) => (v.length > 22 ? `${v.slice(0, 21)}…` : v)}
              />
              <Tooltip content={<HoverTip unit={unit} />} cursor={{ fill: "rgba(255,255,255,0.04)" }} />
              <Bar dataKey="value" fill={SERIES_HUE} radius={[0, 4, 4, 0]} barSize={14}>
                {bars.map((_, i) => <Cell key={i} fill={SERIES_HUE} />)}
                <LabelList
                  dataKey="value"
                  position="right"
                  offset={8}
                  style={{ fill: "#d4d4d8", fontSize: 12, fontVariantNumeric: "tabular-nums" }}
                />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartFrame>
      )}

      {workload.length > 0 && (
        <ChartFrame
          title="Assignments per person"
          subtitle="One person can hold several roles; each is counted separately."
        >
          <DataTable
            rows={workload.map((w) => {
              const row: Record<string, string | number> = { Person: w.person }
              Object.values(w.roles).forEach((r) => {
                row[r.label] = r.items
              })
              row.Total = w.total_items
              if (has_due_dates) row.Overdue = w.overdue_items
              if (measuresDays) row[unit === "days" ? "Days" : "Items"] = Math.round(w.total_days * 10) / 10
              return row
            })}
          />
        </ChartFrame>
      )}

      {by_status.length > 0 && (
        <ChartFrame title="By status" subtitle={`${analytics.total_rows} rows`}>
          <DataTable rows={by_status.map((s) => ({ Status: s.label, Rows: s.count }))} />
        </ChartFrame>
      )}
    </div>
  )
}

/**
 * What the sheet does not say.
 *
 * The analytics panel reports overdue counts with two significant figures; this one
 * reports that 362 of 412 rows carry no deadline at all, which is what those figures were
 * computed against. Both are true, and only together are they honest.
 *
 * Checks that could not run are rendered separately and never as a passing zero. A panel
 * that says "0 problems" because the schema names no deadline column is worse than no
 * panel: it converts an unanswered question into a clean bill of health.
 */
function HealthPanel({
  health,
  isLoading,
}: {
  health: HealthResponse | null
  isLoading: boolean
}) {
  if (isLoading && !health) return <p className="text-sm text-ink-500">Reading the sheet…</p>
  if (!health) return <p className="text-sm text-ink-500">Health data is unavailable.</p>

  const { checks, skipped, completeness, total_rows, truncated } = health
  const problems = checks.filter((c) => c.severity !== "ok")

  const tone: Record<HealthCheck["severity"], string> = {
    error: "border-[color-mix(in_srgb,var(--color-failed)_35%,transparent)] bg-[color-mix(in_srgb,var(--color-failed)_8%,transparent)]",
    warning: "border-[color-mix(in_srgb,var(--color-brass-400)_35%,transparent)] bg-[color-mix(in_srgb,var(--color-brass-400)_8%,transparent)]",
    info: "border-[var(--color-rule-strong)] bg-white/[0.03]",
    ok: "border-[var(--color-rule)] bg-transparent",
  }
  const dot: Record<HealthCheck["severity"], string> = {
    error: "bg-failed",
    warning: "bg-brass-400",
    info: "bg-ink-500",
    ok: "bg-ink-600",
  }

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Rows checked" value={String(total_rows)} sub={truncated ? "partial scan" : undefined} />
        <StatTile
          label="Checks run"
          value={String(checks.length)}
          sub={skipped.length > 0 ? `${skipped.length} could not run` : undefined}
        />
        <StatTile
          label="Needs attention"
          value={String(problems.length)}
          sub={problems.length === 0 ? "nothing flagged" : undefined}
        />
        <StatTile
          // "—" rather than a number the sheet never earned: a fill rate over zero
          // declared columns would be 100% on a sheet nobody has described.
          label="Critical fields filled"
          value={completeness ? `${completeness.score}%` : "—"}
          sub={
            completeness
              ? `${completeness.filled} of ${completeness.cells} cells`
              : "no critical fields declared"
          }
        />
      </div>

      {truncated && (
        <p className="text-[12px] text-ink-500">
          The scan hit its row ceiling, so every count below is a floor rather than a total.
        </p>
      )}

      <section className="space-y-2">
        <h2 className="label-micro">Checks</h2>
        {checks.map((c) => (
          <div key={c.key} className={`rounded-lg border px-3 py-2.5 ${tone[c.severity]}`}>
            <div className="flex items-baseline gap-2">
              <span className={`h-1.5 w-1.5 shrink-0 translate-y-[-1px] rounded-full ${dot[c.severity]}`} />
              <span className="text-sm font-medium text-ink-100">{c.label}</span>
              <span className="tabular-nums text-sm text-ink-300">
                {c.count}
                {c.total > 0 && <span className="text-ink-500"> / {c.total}</span>}
              </span>
            </div>
            <p className="mt-1 pl-3.5 text-[12px] leading-relaxed text-ink-400">{c.detail}</p>
            {c.samples.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1.5 pl-3.5">
                {c.samples.map((s, i) => (
                  <span
                    key={`${c.key}-${i}`}
                    className="rounded border border-[var(--color-rule)] px-1.5 py-0.5 font-mono text-[11px] text-ink-400"
                  >
                    {/* Row 0 marks a sample that is about a value rather than a row —
                        the near-duplicate names, which belong to no single row. */}
                    {s.row > 0 && <span className="text-ink-500">row {s.row} </span>}
                    {s.id || s.column}
                    {s.value && <span className="text-ink-500"> · {s.value}</span>}
                  </span>
                ))}
                {c.count > c.samples.length && (
                  <span className="px-1 py-0.5 text-[11px] text-ink-500">
                    +{c.count - c.samples.length} more
                  </span>
                )}
              </div>
            )}
          </div>
        ))}
      </section>

      {skipped.length > 0 && (
        <section className="space-y-2">
          <h2 className="label-micro">Not checked</h2>
          <p className="text-[12px] text-ink-500">
            This tab&rsquo;s schema does not name the columns these need. Map them in Admin →
            Projects and they start reporting.
          </p>
          {skipped.map((s) => (
            <div key={s.key} className="flex flex-wrap items-baseline gap-2 text-[12.5px]">
              <span className="text-ink-300">{s.label}</span>
              <span className="text-ink-500">{s.reason}</span>
            </div>
          ))}
        </section>
      )}
    </div>
  )
}

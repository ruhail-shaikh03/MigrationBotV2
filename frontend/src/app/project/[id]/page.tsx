"use client"

import { useSession } from "next-auth/react"
import { useParams, useRouter } from "next/navigation"
import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Bar, BarChart, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts"
import {
  AlertTriangle, ArrowLeft, Grid3x3, RefreshCw, Search, Users,
} from "lucide-react"

import {
  ChartFrame, DataTable, HoverTip, MAX_BARS, SERIES_HUE, StatTile,
} from "@/components/DataDisplay"

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
}

interface RoleColumn {
  key: string
  label: string
  header: string
}

interface RowsResponse {
  tab: string
  columns: ColumnDescriptor[]
  people_columns: RoleColumn[]
  rows: Record<string, string>[]
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
  has_due_dates: boolean
  truncated: boolean
}

const PAGE_SIZE = 50

export default function ProjectDashboard() {
  const { data: session, status: authStatus } = useSession()
  const router = useRouter()
  const params = useParams()
  const projectId = params?.id as string

  const [view, setView] = useState<"grid" | "workload">("grid")
  const [rowsData, setRowsData] = useState<RowsResponse | null>(null)
  const [analytics, setAnalytics] = useState<AnalyticsResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [errorMsg, setErrorMsg] = useState("")

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
      if (q.trim()) query.set("q", q.trim())
      if (person) query.set("person", person)
      if (roleKey) query.set("role_key", roleKey)
      if (overdueOnly) query.set("overdue", "true")

      const [rowsRes, analyticsRes] = await Promise.all([
        fetch(`/api/projects/${projectId}/rows?${query}`, { headers: authHeaders }),
        fetch(`/api/projects/${projectId}/analytics`, { headers: authHeaders }),
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
  }, [apiToken, projectId, offset, q, person, roleKey, overdueOnly, authHeaders])

  useEffect(() => {
    load()
  }, [load])

  // Reassignments land through the queue, so the terminal frame is the cue to re-read.
  // Reusing the existing queue_update -> CustomEvent bridge means the dashboard reflects
  // an edit made from chat, too.
  useEffect(() => {
    const onQueueUpdate = () => load()
    window.addEventListener("queue_update", onQueueUpdate)
    return () => window.removeEventListener("queue_update", onQueueUpdate)
  }, [load])

  const peopleColumns = rowsData?.people_columns || analytics?.people_columns || []
  const everyone = useMemo(() => {
    const names = new Set<string>()
    analytics?.workload.forEach((w) => names.add(w.person))
    return Array.from(names).sort((a, b) => a.localeCompare(b))
  }, [analytics])

  const gridColumns = useMemo(
    () => (rowsData?.columns || []).map((c) => c.header),
    [rowsData]
  )
  const columnLabels = useMemo(() => {
    const map: Record<string, string> = {}
    ;(rowsData?.columns || []).forEach((c) => {
      map[c.header] = c.label
    })
    return map
  }, [rowsData])

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
              <h1 className="display-md">Project Dashboard</h1>
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
            </div>
            <button onClick={() => load()} className="btn btn-ghost" aria-label="Refresh">
              <RefreshCw className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`} />
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1400px] space-y-5 p-6">
        {errorMsg && (
          <div className="flex items-start gap-2 rounded-xl border border-[color-mix(in_srgb,var(--color-failed)_35%,transparent)] bg-[color-mix(in_srgb,var(--color-failed)_10%,transparent)] px-4 py-3 text-sm text-failed">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{errorMsg}</span>
          </div>
        )}

        {/* Filters apply to both views: a workload chart that ignores the active filter
            would contradict the grid beside it. */}
        <div className="flex flex-wrap items-end gap-3">
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
                <DataTable
                  rows={labelledRows}
                  columns={gridColumns.map((h) => columnLabels[h] || h)}
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
        ) : (
          <WorkloadPanel analytics={analytics} isLoading={isLoading} />
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

  const { workload, by_status, days_source, has_due_dates } = analytics
  // days_source === null means the sheet records no effort anywhere. Plotting zeros would
  // assert that everyone has no work, so the chart measures item counts and says so.
  const measuresDays = days_source !== null
  const unit = measuresDays ? "days" : "items"
  const bars = workload.slice(0, MAX_BARS).map((w) => ({
    person: w.person,
    value: measuresDays ? Math.round(w.total_days * 10) / 10 : w.total_items,
  }))
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
          label={measuresDays ? "Total days" : "Total items"}
          value={String(
            measuresDays
              ? Math.round(workload.reduce((s, w) => s + w.total_days, 0))
              : workload.reduce((s, w) => s + w.total_items, 0)
          )}
          sub={
            days_source === "dates"
              ? "derived from dates"
              : days_source === "mixed"
                ? "mixed sources"
                : days_source === null
                  ? "no effort column"
                  : undefined
          }
        />
      </div>

      {bars.length > 0 && (
        <ChartFrame
          title={measuresDays ? "Workload by person (days)" : "Workload by person (items)"}
          subtitle={
            days_source === "column"
              ? "From the sheet's effort column"
              : days_source === "dates"
                ? "Derived from start and due dates — the sheet records no effort column"
                : days_source === "mixed"
                  ? "Mixed: some rows from an effort column, some derived from dates"
                  : "The sheet records no effort, so this counts items rather than days"
          }
        >
          <ResponsiveContainer width="100%" height={Math.max(120, bars.length * 34)}>
            <BarChart data={bars} layout="vertical" margin={{ top: 0, right: 44, bottom: 0, left: 0 }}>
              <XAxis type="number" hide />
              <YAxis
                type="category"
                dataKey="person"
                width={168}
                tick={{ fill: "#a1a1aa", fontSize: 12 }}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v: string) => (v.length > 24 ? `${v.slice(0, 23)}…` : v)}
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

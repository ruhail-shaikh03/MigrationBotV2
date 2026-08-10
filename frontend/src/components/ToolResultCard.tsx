"use client"

import { useState } from "react"
import {
  Bar, BarChart, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts"
import { ChevronDown, Table2 } from "lucide-react"

/**
 * Structured renderers for tool results.
 *
 * The tools already return clean structured data (sheets/read.py) — summarize's
 * `breakdown: [{value, count}]`, search_rows' `rows: [{col: val}]` — but the feed only
 * ever showed a JSON.stringify of the *arguments*. The numbers were there all along;
 * nothing drew them.
 *
 * Form choices follow the data's job, not decoration:
 *  - count_by_field is a magnitude comparison across categories -> horizontal bar,
 *    ONE hue (length carries magnitude; hue carries nothing, so it must not vary).
 *    Horizontal because status/owner labels are long.
 *  - completion_rate is a single ratio against a limit -> a meter with a hero figure.
 *    Deliberately NOT a two-slice donut, which is the classic wrong answer here.
 *  - blank_fields is one headline number -> a stat tile, not a one-bar chart.
 *  - More than ~7 meaningful classes -> the table view, not more colors.
 *
 * SERIES_HUE is validated against this surface (#030014 page + 5% white bubble ~= #100d20)
 * with the palette validator: lightness band, chroma floor and >=3:1 contrast all pass.
 * Don't swap it for a themed accent without re-running that check.
 */
const SERIES_HUE = "#3987e5"
const MAX_BARS = 8

/** Tool results arrive as parsed JSON, so the boundary is untyped; narrow at each use. */
type AnyResult = Record<string, unknown>

interface CountByFieldResult {
  field: string
  scope?: string
  total_rows: number
  truncated?: boolean
  breakdown: { value: string; count: number }[]
}

interface CompletionRateResult {
  field: string
  target_value: string
  total_rows: number
  truncated?: boolean
  completed: number
  not_completed: number
  blank: number
  completion_pct: number
}

interface BlankFieldsResult {
  field: string
  total_rows: number
  blank_count: number
  blank_pct: number
  ids?: string[]
}

interface SearchRowsResult {
  count: number
  rows: Record<string, string>[]
  capped?: boolean
  truncated?: boolean
}

function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2.5">
      <div className="text-[11px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div className="mt-0.5 text-xl font-semibold text-zinc-100 tabular-nums">{value}</div>
      {sub && <div className="text-[11px] text-zinc-500">{sub}</div>}
    </div>
  )
}

function ChartFrame({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.02] p-4">
      <div className="mb-3">
        <div className="text-[13px] font-semibold text-zinc-200">{title}</div>
        {subtitle && <div className="text-[11px] text-zinc-500">{subtitle}</div>}
      </div>
      {children}
    </div>
  )
}

/** Shared tooltip. An HTML chart is interactive by default; hover is not optional. */
function HoverTip({
  active,
  payload,
  label,
}: {
  active?: boolean
  payload?: { value?: number }[]
  label?: string
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border border-white/15 bg-[#0b0726] px-3 py-2 text-xs shadow-xl">
      <div className="font-medium text-zinc-200">{label}</div>
      <div className="text-zinc-400 tabular-nums">
        {payload[0].value} {payload[0].value === 1 ? "row" : "rows"}
      </div>
    </div>
  )
}

function DataTable({ rows, caption }: { rows: Record<string, string | number>[]; caption?: string }) {
  if (!rows.length) return <div className="text-xs text-zinc-500">No rows.</div>
  // Union of keys across rows, not just the first: search_rows omits columns that were
  // blank for a given row, so a first-row-only header would silently drop fields.
  const columns = Array.from(
    rows.reduce<Set<string>>((set, r) => {
      Object.keys(r).forEach((k) => set.add(k))
      return set
    }, new Set<string>())
  )

  return (
    <div>
      {caption && <div className="mb-2 text-[11px] text-zinc-500">{caption}</div>}
      <div className="overflow-x-auto rounded-lg border border-white/10">
        <table className="w-full border-collapse text-[12.5px]">
          <thead className="bg-white/[0.04]">
            <tr>
              {columns.map((c) => (
                <th key={c} className="whitespace-nowrap border-b border-white/10 px-3 py-2 text-left font-semibold text-zinc-200">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="transition-colors hover:bg-white/[0.02]">
                {columns.map((c) => (
                  <td key={c} className="border-b border-white/5 px-3 py-2 align-top text-zinc-300">
                    {String(r[c] ?? "")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** count_by_field -> horizontal bar. One hue; value carried by length + direct label. */
function CountByField({ result }: { result: CountByFieldResult }) {
  const [showTable, setShowTable] = useState(false)
  const breakdown = result.breakdown || []
  if (!breakdown.length) return null

  const shown = breakdown.slice(0, MAX_BARS)
  const hidden = breakdown.length - shown.length
  // >7 meaningful classes is the table's job, so default to it rather than 20 stubby bars.
  const tableView = showTable || breakdown.length > 12

  return (
    <ChartFrame
      title={`${result.field} — ${result.total_rows} rows`}
      subtitle={[
        result.scope && result.scope !== "all modules" ? `Scope: ${result.scope}` : null,
        result.truncated ? "Row ceiling reached — partial" : null,
      ].filter(Boolean).join(" · ") || undefined}
    >
      {tableView ? (
        <DataTable rows={breakdown.map((b) => ({ [result.field]: b.value, Rows: b.count }))} />
      ) : (
        <ResponsiveContainer width="100%" height={Math.max(120, shown.length * 34)}>
          <BarChart data={shown} layout="vertical" margin={{ top: 0, right: 44, bottom: 0, left: 0 }}>
            <XAxis type="number" hide />
            {/* Real category values run long ("Pending at Functional End"), which would
                clip against a fixed axis width. Truncate the tick and let the hover
                tooltip carry the full label. */}
            <YAxis
              type="category"
              dataKey="value"
              width={168}
              tick={{ fill: "#a1a1aa", fontSize: 12 }}
              tickLine={false}
              axisLine={false}
              tickFormatter={(v: string) => (v.length > 24 ? `${v.slice(0, 23)}…` : v)}
            />
            <Tooltip content={<HoverTip />} cursor={{ fill: "rgba(255,255,255,0.04)" }} />
            {/* 4px rounded data-end, anchored square to the baseline. */}
            <Bar dataKey="count" fill={SERIES_HUE} radius={[0, 4, 4, 0]} barSize={14}>
              {shown.map((_, i) => <Cell key={i} fill={SERIES_HUE} />)}
              <LabelList
                dataKey="count"
                position="right"
                offset={8}
                style={{ fill: "#d4d4d8", fontSize: 12, fontVariantNumeric: "tabular-nums" }}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}

      <div className="mt-3 flex items-center gap-3">
        {breakdown.length <= 12 && (
          <button
            onClick={() => setShowTable((s) => !s)}
            className="inline-flex items-center gap-1.5 text-[11px] text-zinc-500 hover:text-zinc-300 transition cursor-pointer"
          >
            <Table2 className="h-3 w-3" />
            {showTable ? "Show chart" : "Show data"}
          </button>
        )}
        {hidden > 0 && !tableView && (
          <span className="text-[11px] text-zinc-500">+{hidden} more — use “Show data”</span>
        )}
      </div>
    </ChartFrame>
  )
}

/** completion_rate -> meter + hero figure. A 2-slice donut would be the wrong form. */
function CompletionRate({ result }: { result: CompletionRateResult }) {
  const pct = Number(result.completion_pct ?? 0)
  return (
    <ChartFrame
      title={`${result.field} = “${result.target_value}”`}
      subtitle={`${result.total_rows} rows${result.truncated ? " · row ceiling reached" : ""}`}
    >
      <div className="flex items-end gap-3">
        <div className="text-4xl font-semibold leading-none text-zinc-50 tabular-nums">{pct}%</div>
        <div className="pb-1 text-xs text-zinc-500">
          {result.completed} of {result.total_rows}
        </div>
      </div>
      <div className="mt-3 h-2.5 w-full overflow-hidden rounded-full bg-white/10">
        <div
          className="h-full rounded-full transition-all"
          style={{ width: `${Math.min(100, Math.max(0, pct))}%`, backgroundColor: SERIES_HUE }}
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`${result.field} completion`}
        />
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2">
        <StatTile label="Complete" value={String(result.completed ?? 0)} />
        <StatTile label="Remaining" value={String(result.not_completed ?? 0)} />
        <StatTile label="Blank" value={String(result.blank ?? 0)} />
      </div>
    </ChartFrame>
  )
}

function BlankFields({ result }: { result: BlankFieldsResult }) {
  const ids = result.ids || []
  return (
    <ChartFrame title={`Blank “${result.field}”`} subtitle={`${result.total_rows} rows scanned`}>
      <div className="grid grid-cols-2 gap-2">
        <StatTile label="Blank" value={String(result.blank_count ?? 0)} sub={`${result.blank_pct ?? 0}% of rows`} />
        <StatTile label="Populated" value={String((result.total_rows ?? 0) - (result.blank_count ?? 0))} />
      </div>
      {ids.length > 0 && (
        <div className="mt-3">
          <div className="mb-1.5 text-[11px] uppercase tracking-wider text-zinc-500">
            Affected IDs{ids.length >= 50 ? " (first 50)" : ""}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {ids.map((id) => (
              <span key={id} className="rounded border border-white/10 bg-white/[0.04] px-1.5 py-0.5 font-mono text-[11px] text-zinc-300">
                {id}
              </span>
            ))}
          </div>
        </div>
      )}
    </ChartFrame>
  )
}

function RawResult({ result }: { result: AnyResult }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.02]">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-3 py-2 text-[11px] text-zinc-500 hover:text-zinc-300 transition cursor-pointer"
      >
        <ChevronDown className={`h-3 w-3 transition-transform ${open ? "" : "-rotate-90"}`} />
        Raw result
      </button>
      {open && (
        <pre className="max-h-72 overflow-auto border-t border-white/10 px-3 py-2 font-mono text-[11px] leading-relaxed text-zinc-400">
          {JSON.stringify(result, null, 2)}
        </pre>
      )}
    </div>
  )
}

export default function ToolResultCard({ tool, result }: { tool: string; result: AnyResult | undefined }) {
  if (!result || result.ok === false) return null

  // The `report` / `rows` discriminants come from sheets/read.py's return payloads; each
  // cast is guarded by the check immediately preceding it.
  if (tool === "summarize") {
    if (result.report === "count_by_field") {
      return <CountByField result={result as unknown as CountByFieldResult} />
    }
    if (result.report === "completion_rate") {
      return <CompletionRate result={result as unknown as CompletionRateResult} />
    }
    if (result.report === "blank_fields") {
      return <BlankFields result={result as unknown as BlankFieldsResult} />
    }
  }

  if (tool === "search_rows" && Array.isArray(result.rows)) {
    const search = result as unknown as SearchRowsResult
    if (!search.rows.length) return null
    return (
      <ChartFrame
        title={`${search.count} matching ${search.count === 1 ? "row" : "rows"}`}
        subtitle={[
          search.capped ? "Result limit reached" : null,
          search.truncated ? "Row ceiling reached — partial" : null,
        ].filter(Boolean).join(" · ") || undefined}
      >
        <DataTable rows={search.rows} />
      </ChartFrame>
    )
  }

  if (tool === "data_quality") return <RawResult result={result} />

  return null
}

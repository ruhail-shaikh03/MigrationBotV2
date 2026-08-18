"use client"

import { Undo2 } from "lucide-react"

/**
 * Writes are eventually consistent here — the model enqueues, a worker applies,
 * and the outcome arrives seconds later over the WebSocket. A toast was the
 * wrong primitive for that: it vanished after five seconds, taking the only
 * record of the change with it, so anyone who looked away lost track of whether
 * their edit had landed.
 *
 * The ledger keeps each write visible until it resolves, and keeps the resolved
 * ones for the rest of the session. Each line is stamped with the row it touched,
 * which is the same coordinate vocabulary used everywhere else in the product.
 */

export type LedgerState = "queued" | "applied" | "failed"

export interface LedgerEntry {
  jobId: string
  /** The row identifier the write targeted, e.g. "FF-SD-042". */
  target: string
  /** Sheet tab the write went to. */
  tab: string
  /** Column touched, when the tool reported one. */
  field?: string
  /** New value, when the tool reported one. */
  value?: string
  tool: string
  state: LedgerState
  error?: string
}

const STATE_LABEL: Record<LedgerState, string> = {
  queued: "queued",
  applied: "applied",
  failed: "failed",
}

export default function WriteLedger({
  entries,
  onDismiss,
  onUndo,
  undoing,
}: {
  entries: LedgerEntry[]
  onDismiss: () => void
  /** Absent when the surface cannot revert — the control is then simply not rendered,
   *  rather than shown disabled with no way to find out why. */
  onUndo?: (entry: LedgerEntry) => void
  /** jobId currently being reverted, so only that row's control shows as busy. */
  undoing?: string | null
}) {
  if (entries.length === 0) return null

  const pending = entries.filter((e) => e.state === "queued").length

  return (
    <div className="panel animate-rise rounded-lg">
      <div className="flex items-center gap-2 border-b border-[var(--color-rule)] px-3.5 py-2">
        <span className="label-micro">
          Changes this session
          {pending > 0 && <span className="ml-2 text-queued">{pending} in flight</span>}
        </span>
        <button
          type="button"
          onClick={onDismiss}
          className="btn btn-ghost ml-auto px-2 py-1 text-[11px]"
        >
          <Undo2 className="h-3 w-3" />
          Clear
        </button>
      </div>

      <ul className="max-h-40 divide-y divide-[var(--color-rule)] overflow-y-auto">
        {entries.map((e) => (
          <li key={e.jobId} className="flex flex-wrap items-center gap-x-2.5 gap-y-1 px-3.5 py-2.5 text-[13px]">
            <span className="stamp stamp-muted">{e.tab}</span>
            <span className="stamp">{e.target}</span>

            {e.field ? (
              <span className="text-ink-400">{e.field}</span>
            ) : (
              <span className="text-ink-400">{e.tool}</span>
            )}

            {e.value && (
              <>
                <span className="text-ink-500">→</span>
                <span className="font-medium text-ink-100">{e.value}</span>
              </>
            )}

            <span
              className={`status status-${e.state} ml-auto ${e.state === "queued" ? "animate-inflight" : ""}`}
            >
              {STATE_LABEL[e.state]}
            </span>

            {/* Only on applied writes with a named field. A queued one has not landed yet,
                a failed one changed nothing, and a write with no field (add_row) has no
                cell to put back — the server refuses all three, so offering the control
                would only produce an error the user could have been spared. */}
            {onUndo && e.state === "applied" && e.field && (
              <button
                type="button"
                onClick={() => onUndo(e)}
                disabled={undoing === e.jobId}
                className="btn btn-ghost px-2 py-0.5 text-[11px] disabled:opacity-40"
              >
                <Undo2 className="h-3 w-3" />
                {undoing === e.jobId ? "Undoing…" : "Undo"}
              </button>
            )}

            {e.state === "failed" && e.error && (
              <p className="w-full text-[12px] leading-relaxed text-failed">{e.error}</p>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

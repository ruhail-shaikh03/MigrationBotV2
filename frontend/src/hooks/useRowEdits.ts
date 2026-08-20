"use client"

import { useCallback, useEffect, useState } from "react"

/** An edit that has been queued but not yet confirmed applied by the worker, keyed
 *  `${rowId}::${header}`. Writes are eventually consistent (the worker applies at
 *  1 req/sec), so the cell has to show its own pending state or the edit looks lost. */
export interface PendingEdit {
  jobId: string
  value: string
  state: "queued" | "failed"
  error?: string
}

export function editKey(rowId: string, header: string): string {
  return `${rowId}::${header}`
}

/**
 * One queued-write state machine, shared by every surface that edits a cell.
 *
 * Lifted verbatim out of project/[id]/page.tsx when the timeline panel needed the same
 * cells inside a dialog. Two implementations of this would drift — the argument
 * DataTable's `renderCell` prop already made when the dashboard wanted editable cells
 * rather than its own table.
 *
 * Three resolution paths, and all three are needed:
 *   - the `queue_update` frame the worker publishes on a terminal state, bridged to a DOM
 *     CustomEvent by useWebSocket;
 *   - a GET /api/jobs/{id} poll 8s later, for when that frame was missed (a reload, a
 *     reconnect, a worker restart), which otherwise strands a cell on "queued" forever;
 *   - an immediate local failure, for a 403 or an unreachable server.
 */
export function useRowEdits({
  projectId,
  tab,
  authHeaders,
  apiToken,
  onApplied,
}: {
  projectId: string
  tab?: string
  authHeaders: Record<string, string>
  apiToken?: string
  /** Called when a write has actually landed, so the caller can re-read. */
  onApplied: () => void
}) {
  const [pending, setPending] = useState<Record<string, PendingEdit>>({})
  const [editing, setEditing] = useState<string | null>(null)

  const saveCell = useCallback(
    async (rowId: string, header: string, value: string, rowNumber?: number) => {
      const key = editKey(rowId, header)
      setEditing(null)
      try {
        const res = await fetch(`/api/projects/${projectId}/rows/${encodeURIComponent(rowId)}`, {
          method: "PATCH",
          headers: { ...authHeaders, "Content-Type": "application/json" },
          // `row_number` names the row actually on screen. IDs are not unique on every
          // tracker — 27 of 412 rows on the reference sheet share one — so without it the
          // server can only refuse the edit rather than guess which row was clicked.
          body: JSON.stringify({
            tab,
            updates: [{ field: header, value }],
            row_number: rowNumber,
          }),
        })
        const body = await res.json().catch(() => ({}))
        if (!res.ok) {
          // A 403 here is the RBAC checker refusing the write; show its own wording
          // rather than a generic failure, since it explains what to ask an admin for.
          setPending((prev) => ({
            ...prev,
            [key]: { jobId: "", value, state: "failed", error: body.detail || `Refused (${res.status}).` },
          }))
          return
        }
        setPending((prev) => ({ ...prev, [key]: { jobId: body.job_id, value, state: "queued" } }))
      } catch {
        setPending((prev) => ({
          ...prev,
          [key]: { jobId: "", value, state: "failed", error: "Could not reach the server." },
        }))
      }
    },
    [projectId, authHeaders, tab]
  )

  // Reassignments land through the queue, so the terminal frame is the cue to re-read.
  // Reusing the existing queue_update -> CustomEvent bridge means the page reflects an
  // edit made from chat, too.
  useEffect(() => {
    const onQueueUpdate = (event: Event) => {
      const detail = (event as CustomEvent).detail as
        | { job_id?: string; status?: string; error?: string }
        | undefined

      if (detail?.job_id) {
        setPending((prev) => {
          const key = Object.keys(prev).find((k) => prev[k].jobId === detail.job_id)
          if (!key) return prev
          const next = { ...prev }
          if (detail.status === "completed") {
            // Applied: drop the pending marker and let the reload below show the real
            // cell. Keeping it would leave the UI asserting a value the sheet may have
            // normalised differently.
            delete next[key]
          } else {
            next[key] = { ...next[key], state: "failed", error: detail.error }
          }
          return next
        })
      }
      if (detail?.status === "completed") onApplied()
    }
    window.addEventListener("queue_update", onQueueUpdate)
    return () => window.removeEventListener("queue_update", onQueueUpdate)
  }, [onApplied])

  // A terminal frame can be missed — a reload, a reconnect, a worker restart — which
  // would strand a cell on "queued" forever. GET /api/jobs/{id} is the authoritative
  // fallback for exactly that.
  useEffect(() => {
    const stuck = Object.entries(pending).filter(([, p]) => p.state === "queued")
    if (stuck.length === 0 || !apiToken) return

    const timer = setTimeout(async () => {
      for (const [key, entry] of stuck) {
        try {
          const res = await fetch(`/api/jobs/${entry.jobId}`, { headers: authHeaders })
          if (!res.ok) continue
          const job = await res.json()
          if (job.status === "done") {
            setPending((prev) => {
              const next = { ...prev }
              delete next[key]
              return next
            })
            onApplied()
          } else if (job.status === "error" || job.status === "dead_letter") {
            setPending((prev) => ({
              ...prev,
              [key]: { ...prev[key], state: "failed", error: job.error },
            }))
          }
        } catch {
          // Reconciliation is best-effort; the next poll or a manual refresh will catch it.
        }
      }
    }, 8000)
    return () => clearTimeout(timer)
  }, [pending, apiToken, authHeaders, onApplied])

  return { pending, editing, setEditing, saveCell, editKey }
}

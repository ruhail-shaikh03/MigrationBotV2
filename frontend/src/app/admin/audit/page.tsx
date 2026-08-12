"use client"

import { useSession } from "next-auth/react"
import { useEffect, useState } from "react"
import { 
  ShieldCheck, Search, Filter, RefreshCw, AlertCircle, CheckCircle
} from "lucide-react"

interface AuditLog {
  id: number
  timestamp: string
  user_email: string
  session_id: string
  tool_name: string
  spreadsheet_id: string
  sheet_tab: string
  ricefw_id: string
  field: string
  old_value: any
  new_value: any
  args_json: any
  result_ok: boolean
  error: string | null
}

export default function AdminAudit() {
  const { data: session } = useSession()
  const apiToken = session?.apiToken || ""

  // Data lists state
  const [audits, setAudits] = useState<AuditLog[]>([])
  const [isLoading, setIsLoading] = useState(true)

  // Filters inputs
  const [userEmail, setUserEmail] = useState("")
  const [toolName, setToolName] = useState("")
  const [ricefwId, setRicefwId] = useState("")

  const fetchAudits = async () => {
    try {
      setIsLoading(true)
      const baseUrl = ""
      const headers = { "Authorization": `Bearer ${apiToken}` }
      
      // Construct query string params
      const params = new URLSearchParams()
      if (userEmail.trim()) params.append("user_email", userEmail.trim())
      if (toolName.trim()) params.append("tool_name", toolName.trim())
      if (ricefwId.trim()) params.append("ricefw_id", ricefwId.trim())
      params.append("limit", "100")

      const res = await fetch(`${baseUrl}/api/admin/audits?${params.toString()}`, { headers })
      if (res.ok) {
        const data = await res.json()
        setAudits(data)
      }
    } catch (err) {
      console.error(err)
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    if (apiToken) {
      fetchAudits()
    }
  }, [apiToken])

  // Handle Search submit
  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    fetchAudits()
  }

  // Handle Reset Filters
  const handleReset = () => {
    setUserEmail("")
    setToolName("")
    setRicefwId("")
    // Let state update and then fetch, or call it directly with blanks
    setIsLoading(true)
    const baseUrl = ""
    fetch(`${baseUrl}/api/admin/audits?limit=100`, {
      headers: { "Authorization": `Bearer ${apiToken}` }
    })
      .then(res => res.json())
      .then(data => setAudits(data))
      .finally(() => setIsLoading(false))
  }

  return (
    <div className="space-y-8 animate-rise">
      {/* Header bar */}
      <div>
        <h2 className="display-md">
          Change history
        </h2>
        <p className="text-sm text-ink-500 mt-1">
          Every read and change, with who made it and what it replaced.
        </p>
      </div>

      {/* Filter and Search Bar */}
      <form onSubmit={handleSearchSubmit} className="panel p-5 rounded-xl border border-[var(--color-rule)] space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="space-y-1.5 relative">
            <label className="label-micro">Filter User Email</label>
            <div className="relative">
              <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-ink-500" />
              <input
                type="text"
                value={userEmail}
                onChange={(e) => setUserEmail(e.target.value)}
                placeholder="search email address..."
                className="field pl-9"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="label-micro">Filter Tool Name</label>
            <input
              type="text"
              value={toolName}
              onChange={(e) => setToolName(e.target.value)}
              placeholder="e.g. update_cell, bulk_update"
              className="field"
            />
          </div>

          <div className="space-y-1.5">
            <label className="label-micro">Filter RICEFW ID</label>
            <input
              type="text"
              value={ricefwId}
              onChange={(e) => setRicefwId(e.target.value)}
              placeholder="e.g. SD-045"
              className="field"
            />
          </div>
        </div>

        <div className="flex items-center justify-end gap-3 pt-2">
          <button
            type="button"
            onClick={handleReset}
            className="btn btn-secondary"
          >
            Reset Filters
          </button>
          <button
            type="submit"
            className="btn btn-primary"
          >
            <Filter className="h-3.5 w-3.5" />
            Apply Filters
          </button>
        </div>
      </form>

      {/* Audits Table list */}
      {isLoading ? (
        <div className="flex justify-center h-48 items-center">
          <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-brass-500"></div>
        </div>
      ) : audits.length === 0 ? (
        <div className="panel p-12 text-center text-ink-500 rounded-xl border border-[var(--color-rule)]">
          No audit entries found matching the filter options.
        </div>
      ) : (
        <div className="panel rounded-xl border border-[var(--color-rule)] overflow-hidden">
          <div className="overflow-x-auto">
            {/* Seven columns including an old→new diff; needs the most room. */}
            <table className="w-full min-w-[1080px] text-left border-collapse">
              <thead>
                <tr className="border-b border-[var(--color-rule)] bg-ink-850">
                  <th className="label-micro whitespace-nowrap p-4">Timestamp</th>
                  <th className="label-micro whitespace-nowrap p-4">User</th>
                  <th className="label-micro whitespace-nowrap p-4">Tool Executed</th>
                  <th className="label-micro whitespace-nowrap p-4">Tab / RICEFW ID</th>
                  <th className="label-micro whitespace-nowrap p-4">Field Targeted</th>
                  <th className="label-micro whitespace-nowrap p-4">Changes (Old → New)</th>
                  <th className="label-micro whitespace-nowrap p-4">Result</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--color-rule)] text-xs">
                {audits.map((a) => (
                  <tr key={a.id} className="hover:bg-ink-850 transition">
                    <td className="p-4 text-ink-500 font-mono">
                      {new Date(a.timestamp).toLocaleString(undefined, { 
                        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' 
                      })}
                    </td>
                    <td className="p-4 font-semibold text-ink-300">{a.user_email}</td>
                    <td className="p-4">
                      <span className="px-2 py-0.5 rounded bg-ink-800 border border-[var(--color-rule)] text-[11px] font-mono text-brass-300">
                        {a.tool_name}
                      </span>
                    </td>
                    <td className="p-4 text-ink-300 font-semibold">
                      {a.sheet_tab} {a.ricefw_id && <span className="text-ink-500 font-mono">({a.ricefw_id})</span>}
                    </td>
                    <td className="p-4 text-ink-400 font-mono">{a.field || "-"}</td>
                    <td className="p-4 font-mono max-w-[200px] truncate" title={`${a.old_value} → ${a.new_value}`}>
                      {a.old_value !== null || a.new_value !== null ? (
                        <>
                          <span className="text-failed">{String(a.old_value || "blank")}</span>
                          <span className="text-ink-500 mx-1">→</span>
                          <span className="text-applied">{String(a.new_value || "blank")}</span>
                        </>
                      ) : (
                        <span className="text-ink-500">read operation</span>
                      )}
                    </td>
                    <td className="p-4">
                      {a.result_ok ? (
                        <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-[color-mix(in_srgb,var(--color-applied)_10%,transparent)] border border-[color-mix(in_srgb,var(--color-applied)_25%,transparent)] text-applied font-semibold text-[10px]">
                          <CheckCircle className="h-3 w-3" />
                          OK
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-[color-mix(in_srgb,var(--color-failed)_10%,transparent)] border border-[color-mix(in_srgb,var(--color-failed)_25%,transparent)] text-failed font-semibold text-[10px]" title={a.error || "error details missing"}>
                          <AlertCircle className="h-3 w-3" />
                          ERROR
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

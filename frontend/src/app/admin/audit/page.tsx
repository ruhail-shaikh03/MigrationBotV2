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
  const apiToken = (session as any)?.apiToken || ""

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
    <div className="space-y-8 animate-slide-up">
      {/* Header bar */}
      <div>
        <h2 className="text-3xl font-extrabold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-zinc-50 to-indigo-200">
          Security Audits & History Log
        </h2>
        <p className="text-sm text-zinc-500 mt-1">
          Review historical changes, enqueued writes, read queries, and RBAC errors in real time.
        </p>
      </div>

      {/* Filter and Search Bar */}
      <form onSubmit={handleSearchSubmit} className="glass-panel p-5 rounded-2xl border border-white/5 space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="space-y-1.5 relative">
            <label className="text-[10px] font-bold uppercase tracking-wider text-zinc-400">Filter User Email</label>
            <div className="relative">
              <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-zinc-500" />
              <input
                type="text"
                value={userEmail}
                onChange={(e) => setUserEmail(e.target.value)}
                placeholder="search email address..."
                className="w-full bg-[#120e2e]/50 border border-white/10 rounded-xl pl-10 pr-4 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="text-[10px] font-bold uppercase tracking-wider text-zinc-400">Filter Tool Name</label>
            <input
              type="text"
              value={toolName}
              onChange={(e) => setToolName(e.target.value)}
              placeholder="e.g. update_cell, bulk_update"
              className="w-full bg-[#120e2e]/50 border border-white/10 rounded-xl px-4 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none"
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-[10px] font-bold uppercase tracking-wider text-zinc-400">Filter RICEFW ID</label>
            <input
              type="text"
              value={ricefwId}
              onChange={(e) => setRicefwId(e.target.value)}
              placeholder="e.g. SD-045"
              className="w-full bg-[#120e2e]/50 border border-white/10 rounded-xl px-4 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none"
            />
          </div>
        </div>

        <div className="flex items-center justify-end gap-3 pt-2">
          <button
            type="button"
            onClick={handleReset}
            className="flex items-center gap-1.5 px-4 py-2 rounded-xl border border-white/10 text-zinc-400 hover:text-zinc-200 text-xs font-semibold uppercase transition cursor-pointer"
          >
            Reset Filters
          </button>
          <button
            type="submit"
            className="flex items-center gap-1.5 px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl text-xs font-semibold uppercase transition cursor-pointer shadow-lg"
          >
            <Filter className="h-3.5 w-3.5" />
            Apply Filters
          </button>
        </div>
      </form>

      {/* Audits Table list */}
      {isLoading ? (
        <div className="flex justify-center h-48 items-center">
          <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-indigo-500"></div>
        </div>
      ) : audits.length === 0 ? (
        <div className="glass-panel p-12 text-center text-zinc-500 rounded-2xl border border-white/5">
          No audit entries found matching the filter options.
        </div>
      ) : (
        <div className="glass-panel rounded-2xl border border-white/5 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-white/5 bg-white/[0.02]">
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Timestamp</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">User</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Tool Executed</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Tab / RICEFW ID</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Field Targeted</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Changes (Old → New)</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Result</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5 text-xs">
                {audits.map((a) => (
                  <tr key={a.id} className="hover:bg-white/[0.01] transition">
                    <td className="p-4 text-zinc-500 font-mono">
                      {new Date(a.timestamp).toLocaleString(undefined, { 
                        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' 
                      })}
                    </td>
                    <td className="p-4 font-semibold text-zinc-300">{a.user_email}</td>
                    <td className="p-4">
                      <span className="px-2 py-0.5 rounded bg-white/5 border border-white/5 text-[11px] font-mono text-indigo-300">
                        {a.tool_name}
                      </span>
                    </td>
                    <td className="p-4 text-zinc-300 font-semibold">
                      {a.sheet_tab} {a.ricefw_id && <span className="text-zinc-500 font-mono">({a.ricefw_id})</span>}
                    </td>
                    <td className="p-4 text-zinc-400 font-mono">{a.field || "-"}</td>
                    <td className="p-4 font-mono max-w-[200px] truncate" title={`${a.old_value} → ${a.new_value}`}>
                      {a.old_value !== null || a.new_value !== null ? (
                        <>
                          <span className="text-rose-400">{String(a.old_value || "blank")}</span>
                          <span className="text-zinc-500 mx-1">→</span>
                          <span className="text-emerald-400">{String(a.new_value || "blank")}</span>
                        </>
                      ) : (
                        <span className="text-zinc-500">read operation</span>
                      )}
                    </td>
                    <td className="p-4">
                      {a.result_ok ? (
                        <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 font-semibold text-[10px]">
                          <CheckCircle className="h-3 w-3" />
                          OK
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-rose-500/10 border border-rose-500/20 text-rose-400 font-semibold text-[10px]" title={a.error || "error details missing"}>
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

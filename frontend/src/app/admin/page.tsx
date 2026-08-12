"use client"

import { useSession } from "next-auth/react"
import { useEffect, useState } from "react"
import { 
  FolderKanban, Users, ShieldAlert, AlertCircle, 
  TrendingUp, Activity, Terminal
} from "lucide-react"
import { 
  AreaChart, Area, XAxis, YAxis, CartesianGrid, 
  Tooltip, ResponsiveContainer, BarChart, Bar, Legend
} from "recharts"

interface Metric {
  title: string
  value: string | number
  icon: any
  description: string
  color: string
}

export default function AdminDashboard() {
  const { data: session } = useSession()
  const apiToken = session?.apiToken || ""
  
  // Data States
  const [projectsCount, setProjectsCount] = useState(0)
  const [usersCount, setUsersCount] = useState(0)
  const [auditsCount, setAuditsCount] = useState(0)
  const [errorsCount, setErrorsCount] = useState(0)
  const [chartData, setChartData] = useState<any[]>([])
  const [toolData, setToolData] = useState<any[]>([])
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    if (!apiToken) return

    const fetchMetrics = async () => {
      try {
        const headers = { "Authorization": `Bearer ${apiToken}` }
        const baseUrl = ""
        
        const [summaryRes, projRes, permRes, auditRes] = await Promise.all([
          fetch(`/api/admin/analytics/summary`, { headers }),
          fetch(`/api/admin/projects`, { headers }),
          fetch(`/api/admin/permissions`, { headers }),
          fetch(`/api/admin/audits?limit=500`, { headers })
        ])

        if (summaryRes.ok) {
          const summary = await summaryRes.json()
          setProjectsCount(summary.projects_count)
          setUsersCount(summary.users_count)
          setAuditsCount(summary.audits_total)
          setErrorsCount(summary.failed_operations)
        } else if (projRes.ok && permRes.ok && auditRes.ok) {
          const projs = await projRes.json()
          const perms = await permRes.json()
          const audits = await auditRes.json()

          setProjectsCount(projs.length)
          const uniqueUsers = new Set(perms.map((p: any) => p.user_email))
          setUsersCount(uniqueUsers.size)
          setAuditsCount(audits.length)
          setErrorsCount(audits.filter((a: any) => !a.result_ok).length)
        }

        if (auditRes.ok) {
          const audits = await auditRes.json()

          // Process audits over time for chart (grouped by date)
          const dateMap: { [key: string]: { success: number; failed: number } } = {}
          const toolMap: { [key: string]: number } = {}

          audits.forEach((audit: any) => {
            // Group by date (YYYY-MM-DD)
            const date = new Date(audit.timestamp).toLocaleDateString(undefined, { 
              month: 'short', day: 'numeric' 
            })
            if (!dateMap[date]) {
              dateMap[date] = { success: 0, failed: 0 }
            }
            if (audit.result_ok) {
              dateMap[date].success += 1
            } else {
              dateMap[date].failed += 1
            }

            // Group by tool calls
            const toolName = audit.tool_name || "unknown"
            toolMap[toolName] = (toolMap[toolName] || 0) + 1
          })

          // Transform maps to array
          const timeChart = Object.keys(dateMap).map(key => ({
            date: key,
            success: dateMap[key].success,
            failed: dateMap[key].failed
          })).reverse().slice(-7) // Last 7 active days

          const toolChart = Object.keys(toolMap).map(key => ({
            name: key,
            count: toolMap[key]
          }))

          setChartData(timeChart)
          setToolData(toolChart)
        }
      } catch (err) {
        console.error("Failed to load admin summary:", err)
      } finally {
        setIsLoading(false)
      }
    }

    fetchMetrics()
  }, [apiToken])

  // Colour is semantic here, not decorative: only the failure count is coloured,
  // and it earns it. Giving each tile its own gradient made four unrelated
  // numbers look like four categories of something.
  const metrics: Metric[] = [
    {
      title: "Connected sheets",
      value: projectsCount,
      icon: FolderKanban,
      description: "Sheets the agent can read",
      color: "text-ink-400"
    },
    {
      title: "People with access",
      value: usersCount,
      icon: Users,
      description: "Users with a role on a sheet",
      color: "text-ink-400"
    },
    {
      title: "Changes recorded",
      value: auditsCount,
      icon: ShieldAlert,
      description: "Every read and write, attributed",
      color: "text-ink-400"
    },
    {
      title: "Failed changes",
      value: errorsCount,
      icon: AlertCircle,
      description: "Rejected, denied or timed out",
      color: errorsCount > 0 ? "text-failed" : "text-ink-400"
    }
  ]

  if (isLoading) {
    return (
      <div className="flex h-96 items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-brass-500"></div>
      </div>
    )
  }

  return (
    <div className="space-y-8 animate-rise">
      {/* Title */}
      <div>
        <h2 className="display-md">Overview</h2>
        <p className="mt-1.5 text-[13px] text-ink-400">
          Which sheets are connected, who can reach them, and what has been changed.
        </p>
      </div>

      {/* Metrics Cards Grid */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {metrics.map((m) => {
          const Icon = m.icon
          return (
            <div
              key={m.title}
              className="panel relative flex items-start justify-between overflow-hidden rounded-xl p-5"
            >
              <div>
                <span className="label-micro block">{m.title}</span>
                <span className="mt-2.5 block text-[28px] font-semibold leading-none tracking-tight text-ink-100">
                  {m.value}
                </span>
                <span className="mt-2 block text-[12px] text-ink-500">
                  {m.description}
                </span>
              </div>
              <div className={m.color}>
                <Icon className="h-4.5 w-4.5" />
              </div>
            </div>
          )
        })}
      </div>

      {/* Charts section */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Operations over time */}
        <div className="lg:col-span-2 panel p-6 rounded-xl border border-[var(--color-rule)] space-y-4">
          <div className="flex items-center justify-between">
            <div className="space-y-1">
              <h3 className="font-bold text-ink-200 text-sm tracking-wider uppercase flex items-center gap-2">
                <Activity className="h-4 w-4 text-brass-400" />
                Operations Activity
              </h3>
              <p className="text-xs text-ink-500">Spreadsheet mutations vs. read queries log</p>
            </div>
          </div>
          <div className="h-72 w-full">
            {chartData.length === 0 ? (
              <div className="h-full flex items-center justify-center text-ink-500 text-xs">
                No activity data recorded yet
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                  <defs>
                    <linearGradient id="colorSuccess" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#6366f1" stopOpacity={0.2}/>
                      <stop offset="95%" stopColor="#6366f1" stopOpacity={0}/>
                    </linearGradient>
                    <linearGradient id="colorFailed" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#ef4444" stopOpacity={0.2}/>
                      <stop offset="95%" stopColor="#ef4444" stopOpacity={0}/>
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.03)" />
                  <XAxis dataKey="date" stroke="#71717a" fontSize={10} />
                  <YAxis stroke="#71717a" fontSize={10} />
                  <Tooltip 
                    contentStyle={{ 
                      backgroundColor: "#0d0a21", 
                      border: "1px solid rgba(255,255,255,0.08)",
                      borderRadius: "10px",
                      color: "#f4f4f5",
                      fontSize: "11px"
                    }}
                  />
                  <Area type="monotone" dataKey="success" name="Succeeded" stroke="#6366f1" fillOpacity={1} fill="url(#colorSuccess)" />
                  <Area type="monotone" dataKey="failed" name="Failed/Blocked" stroke="#ef4444" fillOpacity={1} fill="url(#colorFailed)" />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>

        {/* Tool Distribution */}
        <div className="panel p-6 rounded-xl border border-[var(--color-rule)] space-y-4">
          <div className="space-y-1">
            <h3 className="font-bold text-ink-200 text-sm tracking-wider uppercase flex items-center gap-2">
              <Terminal className="h-4 w-4 text-ink-400" />
              Tool Distribution
            </h3>
            <p className="text-xs text-ink-500">Distribution of executed agent actions</p>
          </div>
          <div className="h-72 w-full">
            {toolData.length === 0 ? (
              <div className="h-full flex items-center justify-center text-ink-500 text-xs">
                No tool executions logged
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={toolData} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.03)" />
                  <XAxis dataKey="name" stroke="#71717a" fontSize={9} />
                  <YAxis stroke="#71717a" fontSize={10} />
                  <Tooltip 
                    contentStyle={{ 
                      backgroundColor: "#0d0a21", 
                      border: "1px solid rgba(255,255,255,0.08)",
                      borderRadius: "10px",
                      color: "#f4f4f5",
                      fontSize: "11px"
                    }}
                  />
                  <Bar dataKey="count" name="CallsCount" fill="#a855f7" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

"use client"

import { useSession, signOut } from "next-auth/react"
import { useRouter } from "next/navigation"
import { useEffect, useMemo, useRef, useState } from "react"
import { useChatStore, Project, Message } from "@/store/useChatStore"
import { useWebSocket } from "@/hooks/useWebSocket"
import MarkdownMessage from "@/components/MarkdownMessage"
import ToolResultCard from "@/components/ToolResultCard"
import WriteLedger, { LedgerEntry, LedgerState } from "@/components/WriteLedger"
import {
  Send, Database, LogOut, Settings, RefreshCw, CheckCircle2, AlertTriangle, X, LayoutDashboard
} from "lucide-react"

export default function ChatPage() {
  const { data: session, status } = useSession()
  const router = useRouter()

  // Local UI States
  const [input, setInput] = useState("")
  // Terminal outcomes keyed by job_id, plus the ids the user has cleared.
  const [outcomes, setOutcomes] = useState<Record<string, LedgerEntry>>({})
  const [dismissed, setDismissed] = useState<Set<string>>(new Set())
  const [isAdminState, setIsAdminState] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement | null>(null)

  // Zustand Store
  const { 
    projects, setProjects,
    activeProject, setActiveProject,
    activeTab,
    messages, setMessages,
    isConnected, clearChat
  } = useChatStore()

  const apiToken = session?.apiToken || null
  const googleToken = session?.googleAccessToken || null

  // Instantiate WebSocket
  const { sendMessage, switchTab } = useWebSocket(apiToken, activeProject?.id || null)

  // The queue_update listener is mounted once and must not re-subscribe on every
  // tab change, so it reads the active tab through a ref rather than closing over it.
  const activeTabRef = useRef(activeTab)
  useEffect(() => {
    activeTabRef.current = activeTab
  }, [activeTab])

  // Fetch user profile to check admin status dynamically
  useEffect(() => {
    if (status === "authenticated" && apiToken) {
      fetch(`/api/me`, {
        headers: { "Authorization": `Bearer ${apiToken}` }
      })
        .then(res => res.json())
        .then(data => {
          if (data && data.is_admin) {
            setIsAdminState(true)
          }
        })
        .catch(err => console.error("Error fetching user profile:", err))
    }
  }, [status, apiToken])

  // Redirect if not authenticated
  useEffect(() => {
    if (status === "unauthenticated") {
      router.push("/")
    }
  }, [status, router])

  // Fetch Projects List
  useEffect(() => {
    if (status === "authenticated" && apiToken) {
      const fetchProjects = async () => {
        try {
          const res = await fetch(`/api/projects`, {
            headers: {
              "Authorization": `Bearer ${apiToken}`
            }
          })
          if (res.ok) {
            const data = await res.json()
            setProjects(data)
            if (data.length > 0 && !activeProject) {
              setActiveProject(data[0])
            }
          }
        } catch (err) {
          console.error("Failed to fetch projects:", err)
        }
      }
      fetchProjects()
    }
  }, [status, apiToken, setProjects, setActiveProject, activeProject])

  // Scroll to bottom of chat
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }

  useEffect(() => {
    scrollToBottom()
  }, [messages])

  // Background write outcomes feed the ledger, which keeps them until the user
  // clears them. A toast was wrong for an eventually-consistent write path: it
  // expired after five seconds and took the only record of the change with it.
  useEffect(() => {
    const handleQueueUpdate = (e: Event) => {
      const data = (e as CustomEvent).detail
      const args = data.args || {}
      const firstUpdate = Array.isArray(args.updates) ? args.updates[0] : undefined

      const state: LedgerState =
        data.status === "completed" ? "applied" : data.status === "failed" ? "failed" : "queued"

      const entry: LedgerEntry = {
        jobId: String(data.job_id ?? Math.random()),
        target: args.ricefw_id || "—",
        tab: data.sheet_tab || activeTabRef.current || "—",
        field: firstUpdate?.field ?? args.set_field,
        value: firstUpdate?.value ?? args.set_value,
        tool: data.tool_name || "write",
        state,
        // core/errors.py classifies failures and sends a message written for a
        // person, so this is shown verbatim rather than being flattened to
        // "encountered an error" the way the toast used to.
        error: typeof data.error === "string" && data.error.trim() ? data.error.trim() : undefined,
      }

      setOutcomes((prev) => ({ ...prev, [entry.jobId]: entry }))
    }

    window.addEventListener("queue_update", handleQueueUpdate)
    return () => {
      window.removeEventListener("queue_update", handleQueueUpdate)
    }
  }, [])

  // The queued half of the ledger, derived during render rather than pushed into
  // state from an effect — deriving it in an effect means a second render pass
  // for every message update, and React lints against it for that reason.
  //
  // It has to come from here at all because queue/worker.py publishes a
  // queue_update only on TERMINAL states; there is no in-flight frame on the
  // socket. Verified against a real write, which arrived already "applied". The
  // enqueue result carries job_id and status:"queued" back to us
  // (tool_dispatch.py), so that is the queued row, and the terminal frame in
  // `outcomes` replaces it by job_id below.
  const ledger: LedgerEntry[] = useMemo(() => {
    const rows: LedgerEntry[] = []
    const seen = new Set<string>()

    for (const msg of messages) {
      for (const tool of msg.toolCalls ?? []) {
        const res = tool.result
        if (!res || res.status !== "queued" || !res.job_id) continue
        const jobId = String(res.job_id)
        if (seen.has(jobId) || dismissed.has(jobId)) continue
        seen.add(jobId)

        const args = tool.args || {}
        const firstUpdate = Array.isArray(args.updates) ? args.updates[0] : undefined
        rows.push(
          outcomes[jobId] ?? {
            jobId,
            target: args.ricefw_id || "—",
            tab: activeTab || "—",
            field: firstUpdate?.field ?? args.set_field,
            value: firstUpdate?.value ?? args.set_value,
            tool: tool.name,
            state: "queued",
          }
        )
      }
    }

    // An outcome with no matching tool call — a job recovered by the worker
    // after a restart, say — still deserves a line.
    for (const [jobId, entry] of Object.entries(outcomes)) {
      if (!seen.has(jobId) && !dismissed.has(jobId)) rows.push(entry)
    }

    return rows
  }, [messages, outcomes, dismissed, activeTab])

  // Handle message send
  const handleSend = (e: React.FormEvent) => {
    e.preventDefault()
    if (!input.trim() || !isConnected) return
    sendMessage(input.trim())
    setInput("")
  }

  // Switch Module Tab — sent as an explicit control frame, not a chat message routed
  // through the LLM. The tab highlight updates only once the server confirms with a
  // "tab_switched" frame (handled in useWebSocket.ts), not optimistically here, so it
  // can't drift out of sync with sessions.active_tab if the switch fails.
  const handleTabChange = (tabName: string) => {
    if (!isConnected) return
    switchTab(tabName)
  }

  if (status === "loading" || !session) {
    return (
      <div className="flex h-screen items-center justify-center bg-ink-950">
        <div className="label-micro animate-inflight">Opening your sheets</div>
      </div>
    )
  }

  const isAdmin = isAdminState

  return (
    <div className="relative flex h-screen flex-col overflow-hidden bg-ink-950 text-ink-100">
      {/* Main chat window container */}
      <div className="relative z-10 flex flex-1 flex-col">
        {/* Header Bar */}
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-rule)] bg-ink-900 px-4 py-3 sm:px-6">
          <div className="flex min-w-0 items-center gap-4 sm:gap-5">
            <span className="text-[15px] font-semibold tracking-tight text-ink-100">
              MigrationBot
            </span>

            {/* Project selector */}
            <div className="flex min-w-0 items-center gap-2">
              <label htmlFor="project-select" className="label-micro">
                Sheet
              </label>
              <select
                id="project-select"
                value={activeProject?.id || ""}
                onChange={(e) => {
                  const proj = projects.find((p) => p.id === parseInt(e.target.value))
                  if (proj) setActiveProject(proj)
                }}
                className="field w-auto max-w-[220px] cursor-pointer py-1.5 text-[13px]"
              >
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.project_name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Sheet tabs. Previously `hidden md:flex`, which left no way at all to
              change tab on a narrow screen; now it scrolls sideways instead. */}
          <div className="flex max-w-full items-center gap-1 overflow-x-auto rounded-md border border-[var(--color-rule)] bg-ink-950 p-1">
            {(activeProject?.schema_config?.tabs
              ? Object.keys(activeProject.schema_config.tabs)
              : (activeProject?.schema_config?.global?.valid_modules || [])
            ).map((tab: string) => (
              <button
                key={tab}
                onClick={() => handleTabChange(tab)}
                disabled={!isConnected}
                aria-current={activeTab === tab ? "true" : undefined}
                className={`shrink-0 cursor-pointer rounded-sm px-3 py-1 font-mono text-[12px] font-medium transition disabled:opacity-40 ${
                  activeTab === tab
                    ? "bg-brass-400 text-ink-950"
                    : "text-ink-400 hover:bg-ink-800 hover:text-ink-200"
                }`}
              >
                {tab}
              </button>
            ))}
          </div>

          {/* Connection + account */}
          <div className="flex items-center gap-2">
            <span
              className={`status ${isConnected ? "status-applied" : "status-failed"}`}
              title={isConnected ? "Connected to the sheet" : "Reconnecting"}
            >
              {isConnected ? "live" : "offline"}
            </span>

            {/* The dashboard is the only non-chat surface a non-admin can reach, so the
                entry point is deliberately not gated behind isAdmin. */}
            {activeProject && (
              <button
                onClick={() => router.push(`/project/${activeProject.id}`)}
                className="btn btn-ghost px-2 py-2"
                title="Project dashboard"
              >
                <LayoutDashboard className="h-4 w-4" />
              </button>
            )}

            {isAdmin && (
              <button
                onClick={() => router.push("/admin")}
                className="btn btn-ghost px-2 py-2"
                title="Admin settings"
              >
                <Settings className="h-4 w-4" />
              </button>
            )}

            <button
              onClick={() => signOut({ callbackUrl: "/" })}
              className="btn btn-ghost px-2 py-2"
              title="Sign out"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        </header>

        {/* Messages Feed. The column is capped to the same max-w-4xl the input bar
            uses — it was full-bleed, so on a wide monitor the conversation sprawled
            edge to edge while the composer sat centred underneath it. */}
        <div className="flex-1 overflow-y-auto px-4 py-8 sm:px-6">
          <div className="mx-auto flex min-h-full w-full max-w-4xl flex-col space-y-6">
          {messages.length === 0 ? (
            // An empty screen is an invitation to act: the examples are real
            // prompts and clicking one sends it, rather than describing in prose
            // what the user could theoretically type.
            <div className="flex flex-1 flex-col justify-center py-8">
              <div className="label-micro">
                {activeTab ? `${activeTab} · ready` : "Ready"}
              </div>
              <h2 className="display-md mt-3 max-w-lg">
                What do you want to know about this sheet?
              </h2>
              <p className="mt-3 max-w-md text-[14px] leading-relaxed text-ink-400">
                Ask in plain language. Answers come back as tables and charts, with the rows
                they came from.
              </p>

              <ul className="mt-7 flex flex-wrap gap-2">
                {[
                  "Break down items by status",
                  "How complete is this tab?",
                  "What's overdue?",
                  "Show everything assigned to Sara",
                ].map((example) => (
                  <li key={example}>
                    <button
                      type="button"
                      disabled={!isConnected}
                      onClick={() => sendMessage(example)}
                      className="btn btn-secondary text-[13px] font-normal"
                    >
                      {example}
                    </button>
                  </li>
                ))}
              </ul>

              <p className="mt-6 flex items-center gap-2 text-[12.5px] text-ink-500">
                <Database className="h-3.5 w-3.5 shrink-0" />
                You can also make changes — &ldquo;set status to Done for SD-045&rdquo;.
              </p>
            </div>
          ) : (
            messages.map((msg) => {
              if (msg.role === "system") {
                return (
                  <div key={msg.id} className="animate-rise flex justify-center">
                    <div className="flex items-center gap-2 rounded-md border border-[color-mix(in_srgb,var(--color-failed)_25%,transparent)] bg-[color-mix(in_srgb,var(--color-failed)_10%,transparent)] px-3 py-2 text-[12.5px] text-failed">
                      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                      <span>{msg.content}</span>
                    </div>
                  </div>
                )
              }

              const isUser = msg.role === "user"

              return (
                <div
                  key={msg.id}
                  className={`flex w-full ${isUser ? "justify-end" : "justify-start"} animate-rise`}
                >
                  <div
                    className={`flex min-w-0 flex-col space-y-2 ${
                      // The agent's reply is plain text on the page, so it gets the
                      // full column; only the user's turn is a bubble that needs
                      // constraining.
                      isUser ? "max-w-[85%]" : "w-full"
                    }`}
                  >
                    <div className={isUser ? "bubble-user px-4 py-2.5" : "bubble-agent"}>
                      {/* User text stays literal — it is whatever they typed. Assistant
                          text is Markdown, which is the format it is written in. */}
                      {isUser ? (
                        <p className="select-text whitespace-pre-wrap text-[14.5px] leading-relaxed">
                          {msg.content}
                        </p>
                      ) : (
                        msg.content && <MarkdownMessage content={msg.content} />
                      )}

                      {/* Thinking indicator on an as-yet-empty assistant turn */}
                      {!isUser && msg.content === "" && (!msg.toolCalls || msg.toolCalls.length === 0) && (
                        <span className="label-micro animate-inflight">Reading the sheet</span>
                      )}
                    </div>

                    {/* Tool Calls Visualizer */}
                    {!isUser && msg.toolCalls && msg.toolCalls.length > 0 && (
                      <div className="space-y-2">
                        {msg.toolCalls.map((tool, idx) => (
                          <div key={idx} className="space-y-2">
                            <div className="card flex items-center justify-between gap-3 rounded-md px-3 py-2">
                              <div className="flex min-w-0 items-center gap-2.5">
                                {tool.status === "running" ? (
                                  <RefreshCw className="h-3.5 w-3.5 shrink-0 animate-spin text-queued" />
                                ) : tool.status === "completed" ? (
                                  <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-applied" />
                                ) : (
                                  <X className="h-3.5 w-3.5 shrink-0 text-failed" />
                                )}
                                <span className="shrink-0 font-mono text-[12px] font-medium text-ink-200">
                                  {tool.name}
                                </span>
                                {/* Readable summary of the arguments rather than a raw
                                    JSON.stringify blob spilling across the bubble. */}
                                {tool.args && Object.keys(tool.args).length > 0 && (
                                  <span className="truncate text-[12px] text-ink-500">
                                    {Object.entries(tool.args)
                                      .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
                                      .join(" · ")}
                                  </span>
                                )}
                              </div>
                              <span className="label-micro shrink-0">{tool.status}</span>
                            </div>

                            {/* Structured view of the result: chart, meter or table
                                depending on what the tool actually returned. */}
                            {tool.status === "completed" && (
                              <ToolResultCard tool={tool.name} result={tool.result} />
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )
            })
          )}
          <div ref={messagesEndRef} />
          </div>
        </div>

        {/* Composer, with the write ledger docked directly above it — the two
            belong together: you make a change here and watch it land there. */}
        <footer className="border-t border-[var(--color-rule)] bg-ink-950 p-4 sm:px-6">
          <div className="mx-auto w-full max-w-4xl space-y-3">
            <WriteLedger
              entries={ledger}
              onDismiss={() => setDismissed(new Set(ledger.map((l) => l.jobId)))}
            />

            <form onSubmit={handleSend} className="flex items-center gap-2">
              <input
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder={
                  isConnected
                    ? "Ask about this sheet, or describe a change…"
                    : "Reconnecting…"
                }
                disabled={!isConnected}
                className="field flex-1 py-3"
              />
              <button
                type="submit"
                disabled={!isConnected || !input.trim()}
                aria-label="Send"
                className="btn btn-primary px-3.5 py-3"
              >
                <Send className="h-4 w-4" />
              </button>
            </form>
          </div>
        </footer>
      </div>
    </div>
  )
}

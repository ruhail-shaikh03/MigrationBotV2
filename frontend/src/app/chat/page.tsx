"use client"

import { useSession, signOut } from "next-auth/react"
import { useRouter } from "next/navigation"
import { useEffect, useState, useRef } from "react"
import { useChatStore, Project, Message } from "@/store/useChatStore"
import { useWebSocket } from "@/hooks/useWebSocket"
import { 
  Send, Database, Users, History, LogOut, ArrowLeft, 
  Settings, RefreshCw, Circle, CheckCircle2, AlertTriangle, 
  Layers, Play, Check, X, Bell
} from "lucide-react"

interface Toast {
  id: string
  message: string
  type: "success" | "info" | "error"
}

export default function ChatPage() {
  const { data: session, status } = useSession()
  const router = useRouter()
  
  // Local UI States
  const [input, setInput] = useState("")
  const [toasts, setToasts] = useState<Toast[]>([])
  const [isAdminState, setIsAdminState] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement | null>(null)
  
  // Zustand Store
  const { 
    projects, setProjects,
    activeProject, setActiveProject,
    activeTab, setActiveTab,
    messages, setMessages,
    isConnected, clearChat
  } = useChatStore()

  const apiToken = (session as any)?.apiToken || null
  const googleToken = (session as any)?.googleAccessToken || null

  // Instantiate WebSocket
  const { sendMessage } = useWebSocket(apiToken, activeProject?.id || null)

  // Fetch user profile to check admin status dynamically
  useEffect(() => {
    if (status === "authenticated" && apiToken) {
      fetch(`/api/auth/me`, {
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

  // Listen to background queue writes notifications
  useEffect(() => {
    const handleQueueUpdate = (e: Event) => {
      const data = (e as CustomEvent).detail
      const job_id = data.job_id
      const jobStatus = data.status // queued, processing, completed, failed
      const tool = data.tool_name
      const args = data.args || {}
      const targetId = args.ricefw_id || "Item"

      if (jobStatus === "completed") {
        addToast(`✅ Background write completed: ${tool} succeeded for ${targetId}!`, "success")
      } else if (jobStatus === "failed") {
        addToast(`❌ Background write failed: ${tool} for ${targetId} encountered an error.`, "error")
      } else {
        addToast(`⏳ Queue Update: Write job for ${targetId} is ${jobStatus}.`, "info")
      }
    }

    window.addEventListener("queue_update", handleQueueUpdate)
    return () => {
      window.removeEventListener("queue_update", handleQueueUpdate)
    }
  }, [])

  // Toast Helpers
  const addToast = (message: string, type: "success" | "info" | "error" = "info") => {
    const id = Math.random().toString(36).substring(7)
    setToasts((prev) => [...prev, { id, message, type }])
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id))
    }, 5000)
  }

  // Handle message send
  const handleSend = (e: React.FormEvent) => {
    e.preventDefault()
    if (!input.trim() || !isConnected) return
    sendMessage(input.trim())
    setInput("")
  }

  // Switch Module Tab in Agent Context
  const handleTabChange = (tabName: string) => {
    if (!isConnected) return
    setActiveTab(tabName)
    // Send a command to the LLM agent to invoke switch_module
    sendMessage(`Switch active module to ${tabName}`)
  }

  if (status === "loading" || !session) {
    return (
      <div className="flex h-screen items-center justify-center bg-[#030014]">
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-indigo-500"></div>
      </div>
    )
  }

  const email = session?.user?.email
  const isAdmin = isAdminState || (email ? ["rohai", "ruhail", "admin"].some(adminKey => email.includes(adminKey)) : false)

  return (
    <div className="flex h-screen bg-[#030014] text-zinc-100 overflow-hidden font-sans relative">
      {/* Background blobs */}
      <div className="absolute top-0 right-1/4 -z-10 h-[500px] w-[500px] rounded-full bg-indigo-950/20 blur-3xl"></div>
      <div className="absolute bottom-0 left-1/4 -z-10 h-[500px] w-[500px] rounded-full bg-purple-950/20 blur-3xl"></div>

      {/* Main chat window container */}
      <div className="flex flex-1 flex-col relative z-10">
        
        {/* Header Bar */}
        <header className="glass-panel px-6 py-4 flex items-center justify-between border-b border-white/5">
          <div className="flex items-center gap-6">
            <div className="flex items-center gap-3">
              <div className="h-8 w-8 rounded-lg bg-indigo-600 flex items-center justify-center text-white font-extrabold text-lg">
                M
              </div>
              <span className="font-bold text-lg tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-zinc-100 to-indigo-200">
                MigrationBot Chat
              </span>
            </div>
            
            {/* Project Dropdown Select */}
            <div className="flex items-center gap-2">
              <span className="text-xs text-zinc-400">Project:</span>
              <select
                value={activeProject?.id || ""}
                onChange={(e) => {
                  const proj = projects.find((p) => p.id === parseInt(e.target.value))
                  if (proj) setActiveProject(proj)
                }}
                className="bg-[#120e2e] border border-white/10 rounded-lg px-3 py-1.5 text-sm font-medium text-zinc-200 focus:outline-none focus:border-indigo-500 transition cursor-pointer"
              >
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.project_name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Module Tabs (SD, MM, FI, etc.) */}
          <div className="hidden md:flex items-center gap-1.5 bg-[#120e2e]/60 p-1 rounded-xl border border-white/5">
            {(activeProject?.schema_config?.tabs 
              ? Object.keys(activeProject.schema_config.tabs) 
              : (activeProject?.schema_config?.global?.valid_modules || ["SD", "MM", "FI", "CO", "PP", "QM"])
            ).map((tab: string) => (
              <button
                key={tab}
                onClick={() => handleTabChange(tab)}
                disabled={!isConnected}
                className={`px-4 py-1.5 rounded-lg text-xs font-semibold tracking-wide transition cursor-pointer ${
                  activeTab === tab 
                    ? "bg-indigo-600 text-white shadow-md"
                    : "text-zinc-400 hover:text-zinc-200 hover:bg-white/5"
                } disabled:opacity-50`}
              >
                {tab}
              </button>
            ))}
          </div>

          {/* User Profile & Navigation */}
          <div className="flex items-center gap-4">
            {/* WS Connection Status Dot */}
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-white/5 border border-white/5">
              <span className="relative flex h-2 w-2">
                {isConnected ? (
                  <>
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                    <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
                  </>
                ) : (
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-rose-500"></span>
                )}
              </span>
              <span className="text-xs font-medium text-zinc-400">
                {isConnected ? "Live" : "Offline"}
              </span>
            </div>

            {/* Admin link if user is administrator */}
            {isAdmin && (
              <button
                onClick={() => router.push("/admin")}
                className="p-2 rounded-lg bg-indigo-600/10 text-indigo-400 border border-indigo-500/20 hover:bg-indigo-600/20 transition cursor-pointer"
                title="Admin Dashboard"
              >
                <Settings className="h-4.5 w-4.5" />
              </button>
            )}

            {/* Sign Out Button */}
            <button
              onClick={() => signOut({ callbackUrl: "/" })}
              className="p-2 rounded-lg bg-zinc-800/40 text-zinc-400 border border-white/5 hover:bg-zinc-800/80 hover:text-rose-400 transition cursor-pointer"
              title="Sign Out"
            >
              <LogOut className="h-4.5 w-4.5" />
            </button>
          </div>
        </header>

        {/* Messages Feed */}
        <div className="flex-1 overflow-y-auto px-6 py-8 space-y-6">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-center max-w-lg mx-auto space-y-4">
              <div className="p-4 bg-indigo-600/10 text-indigo-400 rounded-full border border-indigo-500/20 animate-pulse-slow">
                <Database className="h-10 w-10" />
              </div>
              <h3 className="text-lg font-bold text-zinc-200">How can I help you switch or update S/4HANA migrations today?</h3>
              <p className="text-sm text-zinc-500 leading-relaxed">
                You can ask questions like "Show SD tracker overview", "Switch module to MM", "Verify data completeness", or perform updates like "Set Dev Status to Done for RICEFW SD-045".
              </p>
            </div>
          ) : (
            messages.map((msg) => {
              if (msg.role === "system") {
                return (
                  <div key={msg.id} className="flex justify-center animate-slide-up">
                    <div className="bg-rose-500/10 border border-rose-500/20 text-rose-400 text-xs px-4 py-2 rounded-lg flex items-center gap-2">
                      <AlertTriangle className="h-4 w-4" />
                      <span>{msg.content}</span>
                    </div>
                  </div>
                )
              }

              const isUser = msg.role === "user"

              return (
                <div 
                  key={msg.id} 
                  className={`flex w-full ${isUser ? "justify-end" : "justify-start"} animate-slide-up`}
                >
                  <div className={`max-w-[80%] flex flex-col space-y-2`}>
                    
                    {/* Message Bubble */}
                    <div 
                      className={`px-5 py-3.5 shadow-md ${
                        isUser ? "chat-bubble-user" : "chat-bubble-agent"
                      }`}
                    >
                      {/* Message Content */}
                      <p className="text-[15px] leading-relaxed whitespace-pre-wrap select-text">
                        {msg.content}
                      </p>
                      
                      {/* Streaming cursor if empty assistant bubble */}
                      {!isUser && msg.content === "" && (!msg.toolCalls || msg.toolCalls.length === 0) && (
                        <div className="flex space-x-1 items-center h-5">
                          <div className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-bounce"></div>
                          <div className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-bounce [animation-delay:0.2s]"></div>
                          <div className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-bounce [animation-delay:0.4s]"></div>
                        </div>
                      )}
                    </div>

                    {/* Tool Calls Visualizer */}
                    {!isUser && msg.toolCalls && msg.toolCalls.length > 0 && (
                      <div className="space-y-1.5">
                        {msg.toolCalls.map((tool, idx) => (
                          <div 
                            key={idx}
                            className="glass-card px-4 py-2 rounded-xl text-xs flex items-center justify-between border border-white/5"
                          >
                            <div className="flex items-center gap-2.5">
                              {tool.status === "running" ? (
                                <RefreshCw className="h-3.5 w-3.5 text-amber-400 animate-spin" />
                              ) : tool.status === "completed" ? (
                                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
                              ) : (
                                <X className="h-3.5 w-3.5 text-rose-500" />
                              )}
                              <span className="font-semibold text-zinc-300">
                                Tool: {tool.name}
                              </span>
                              {tool.args && Object.keys(tool.args).length > 0 && (
                                <span className="text-zinc-500 font-mono text-[10px]">
                                  ({JSON.stringify(tool.args)})
                                </span>
                              )}
                            </div>
                            <span className="text-zinc-400 text-[10px] uppercase font-bold tracking-wider">
                              {tool.status}
                            </span>
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

        {/* Input Bar */}
        <footer className="p-6 bg-[#030014] border-t border-white/5">
          <form onSubmit={handleSend} className="max-w-4xl mx-auto flex items-center gap-3 relative">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={isConnected ? "Ask MigrationBot or update a sheet cell..." : "Waiting for WebSocket connection..."}
              disabled={!isConnected}
              className="flex-1 bg-[#120e2e] border border-white/10 rounded-xl px-5 py-4 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition disabled:opacity-50"
            />
            <button
              type="submit"
              disabled={!isConnected || !input.trim()}
              className="bg-indigo-600 text-white p-4 rounded-xl hover:bg-indigo-500 active:scale-95 transition disabled:opacity-40 disabled:scale-100 cursor-pointer shadow-lg"
            >
              <Send className="h-4.5 w-4.5" />
            </button>
          </form>
        </footer>

      </div>

      {/* Floating Notifications / Toasts container */}
      <div className="fixed bottom-6 right-6 z-50 flex flex-col gap-2 max-w-sm">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`glass-panel p-4 rounded-xl shadow-lg border border-white/10 flex items-start gap-3 animate-slide-up ${
              toast.type === "success" 
                ? "border-emerald-500/30" 
                : toast.type === "error" 
                ? "border-rose-500/30" 
                : "border-indigo-500/30"
            }`}
          >
            <Bell className={`h-5 w-5 ${
              toast.type === "success" 
                ? "text-emerald-400" 
                : toast.type === "error" 
                ? "text-rose-400" 
                : "text-indigo-400"
            }`} />
            <div className="flex-1 text-xs font-medium text-zinc-300">
              {toast.message}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

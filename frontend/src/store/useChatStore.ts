import { create } from "zustand"

export interface Project {
  id: number
  project_name: string
  spreadsheet_id: string
  default_tab: string
  company_prefix: string
  is_active: boolean
  schema_config: any
  created_at: string
}

export interface Message {
  id: string
  role: "user" | "assistant" | "system"
  content: string
  timestamp: Date
  toolCalls?: Array<{
    name: string
    args: any
    status: "running" | "completed" | "failed"
    result?: any
  }>
}

interface ChatStore {
  projects: Project[]
  activeProject: Project | null
  activeTab: string
  isConnected: boolean
  messages: Message[]
  ws: WebSocket | null
  sessionUserEmail: string | null
  sessionProjectName: string | null

  setProjects: (projects: Project[]) => void
  setActiveProject: (project: Project | null) => void
  setActiveTab: (tab: string) => void
  setIsConnected: (connected: boolean) => void
  setMessages: (messages: Message[]) => void
  addMessage: (message: Message) => void
  updateLastMessage: (updater: (msg: Message) => Message, targetId?: string, targetRole?: Message["role"]) => void
  setWs: (ws: WebSocket | null) => void
  setSessionInfo: (info: { userEmail?: string; projectName?: string; activeTab?: string }) => void
  clearChat: () => void
}

export const useChatStore = create<ChatStore>((set) => ({
  projects: [],
  activeProject: null,
  activeTab: "",
  isConnected: false,
  messages: [],
  ws: null,
  sessionUserEmail: null,
  sessionProjectName: null,

  setProjects: (projects) => set({ projects }),
  setActiveProject: (project) => set({
    activeProject: project,
    activeTab: project ? (project.default_tab || "") : ""
  }),
  setActiveTab: (activeTab) => set({ activeTab }),
  setIsConnected: (connected) => set({ isConnected: connected }),
  setMessages: (messages) => set({ messages }),
  addMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),
  updateLastMessage: (updater, targetId, targetRole) => set((state) => {
    if (state.messages.length === 0) return {}
    let idx = -1
    if (targetId) {
      idx = state.messages.findIndex((m) => m.id === targetId)
    } else if (targetRole) {
      for (let i = state.messages.length - 1; i >= 0; i--) {
        if (state.messages[i].role === targetRole) {
          idx = i
          break
        }
      }
    } else {
      idx = state.messages.length - 1
    }
    if (idx === -1) return {}
    const newMessages = [...state.messages]
    newMessages[idx] = updater(newMessages[idx])
    return { messages: newMessages }
  }),
  setWs: (ws) => set({ ws }),
  setSessionInfo: (info) => set((state) => ({
    sessionUserEmail: info.userEmail ?? state.sessionUserEmail,
    sessionProjectName: info.projectName ?? state.sessionProjectName,
    activeTab: info.activeTab ?? state.activeTab
  })),
  clearChat: () => set({ messages: [] })
}))

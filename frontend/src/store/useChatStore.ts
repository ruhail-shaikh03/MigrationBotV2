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
  
  setProjects: (projects: Project[]) => void
  setActiveProject: (project: Project | null) => void
  setActiveTab: (tab: string) => void
  setIsConnected: (connected: boolean) => void
  setMessages: (messages: Message[]) => void
  addMessage: (message: Message) => void
  updateLastMessage: (updater: (msg: Message) => Message) => void
  setWs: (ws: WebSocket | null) => void
  clearChat: () => void
}

export const useChatStore = create<ChatStore>((set) => ({
  projects: [],
  activeProject: null,
  activeTab: "",
  isConnected: false,
  messages: [],
  ws: null,

  setProjects: (projects) => set({ projects }),
  setActiveProject: (project) => set({
    activeProject: project,
    activeTab: project ? (project.default_tab || "") : ""
  }),
  setActiveTab: (activeTab) => set({ activeTab }),
  setIsConnected: (connected) => set({ isConnected: connected }),
  setMessages: (messages) => set({ messages }),
  addMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),
  updateLastMessage: (updater) => set((state) => {
    if (state.messages.length === 0) return {}
    const newMessages = [...state.messages]
    const idx = newMessages.length - 1
    newMessages[idx] = updater(newMessages[idx])
    return { messages: newMessages }
  }),
  setWs: (ws) => set({ ws }),
  clearChat: () => set({ messages: [] })
}))

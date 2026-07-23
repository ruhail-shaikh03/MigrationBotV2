import { useEffect, useRef, useCallback } from "react"
import { useChatStore } from "@/store/useChatStore"

export function useWebSocket(apiToken: string | null, projectId: number | null) {
  const { isConnected, setIsConnected, addMessage, updateLastMessage, setWs, ws, setActiveTab } = useChatStore()
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null)
  const pingIntervalRef = useRef<NodeJS.Timeout | null>(null)

  const connect = useCallback(() => {
    if (!apiToken) return

    // Close any previous instances
    if (ws) {
      try {
        ws.close()
      } catch (e) {
        console.error(e)
      }
    }

    const protocol = typeof window !== 'undefined' && window.location.protocol === "https:" ? "wss:" : "ws:"
    const host = typeof window !== 'undefined' ? window.location.host : "localhost:3000"
    const wsBaseUrl = process.env.NEXT_PUBLIC_WS_URL || `${protocol}//${host}/ws`
    const url = `${wsBaseUrl}?token=${encodeURIComponent(apiToken)}${projectId ? `&project_id=${projectId}` : ""}`
    
    console.log("Connecting to WebSocket:", wsBaseUrl)
    const socket = new WebSocket(url)

    socket.onopen = () => {
      console.log("WebSocket connected successfully")
      setIsConnected(true)
      
      // Start ping heartbeat to keep connections open
      if (pingIntervalRef.current) clearInterval(pingIntervalRef.current)
      pingIntervalRef.current = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "ping" }))
        }
      }, 30000)
    }

    socket.onclose = (event) => {
      console.log("WebSocket closed:", event)
      setIsConnected(false)
      setWs(null)
      
      if (pingIntervalRef.current) {
        clearInterval(pingIntervalRef.current)
      }

      // Reconnect on unexpected disconnects (exclude authentication/clean close errors)
      if (event.code !== 1008 && event.code !== 1000) {
        if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current)
        reconnectTimeoutRef.current = setTimeout(() => {
          connect()
        }, 3000)
      }
    }

    socket.onerror = (error) => {
      console.error("WebSocket error:", error)
    }

    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        
        switch (data.type) {
          case "assistant": {
            const content = data.content || ""
            
            updateLastMessage((lastMsg) => {
              if (lastMsg && lastMsg.role === "assistant") {
                return {
                  ...lastMsg,
                  content: lastMsg.content + content
                }
              }
              return lastMsg
            })
            break
          }
          case "tool_start": {
            updateLastMessage((lastMsg) => {
              if (lastMsg && lastMsg.role === "assistant") {
                const toolCalls = lastMsg.toolCalls || []
                return {
                  ...lastMsg,
                  toolCalls: [
                    ...toolCalls,
                    { name: data.tool, args: data.args, status: "running" }
                  ]
                }
              }
              return lastMsg
            })
            break
          }
          case "tool_result": {
            updateLastMessage((lastMsg) => {
              if (lastMsg && lastMsg.role === "assistant" && lastMsg.toolCalls) {
                return {
                  ...lastMsg,
                  toolCalls: lastMsg.toolCalls.map((tc) => 
                    tc.name === data.tool && tc.status === "running"
                      ? { ...tc, status: "completed", result: data.result }
                      : tc
                  )
                }
              }
              return lastMsg
            })
            break
          }
          case "queue_update": {
            const queueEvent = new CustomEvent("queue_update", { detail: data })
            window.dispatchEvent(queueEvent)
            break
          }
          case "error": {
            const errorContent = `Error: ${data.message}`
            const currentMessages = useChatStore.getState().messages
            const lastMsg = currentMessages[currentMessages.length - 1]
            if (lastMsg && lastMsg.content === errorContent) {
              break
            }
            addMessage({
              id: Math.random().toString(36).substring(7),
              role: "system",
              content: errorContent,
              timestamp: new Date()
            })
            break
          }
          case "connection_ok": {
            if (data.active_tab) {
              setActiveTab(data.active_tab)
            }
            break
          }
          case "pong":
            break
          default:
            break
        }
      } catch (err) {
        console.error("Failed to parse WebSocket message:", err)
      }
    }

    setWs(socket)
  }, [apiToken, projectId, setIsConnected, setWs, addMessage, updateLastMessage])

  useEffect(() => {
    connect()

    return () => {
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current)
      if (pingIntervalRef.current) clearInterval(pingIntervalRef.current)
    }
  }, [apiToken, projectId, connect])

  const sendMessage = useCallback((content: string) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      addMessage({
        id: Math.random().toString(36).substring(7),
        role: "user",
        content,
        timestamp: new Date()
      })
      addMessage({
        id: Math.random().toString(36).substring(7),
        role: "assistant",
        content: "",
        timestamp: new Date(),
        toolCalls: []
      })
      ws.send(JSON.stringify({ type: "message", content }))
    } else {
      console.warn("WebSocket is not open.")
    }
  }, [ws, addMessage])

  return { sendMessage, isConnected }
}

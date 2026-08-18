import { useEffect, useRef, useCallback } from "react"
import { useChatStore, type Message } from "@/store/useChatStore"

/**
 * Detach every handler, then close cleanly with code 1000.
 *
 * Both halves matter. A socket we close deliberately must not be able to schedule a
 * reconnect: `onclose` below retries on any code other than 1000/1008, and a bare
 * `socket.close()` reports 1005 ("no status received"), which passes that guard. So
 * replacing a socket used to fire the *old* socket's `onclose`, which queued a
 * reconnect 3s later, which closed the healthy socket that had just replaced it —
 * a self-sustaining 3-second flap that reset `isConnected` (and, server-side, the
 * per-connection `message_history` in chat.py) on every cycle.
 *
 * Nulling the handlers also stops a superseded socket from writing store state for a
 * connection that no longer exists: `onclose` fires asynchronously, so it could
 * otherwise land *after* the replacement's `onopen` and stomp `isConnected` back to
 * false, leaving the composer disabled with a live socket open.
 */
function teardownSocket(socket: WebSocket | null): void {
  if (!socket) return
  socket.onopen = null
  socket.onclose = null
  socket.onerror = null
  socket.onmessage = null
  try {
    socket.close(1000, "client closing")
  } catch (e) {
    console.error(e)
  }
}

export function useWebSocket(apiToken: string | null, projectId: number | null) {
  const { isConnected, setIsConnected, addMessage, updateLastMessage, setMessages, setWs, setActiveTab, setSessionInfo } = useChatStore()
  // The canonical "current socket" reference. Read/write this, not the Zustand `ws` state,
  // for anything that must see the actual current socket regardless of when its closure was
  // created — `ws` was previously read inside `connect` without being a dependency of it, so
  // reconnects (from onclose's setTimeout, or an effect re-run) could close a stale reference.
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null)
  const pingIntervalRef = useRef<NodeJS.Timeout | null>(null)

  const connect = useCallback(() => {
    if (!apiToken) return

    // Drop any pending reconnect/heartbeat before standing up a new socket, so a timer
    // queued by an earlier connection can't fire against this one.
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current)
      reconnectTimeoutRef.current = null
    }
    if (pingIntervalRef.current) {
      clearInterval(pingIntervalRef.current)
      pingIntervalRef.current = null
    }

    // Close any previous instance — handlers first, see teardownSocket.
    teardownSocket(wsRef.current)
    wsRef.current = null

    const protocol = typeof window !== 'undefined' && window.location.protocol === "https:" ? "wss:" : "ws:"
    const host = typeof window !== 'undefined' ? window.location.host : "localhost:3000"
    const wsBaseUrl = process.env.NEXT_PUBLIC_WS_URL || `${protocol}//${host}/ws`
    const url = `${wsBaseUrl}?token=${encodeURIComponent(apiToken)}${projectId ? `&project_id=${projectId}` : ""}`

    console.log("Connecting to WebSocket:", wsBaseUrl)
    const socket = new WebSocket(url)
    // Claim the ref before attaching handlers so the `wsRef.current !== socket` guards
    // below can tell "this is the live socket" from "this one was already replaced".
    wsRef.current = socket

    socket.onopen = () => {
      if (wsRef.current !== socket) return
      console.log("WebSocket connected successfully")
      setIsConnected(true)
      setWs(socket)

      // Start ping heartbeat to keep connections open
      if (pingIntervalRef.current) clearInterval(pingIntervalRef.current)
      pingIntervalRef.current = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "ping" }))
        }
      }, 30000)
    }

    socket.onclose = (event) => {
      // A socket that's already been replaced must not touch shared state or queue a
      // reconnect on the live one's behalf.
      if (wsRef.current !== socket) return
      wsRef.current = null

      console.log("WebSocket closed:", event)
      setIsConnected(false)
      setWs(null)

      if (pingIntervalRef.current) {
        clearInterval(pingIntervalRef.current)
        pingIntervalRef.current = null
      }

      // Reconnect only on genuinely unexpected drops. 1000 means we closed it ourselves
      // (teardownSocket, or the effect cleanup on unmount) and a retry would fight the
      // caller that asked for the close; 1008 is the server rejecting auth
      // (chat.py:websocket_chat_endpoint), where retrying just loops on the same token.
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
                  // `reset` empties the bubble instead of appending to it. It arrives when
                  // the server caught the reasoner leaking its internal markup partway
                  // through a streamed reply and is about to retry on another model — the
                  // text already on screen has to go, or the good answer gets stapled to
                  // the tail of the poisoned one.
                  content: data.reset ? "" : lastMsg.content + content
                }
              }
              return lastMsg
            }, undefined, "assistant")
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
            }, undefined, "assistant")
            break
          }
          case "tool_result": {
            const succeeded = data.result?.ok !== false
            updateLastMessage((lastMsg) => {
              if (lastMsg && lastMsg.role === "assistant" && lastMsg.toolCalls) {
                return {
                  ...lastMsg,
                  toolCalls: lastMsg.toolCalls.map((tc) =>
                    tc.name === data.tool && tc.status === "running"
                      ? { ...tc, status: succeeded ? "completed" : "failed", result: data.result }
                      : tc
                  )
                }
              }
              return lastMsg
            }, undefined, "assistant")
            break
          }
          case "queue_update": {
            const queueEvent = new CustomEvent("queue_update", { detail: data })
            window.dispatchEvent(queueEvent)
            break
          }
          case "tab_switched": {
            // Server-confirmed tab switch (see switchTab below) — the client no longer
            // guesses the new active tab optimistically, it waits for this frame.
            setActiveTab(data.active_tab)
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
          case "history": {
          // Applied only into an empty store, which is precisely the reload case.
          //
          // The store is created at module scope, so it survives a socket drop: after the
          // 3s reconnect the browser still holds the whole conversation, including any
          // message sent but not yet answered — and that one is not persisted yet. Writing
          // the server's copy over it would delete exactly the message the user is waiting
          // on. An empty store, by contrast, can only mean a fresh page load.
          if (useChatStore.getState().messages.length > 0) break
          const restored = (data.messages || []).map((m: {
            role: "user" | "assistant" | "system"
            content: string
            toolCalls?: Message["toolCalls"]
          }, i: number) => ({
            id: `history-${i}`,
            role: m.role,
            content: m.content,
            // The sheet timestamps nothing, and inventing per-message times would put a
            // clock on the replay that the record cannot support. One time for the batch.
            timestamp: new Date(),
            toolCalls: m.toolCalls,
          }))
          if (restored.length > 0) setMessages(restored)
          break
        }
        case "connection_ok": {
            setSessionInfo({
              userEmail: data.user_email,
              projectName: data.project_name,
              activeTab: data.active_tab
            })
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

    // wsRef/setWs are assigned above (ref) and in onopen (store), not here — publishing
    // a still-CONNECTING socket to the store let callers send on a socket that wasn't
    // open yet.
  }, [apiToken, projectId, setIsConnected, setWs, addMessage, updateLastMessage, setMessages, setActiveTab, setSessionInfo])

  // `connect` already closes over apiToken/projectId, so depending on it alone covers
  // a token or project change without re-running the effect twice for one change.
  useEffect(() => {
    connect()

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
        reconnectTimeoutRef.current = null
      }
      if (pingIntervalRef.current) {
        clearInterval(pingIntervalRef.current)
        pingIntervalRef.current = null
      }
      // Previously the socket itself was left open here, so unmounting (or navigating
      // away from /chat) leaked a live connection that kept receiving frames.
      teardownSocket(wsRef.current)
      wsRef.current = null
      setIsConnected(false)
      setWs(null)
    }
  }, [connect, setIsConnected, setWs])

  const sendMessage = useCallback((content: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
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
      wsRef.current.send(JSON.stringify({ type: "message", content }))
    } else {
      console.warn("WebSocket is not open.")
    }
  }, [addMessage])

  const switchTab = useCallback((tabName: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "switch_tab", tab_name: tabName }))
    } else {
      console.warn("WebSocket is not open.")
    }
  }, [])

  return { sendMessage, switchTab, isConnected }
}

"use client"

import { signIn, useSession } from "next-auth/react"
import { useRouter } from "next/navigation"
import { useEffect } from "react"
import { ShieldCheck, MessageSquareCode, ArrowRight, Zap, RefreshCcw } from "lucide-react"

export default function Home() {
  const { data: session, status } = useSession()
  const router = useRouter()

  useEffect(() => {
    if (status === "authenticated") {
      router.push("/chat")
    }
  }, [status, router])

  if (status === "loading") {
    return (
      <div className="flex h-screen items-center justify-center bg-[#030014]">
        <div className="relative flex items-center justify-center">
          <div className="h-16 w-16 animate-spin rounded-full border-t-2 border-b-2 border-indigo-500"></div>
          <div className="absolute h-10 w-10 animate-ping rounded-full border border-purple-500 opacity-75"></div>
        </div>
      </div>
    )
  }

  return (
    <div className="relative min-h-screen flex flex-col items-center justify-center overflow-hidden px-4">
      {/* Background Glow Blobs */}
      <div className="absolute top-1/4 left-1/4 -z-10 h-96 w-96 rounded-full bg-indigo-600/20 blur-3xl animate-blob"></div>
      <div className="absolute bottom-1/4 right-1/4 -z-10 h-96 w-96 rounded-full bg-purple-600/20 blur-3xl animate-blob animation-delay-2000"></div>
      <div className="absolute top-1/2 right-1/3 -z-10 h-80 w-80 rounded-full bg-cyan-600/10 blur-3xl animate-blob animation-delay-4000"></div>

      {/* Grid Overlay */}
      <div className="absolute inset-0 -z-20 bg-[linear-gradient(to_right,#ffffff03_1px,transparent_1px),linear-gradient(to_bottom,#ffffff03_1px,transparent_1px)] bg-[size:4rem_4rem]"></div>

      {/* Main Content Area */}
      <main className="w-full max-w-4xl text-center space-y-12">
        <div className="space-y-6">
          {/* Tag */}
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full border border-indigo-500/30 bg-indigo-500/10 text-indigo-300 text-sm font-medium animate-pulse-slow">
            <Zap className="h-4 w-4" />
            <span>SAP S/4HANA WRICEF Automation</span>
          </div>

          <h1 className="text-5xl md:text-7xl font-extrabold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-zinc-50 via-indigo-200 to-purple-400">
            MigrationBot <span className="text-indigo-400">V2</span>
          </h1>

          <p className="max-w-2xl mx-auto text-lg md:text-xl text-zinc-400 leading-relaxed">
            Interact with your S/4HANA migration sheets through conversational AI. Auto-mapped schema configurations, asynchronous task workers, and real-time execution audits.
          </p>
        </div>

        {/* Action card */}
        <div className="glass-panel max-w-md mx-auto p-8 rounded-2xl shadow-2xl relative group">
          <div className="absolute -inset-0.5 rounded-2xl bg-gradient-to-r from-indigo-500 to-purple-500 opacity-20 blur group-hover:opacity-30 transition duration-1000"></div>
          
          <div className="relative space-y-6">
            <h2 className="text-xl font-bold text-zinc-100">Welcome Portal</h2>
            <p className="text-sm text-zinc-400">
              Sign in with your Google Workspace account to read, switch modules, and enqueue updates to live spreadsheets.
            </p>

            <button
              onClick={() => signIn("google")}
              className="w-full flex items-center justify-center gap-3 bg-white text-zinc-950 font-semibold py-3 px-6 rounded-xl hover:bg-zinc-200 active:scale-98 transition duration-200 cursor-pointer shadow-lg"
            >
              {/* Google SVG Icon */}
              <svg className="h-5 w-5" viewBox="0 0 24 24">
                <path
                  fill="#4285F4"
                  d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
                />
                <path
                  fill="#34A853"
                  d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
                />
                <path
                  fill="#FBBC05"
                  d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"
                />
                <path
                  fill="#EA4335"
                  d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
                />
              </svg>
              Sign in with Google
            </button>
          </div>
        </div>

        {/* Core Pillars */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 max-w-4xl mx-auto pt-8">
          <div className="glass-card p-6 rounded-xl text-left space-y-3">
            <div className="inline-flex p-3 rounded-lg bg-indigo-500/10 text-indigo-400">
              <MessageSquareCode className="h-5 w-5" />
            </div>
            <h3 className="font-semibold text-zinc-200">AI-Agentic Actions</h3>
            <p className="text-sm text-zinc-400">
              Conversational prompts are translated directly to column mappings, filters, and dynamic queries.
            </p>
          </div>

          <div className="glass-card p-6 rounded-xl text-left space-y-3">
            <div className="inline-flex p-3 rounded-lg bg-cyan-500/10 text-cyan-400">
              <RefreshCcw className="h-5 w-5 animate-pulse-slow" />
            </div>
            <h3 className="font-semibold text-zinc-200">Queue-Throttled Writes</h3>
            <p className="text-sm text-zinc-400">
              Database operations and spreadsheet modifications are safely enqueued to avoid API quota limits.
            </p>
          </div>

          <div className="glass-card p-6 rounded-xl text-left space-y-3">
            <div className="inline-flex p-3 rounded-lg bg-purple-500/10 text-purple-400">
              <ShieldCheck className="h-5 w-5" />
            </div>
            <h3 className="font-semibold text-zinc-200">RBAC & Auditing</h3>
            <p className="text-sm text-zinc-400">
              Granular project authorization blocks unauthorized operations, logging all mutations asynchronously.
            </p>
          </div>
        </div>
      </main>

      {/* Footer */}
      <footer className="mt-20 text-zinc-500 text-xs py-4 border-t border-white/5 w-full text-center">
        &copy; {new Date().getFullYear()} MigrationBot V2. All rights reserved.
      </footer>
    </div>
  )
}

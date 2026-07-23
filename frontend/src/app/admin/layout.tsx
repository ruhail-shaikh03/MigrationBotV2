"use client"

import { useSession } from "next-auth/react"
import { useRouter, usePathname } from "next/navigation"
import { useEffect, useState } from "react"
import { 
  LayoutDashboard, FolderKanban, Users, ShieldAlert, 
  MessageSquare, LogOut, ArrowLeft, Database, ShieldX, RefreshCw
} from "lucide-react"
import Link from "next/link"

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const { data: session, status } = useSession()
  const router = useRouter()
  const pathname = usePathname()
  const [adminStatus, setAdminStatus] = useState<"loading" | "authorized" | "denied">("loading")

  const checkAdminStatus = () => {
    if (status === "unauthenticated") {
      router.push("/")
      return
    }
    if (status === "authenticated") {
      const apiToken = (session as any)?.apiToken
      if (!apiToken) {
        setAdminStatus("loading")
        return
      }
      fetch("/api/me", {
        headers: { Authorization: `Bearer ${apiToken}` }
      })
        .then((res) => {
          if (!res.ok) throw new Error("Auth check failed")
          return res.json()
        })
        .then((data) => {
          if (data && data.is_admin) {
            setAdminStatus("authorized")
          } else {
            setAdminStatus("denied")
          }
        })
        .catch((err) => {
          console.error("Admin verification error:", err)
          setAdminStatus("denied")
        })
    }
  }

  useEffect(() => {
    checkAdminStatus()
  }, [status, session])

  if (status === "loading" || adminStatus === "loading") {
    return (
      <div className="flex h-screen items-center justify-center bg-[#030014]">
        <div className="flex flex-col items-center gap-3">
          <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-indigo-500"></div>
          <span className="text-xs text-zinc-400 font-mono">Verifying admin privileges...</span>
        </div>
      </div>
    )
  }

  if (adminStatus === "denied") {
    const userEmail = session?.user?.email || "Unknown user"
    return (
      <div className="flex h-screen items-center justify-center bg-[#030014] text-zinc-100 p-6">
        <div className="glass-panel max-w-md w-full p-8 rounded-2xl border border-rose-500/20 text-center space-y-6">
          <div className="h-16 w-16 bg-rose-500/10 border border-rose-500/30 text-rose-400 rounded-2xl flex items-center justify-center mx-auto">
            <ShieldX className="h-8 w-8" />
          </div>
          <div>
            <h2 className="text-xl font-bold text-white mb-2">Admin Access Required</h2>
            <p className="text-xs text-zinc-400 leading-relaxed">
              Logged in as <code className="text-indigo-300 font-mono bg-white/5 px-2 py-0.5 rounded">{userEmail}</code>.
              This email is not listed in the server's <code className="text-zinc-300">ADMIN_EMAILS</code> configuration.
            </p>
          </div>

          <div className="bg-white/[0.02] border border-white/5 p-4 rounded-xl text-left text-[11px] text-zinc-400 space-y-1.5 font-mono">
            <div className="font-semibold text-zinc-300">How to grant access:</div>
            <div>Add <code className="text-indigo-300">{userEmail}</code> to <code className="text-indigo-300">ADMIN_EMAILS</code> in your VPS <code className="text-indigo-300">.env</code> file:</div>
            <div className="text-zinc-500 bg-black/40 p-2 rounded overflow-x-auto text-[10px]">
              ADMIN_EMAILS={userEmail}
            </div>
          </div>

          <div className="flex items-center justify-center gap-3 pt-2">
            <button
              onClick={() => checkAdminStatus()}
              className="flex items-center gap-2 px-4 py-2 bg-white/5 hover:bg-white/10 text-zinc-200 border border-white/10 rounded-xl text-xs font-semibold uppercase tracking-wider transition cursor-pointer"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              <span>Retry</span>
            </button>
            <Link
              href="/chat"
              className="flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl text-xs font-semibold uppercase tracking-wider transition cursor-pointer"
            >
              <MessageSquare className="h-3.5 w-3.5" />
              <span>Back to Chat</span>
            </Link>
          </div>
        </div>
      </div>
    )
  }

  const navItems = [
    { name: "Overview", href: "/admin", icon: LayoutDashboard },
    { name: "Projects Manager", href: "/admin/projects", icon: FolderKanban },
    { name: "User Permissions", href: "/admin/users", icon: Users },
    { name: "Audit Viewer", href: "/admin/audit", icon: ShieldAlert },
  ]

  return (
    <div className="flex h-screen bg-[#030014] text-zinc-100 font-sans overflow-hidden">
      {/* Sidebar Navigation */}
      <aside className="w-64 glass-panel border-r border-white/5 flex flex-col justify-between">
        <div className="p-6 space-y-8">
          {/* Logo */}
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 rounded-lg bg-indigo-600 flex items-center justify-center text-white font-extrabold text-lg">
              M
            </div>
            <div>
              <h1 className="font-bold text-sm tracking-tight">MigrationBot</h1>
              <span className="text-[10px] text-zinc-500 font-semibold uppercase tracking-wider">Admin Panel</span>
            </div>
          </div>

          {/* Navigation Links */}
          <nav className="space-y-1">
            {navItems.map((item) => {
              const Icon = item.icon
              const isActive = pathname === item.href
              return (
                <Link
                  key={item.name}
                  href={item.href}
                  className={`flex items-center gap-3 px-4 py-3 rounded-xl text-xs font-semibold tracking-wider uppercase transition ${
                    isActive
                      ? "bg-indigo-600 text-white shadow-md"
                      : "text-zinc-400 hover:text-zinc-200 hover:bg-white/5"
                  }`}
                >
                  <Icon className="h-4.5 w-4.5" />
                  <span>{item.name}</span>
                </Link>
              )
            })}
          </nav>
        </div>

        {/* Footer Sidebar buttons */}
        <div className="p-6 border-t border-white/5 space-y-2">
          <Link
            href="/chat"
            className="flex items-center gap-3 px-4 py-3 rounded-xl text-xs font-semibold tracking-wider uppercase text-zinc-400 hover:text-zinc-200 hover:bg-white/5 transition"
          >
            <MessageSquare className="h-4.5 w-4.5" />
            <span>Agent Chat</span>
          </Link>
        </div>
      </aside>

      {/* Main Panel Content Area */}
      <div className="flex-1 flex flex-col overflow-hidden">
        <main className="flex-1 overflow-y-auto p-8 relative">
          {/* Grid Background Overlay */}
          <div className="absolute inset-0 -z-10 bg-[linear-gradient(to_right,#ffffff01_1px,transparent_1px),linear-gradient(to_bottom,#ffffff01_1px,transparent_1px)] bg-[size:3rem_3rem]"></div>
          {children}
        </main>
      </div>
    </div>
  )
}

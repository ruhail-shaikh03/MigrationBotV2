"use client"

import { useSession } from "next-auth/react"
import { useRouter, usePathname } from "next/navigation"
import { useEffect } from "react"
import { 
  LayoutDashboard, FolderKanban, Users, ShieldAlert, 
  MessageSquare, LogOut, ArrowLeft, Database
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

  useEffect(() => {
    if (status === "unauthenticated") {
      router.push("/")
    } else if (status === "authenticated") {
      const apiToken = (session as any)?.apiToken
      if (!apiToken) {
        router.push("/chat")
        return
      }
      fetch("/api/auth/me", {
        headers: { Authorization: `Bearer ${apiToken}` }
      })
        .then((res) => res.json())
        .then((data) => {
          if (!data || !data.is_admin) {
            router.push("/chat")
          }
        })
        .catch(() => {
          router.push("/chat")
        })
    }
  }, [status, session, router])

  if (status === "loading" || !session) {
    return (
      <div className="flex h-screen items-center justify-center bg-[#030014]">
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-indigo-500"></div>
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

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
      <div className="flex h-screen items-center justify-center bg-ink-950">
        <div className="flex flex-col items-center gap-3">
          <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-brass-500"></div>
          <span className="text-xs text-ink-400 font-mono">Verifying admin privileges...</span>
        </div>
      </div>
    )
  }

  if (adminStatus === "denied") {
    const userEmail = session?.user?.email || "Unknown user"
    return (
      <div className="flex h-screen items-center justify-center bg-ink-950 text-ink-100 p-6">
        <div className="panel max-w-md w-full p-8 rounded-xl border border-[color-mix(in_srgb,var(--color-failed)_25%,transparent)] text-center space-y-6">
          <div className="h-16 w-16 bg-[color-mix(in_srgb,var(--color-failed)_10%,transparent)] border border-[color-mix(in_srgb,var(--color-failed)_30%,transparent)] text-failed rounded-xl flex items-center justify-center mx-auto">
            <ShieldX className="h-8 w-8" />
          </div>
          <div>
            <h2 className="text-xl font-bold text-white mb-2">Admin Access Required</h2>
            <p className="text-xs text-ink-400 leading-relaxed">
              Logged in as <code className="text-brass-300 font-mono bg-ink-800 px-2 py-0.5 rounded">{userEmail}</code>.
              This email is not listed in the server's <code className="text-ink-300">ADMIN_EMAILS</code> configuration.
            </p>
          </div>

          <div className="bg-ink-850 border border-[var(--color-rule)] p-4 rounded-xl text-left text-[11px] text-ink-400 space-y-1.5 font-mono">
            <div className="font-semibold text-ink-300">How to grant access:</div>
            <div>Add <code className="text-brass-300">{userEmail}</code> to <code className="text-brass-300">ADMIN_EMAILS</code> in your VPS <code className="text-brass-300">.env</code> file:</div>
            <div className="text-ink-500 bg-black/40 p-2 rounded overflow-x-auto text-[10px]">
              ADMIN_EMAILS={userEmail}
            </div>
          </div>

          <div className="flex items-center justify-center gap-3 pt-2">
            <button
              onClick={() => checkAdminStatus()}
              className="flex items-center gap-2 px-4 py-2 bg-ink-800 hover:bg-ink-700 text-ink-200 border border-[var(--color-rule-strong)] rounded-xl text-xs font-semibold uppercase tracking-wider transition cursor-pointer"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              <span>Retry</span>
            </button>
            <Link
              href="/chat"
              className="flex items-center gap-2 px-4 py-2 bg-brass-400 hover:bg-brass-300 text-white rounded-xl text-xs font-semibold uppercase tracking-wider transition cursor-pointer"
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
    <div className="flex h-screen bg-ink-950 text-ink-100 font-sans overflow-hidden">
      {/* Sidebar Navigation. Collapses to an icon rail below lg rather than eating
          a quarter of a narrow viewport — `shrink-0` stops flex from squeezing it
          into the content when a wide table is present. */}
      <aside className="flex w-16 shrink-0 flex-col justify-between border-r border-[var(--color-rule)] panel lg:w-64">
        <div className="space-y-8 p-3 lg:p-6">
          {/* Logo */}
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-[var(--color-rule-strong)] bg-ink-800 text-[15px] font-semibold text-ink-100">
              M
            </div>
            <div className="hidden lg:block">
              <h1 className="font-bold text-sm tracking-tight">MigrationBot</h1>
              <span className="text-[10px] text-ink-500 font-semibold uppercase tracking-wider">Admin Panel</span>
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
                  title={item.name}
                  aria-current={isActive ? "page" : undefined}
                  className={`flex items-center justify-center gap-3 rounded-xl px-3 py-3 text-xs font-semibold uppercase tracking-wider transition lg:justify-start lg:px-4 ${
                    isActive
                      ? "bg-ink-800 text-ink-100 shadow-[inset_2px_0_0_var(--color-brass-400)]"
                      : "text-ink-400 hover:bg-ink-800 hover:text-ink-200"
                  }`}
                >
                  <Icon className="h-4.5 w-4.5 shrink-0" />
                  <span className="hidden lg:inline">{item.name}</span>
                </Link>
              )
            })}
          </nav>
        </div>

        {/* Footer Sidebar buttons */}
        <div className="space-y-2 border-t border-[var(--color-rule)] p-3 lg:p-6">
          <Link
            href="/chat"
            title="Agent Chat"
            className="flex items-center justify-center gap-3 rounded-xl px-3 py-3 text-xs font-semibold uppercase tracking-wider text-ink-400 transition hover:bg-ink-800 hover:text-ink-200 lg:justify-start lg:px-4"
          >
            <MessageSquare className="h-4.5 w-4.5 shrink-0" />
            <span className="hidden lg:inline">Agent Chat</span>
          </Link>
        </div>
      </aside>

      {/* Main Panel Content Area. `min-w-0` is what allows the wide admin tables to
          scroll inside their own container instead of stretching this column and
          pushing the whole layout sideways. */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <main className="relative flex-1 overflow-y-auto p-5 sm:p-8">
          {/* Grid Background Overlay */}
          <div className="absolute inset-0 -z-10 bg-[linear-gradient(to_right,#ffffff01_1px,transparent_1px),linear-gradient(to_bottom,#ffffff01_1px,transparent_1px)] bg-[size:3rem_3rem]"></div>
          {children}
        </main>
      </div>
    </div>
  )
}

"use client"

import { useSession } from "next-auth/react"
import { useEffect, useState } from "react"
import { 
  Users, Plus, Edit2, Trash2, X, AlertTriangle, ShieldCheck
} from "lucide-react"

interface PermissionRecord {
  id: number
  user_email: string
  project_name: string
  project_id: number
  role: string
  allowed_fields: string[]
  denied_operations: string[]
  updated_at: string
}

interface Project {
  id: number
  project_name: string
}

export default function AdminUsers() {
  const { data: session } = useSession()
  const apiToken = (session as any)?.apiToken || ""

  // Data lists state
  const [permissions, setPermissions] = useState<PermissionRecord[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [errorMsg, setErrorMsg] = useState("")

  // Form Modal states
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [editingPerm, setEditingPerm] = useState<PermissionRecord | null>(null)

  // Form input fields
  const [userEmail, setUserEmail] = useState("")
  const [projectId, setProjectId] = useState<number | "">("")
  const [role, setRole] = useState("viewer")
  const [allowedFieldsStr, setAllowedFieldsStr] = useState("*")
  const [deniedOpsStr, setDeniedOpsStr] = useState("")

  const loadData = async () => {
    try {
      setIsLoading(true)
      const headers = { "Authorization": `Bearer ${apiToken}` }
      const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"

      const [permRes, projRes] = await Promise.all([
        fetch(`${baseUrl}/api/admin/permissions`, { headers }),
        fetch(`${baseUrl}/api/admin/projects`, { headers })
      ])

      if (permRes.ok && projRes.ok) {
        const permsData = await permRes.json()
        const projsData = await projRes.json()
        setPermissions(permsData)
        setProjects(projsData)
      } else {
        setErrorMsg("Failed to retrieve RBAC record maps from database.")
      }
    } catch (err) {
      console.error(err)
      setErrorMsg("Failed to connect to the backend server.")
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    if (apiToken) {
      loadData()
    }
  }, [apiToken])

  // Open modal for creating permissions mapping
  const handleOpenCreateModal = () => {
    setEditingPerm(null)
    setUserEmail("")
    setProjectId(projects.length > 0 ? projects[0].id : "")
    setRole("viewer")
    setAllowedFieldsStr("*")
    setDeniedOpsStr("")
    setIsModalOpen(true)
  }

  // Open modal for editing permissions mapping
  const handleOpenEditModal = (p: PermissionRecord) => {
    setEditingPerm(p)
    setUserEmail(p.user_email)
    setProjectId(p.project_id)
    setRole(p.role)
    setAllowedFieldsStr(p.allowed_fields.join(", "))
    setDeniedOpsStr(p.denied_operations.join(", "))
    setIsModalOpen(true)
  }

  // Handle Save
  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    setErrorMsg("")

    if (!projectId) {
      setErrorMsg("You must select a project to assign permissions.")
      return
    }

    // Process lists fields
    const allowed_fields = allowedFieldsStr.split(",")
      .map(s => s.trim())
      .filter(s => s.length > 0)
    
    const denied_operations = deniedOpsStr.split(",")
      .map(s => s.trim())
      .filter(s => s.length > 0)

    const payload = {
      user_email: userEmail,
      project_id: Number(projectId),
      role: role,
      allowed_fields,
      denied_operations
    }

    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/admin/permissions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${apiToken}`
        },
        body: JSON.stringify(payload)
      })

      if (res.ok) {
        setIsModalOpen(false)
        loadData()
      } else {
        const err = await res.json()
        setErrorMsg(err.detail || "Upsert failed.")
      }
    } catch (err) {
      console.error(err)
      setErrorMsg("Network request failed.")
    }
  }

  // Handle Delete
  const handleDelete = async (id: number) => {
    if (!confirm("Are you sure you want to remove this permission assignment?")) return
    
    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/admin/permissions/${id}`, {
        method: "DELETE",
        headers: { "Authorization": `Bearer ${apiToken}` }
      })
      if (res.ok) {
        loadData()
      } else {
        const err = await res.json()
        alert(err.detail || "Removal failed.")
      }
    } catch (err) {
      console.error(err)
      alert("Failed to communicate with server.")
    }
  }

  return (
    <div className="space-y-8 animate-slide-up">
      {/* Header bar */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-3xl font-extrabold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-zinc-50 to-indigo-200">
            User Security & RBAC Policies
          </h2>
          <p className="text-sm text-zinc-500 mt-1">
            Map project scopes, roles, and allowed/denied columns rules for spreadsheet editors.
          </p>
        </div>

        <button
          onClick={handleOpenCreateModal}
          disabled={projects.length === 0}
          className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-500 text-white font-semibold py-2.5 px-5 rounded-xl text-xs tracking-wider uppercase transition cursor-pointer shadow-lg disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <Plus className="h-4.5 w-4.5" />
          <span>Add Permissions Mapping</span>
        </button>
      </div>

      {errorMsg && (
        <div className="p-4 bg-rose-500/10 border border-rose-500/20 text-rose-400 text-sm rounded-xl flex items-center gap-3">
          <AlertTriangle className="h-5 w-5" />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Permissions List Table */}
      {isLoading ? (
        <div className="flex justify-center h-48 items-center">
          <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-indigo-500"></div>
        </div>
      ) : permissions.length === 0 ? (
        <div className="glass-panel p-12 text-center text-zinc-500 rounded-2xl border border-white/5">
          No user permissions mapped. {projects.length === 0 ? "Create a project first." : 'Click "Add Permissions Mapping" to add.'}
        </div>
      ) : (
        <div className="glass-panel rounded-2xl border border-white/5 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-white/5 bg-white/[0.02]">
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">User Email</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Project Context</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Role Role</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Allowed Fields</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Denied Ops</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {permissions.map((p) => (
                  <tr key={p.id} className="hover:bg-white/[0.01] transition">
                    <td className="p-4 text-sm font-bold text-zinc-200">{p.user_email}</td>
                    <td className="p-4 text-sm text-zinc-400 font-semibold">{p.project_name}</td>
                    <td className="p-4 text-xs uppercase font-extrabold tracking-wide">
                      <span className={`px-2.5 py-0.5 rounded-full ${
                        p.role === "admin" 
                          ? "bg-rose-500/10 border border-rose-500/20 text-rose-400"
                          : p.role === "editor"
                          ? "bg-indigo-500/10 border border-indigo-500/20 text-indigo-400"
                          : "bg-zinc-500/10 border border-white/5 text-zinc-400"
                      }`}>
                        {p.role}
                      </span>
                    </td>
                    <td className="p-4 text-xs text-zinc-500 font-mono truncate max-w-[200px]" title={p.allowed_fields.join(", ")}>
                      {p.allowed_fields.join(", ")}
                    </td>
                    <td className="p-4 text-xs text-zinc-500 font-mono truncate max-w-[200px]" title={p.denied_operations.join(", ")}>
                      {p.denied_operations.length === 0 ? "none" : p.denied_operations.join(", ")}
                    </td>
                    <td className="p-4 text-right flex items-center justify-end gap-2.5">
                      <button
                        onClick={() => handleOpenEditModal(p)}
                        className="p-2 rounded-lg text-indigo-400 bg-indigo-500/10 hover:bg-indigo-500/20 transition cursor-pointer"
                        title="Edit Permissions"
                      >
                        <Edit2 className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(p.id)}
                        className="p-2 rounded-lg text-rose-400 bg-rose-500/10 hover:bg-rose-500/20 transition cursor-pointer"
                        title="Remove Mapping"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Edit / Create User Permission Mapping Modal */}
      {isModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
          <div className="glass-panel w-full max-w-xl rounded-2xl border border-white/10 shadow-2xl p-8 relative">
            <button
              onClick={() => setIsModalOpen(false)}
              className="absolute top-6 right-6 p-1.5 rounded-lg text-zinc-400 hover:text-zinc-200 hover:bg-white/5 transition cursor-pointer"
            >
              <X className="h-5 w-5" />
            </button>

            <h3 className="text-xl font-bold text-zinc-100 mb-6 flex items-center gap-2">
              <ShieldCheck className="h-5.5 w-5.5 text-indigo-400" />
              {editingPerm ? "Edit User Permissions Policy" : "Create New User RBAC Rule Mapping"}
            </h3>

            <form onSubmit={handleSave} className="space-y-5">
              <div className="space-y-1.5">
                <label className="text-xs font-semibold uppercase text-zinc-400">User Email Address</label>
                <input
                  type="email"
                  required
                  disabled={!!editingPerm}
                  value={userEmail}
                  onChange={(e) => setUserEmail(e.target.value)}
                  className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-indigo-500 disabled:opacity-50"
                  placeholder="e.g. consult@company.com"
                />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="space-y-1.5">
                  <label className="text-xs font-semibold uppercase text-zinc-400">Project Boundary</label>
                  <select
                    required
                    disabled={!!editingPerm}
                    value={projectId}
                    onChange={(e) => setProjectId(Number(e.target.value))}
                    className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-200 focus:outline-none focus:border-indigo-500 cursor-pointer disabled:opacity-50"
                  >
                    {projects.map((proj) => (
                      <option key={proj.id} value={proj.id}>
                        {proj.project_name}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="space-y-1.5">
                  <label className="text-xs font-semibold uppercase text-zinc-400">Role Assigned</label>
                  <select
                    required
                    value={role}
                    onChange={(e) => setRole(e.target.value)}
                    className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-200 focus:outline-none focus:border-indigo-500 cursor-pointer"
                  >
                    <option value="viewer">Viewer (Read-only)</option>
                    <option value="editor">Editor (Queue cell updates)</option>
                    <option value="admin">Admin (Full settings control)</option>
                  </select>
                </div>
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-semibold uppercase text-zinc-400">Allowed Columns Fields (Comma Separated)</label>
                <input
                  type="text"
                  required
                  value={allowedFieldsStr}
                  onChange={(e) => setAllowedFieldsStr(e.target.value)}
                  className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 focus:outline-none focus:border-indigo-500 font-mono text-indigo-300"
                  placeholder="e.g. *, Dev Status, Comments"
                />
                <span className="text-[10px] text-zinc-500 block">Use '*' to allow editing of all sheet columns.</span>
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-semibold uppercase text-zinc-400">Denied Operations (Comma Separated)</label>
                <input
                  type="text"
                  value={deniedOpsStr}
                  onChange={(e) => setDeniedOpsStr(e.target.value)}
                  className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 focus:outline-none focus:border-indigo-500 font-mono text-rose-300"
                  placeholder="e.g. format_row, add_row"
                />
                <span className="text-[10px] text-zinc-500 block">List tools to forbid (e.g. format_row, add_row, bulk_update).</span>
              </div>

              <div className="flex items-center justify-end gap-3 pt-4 border-t border-white/5">
                <button
                  type="button"
                  onClick={() => setIsModalOpen(false)}
                  className="px-5 py-2.5 rounded-xl border border-white/10 text-zinc-400 hover:text-zinc-200 text-xs font-semibold tracking-wider uppercase transition cursor-pointer"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold tracking-wider uppercase transition cursor-pointer shadow-lg"
                >
                  Save Configuration
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

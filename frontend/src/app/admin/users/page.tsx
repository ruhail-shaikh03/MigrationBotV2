"use client"

import { useSession } from "next-auth/react"
import { useEffect, useState } from "react"
import {
  Users, Plus, Edit2, Trash2, X, AlertTriangle, ShieldCheck
} from "lucide-react"
import Modal from "@/components/Modal"

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
      const baseUrl = ""

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
      const res = await fetch(`${""}/api/admin/permissions`, {
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
      const res = await fetch(`${""}/api/admin/permissions/${id}`, {
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
    <div className="space-y-8 animate-rise">
      {/* Header bar */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="display-md">
            Who can do what
          </h2>
          <p className="text-sm text-ink-500 mt-1">
            Give someone a role on a sheet, and limit which columns they can change.
          </p>
        </div>

        <button
          onClick={handleOpenCreateModal}
          disabled={projects.length === 0}
          className="btn btn-primary"
        >
          <Plus className="h-4.5 w-4.5" />
          <span>Add Permissions Mapping</span>
        </button>
      </div>

      {errorMsg && (
        <div className="p-4 bg-[color-mix(in_srgb,var(--color-failed)_10%,transparent)] border border-[color-mix(in_srgb,var(--color-failed)_25%,transparent)] text-failed text-sm rounded-xl flex items-center gap-3">
          <AlertTriangle className="h-5 w-5" />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Permissions List Table */}
      {isLoading ? (
        <div className="flex justify-center h-48 items-center">
          <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-brass-500"></div>
        </div>
      ) : permissions.length === 0 ? (
        <div className="panel p-12 text-center text-ink-500 rounded-xl border border-[var(--color-rule)]">
          No user permissions mapped. {projects.length === 0 ? "Create a project first." : 'Click "Add Permissions Mapping" to add.'}
        </div>
      ) : (
        <div className="panel rounded-xl border border-[var(--color-rule)] overflow-hidden">
          <div className="overflow-x-auto">
            {/* See projects/page.tsx: min-w is required for the wrapper to scroll. */}
            <table className="w-full min-w-[900px] text-left border-collapse">
              <thead>
                <tr className="border-b border-[var(--color-rule)] bg-ink-850">
                  <th className="label-micro whitespace-nowrap p-4">User Email</th>
                  <th className="label-micro whitespace-nowrap p-4">Project Context</th>
                  <th className="label-micro whitespace-nowrap p-4">Role</th>
                  <th className="label-micro whitespace-nowrap p-4">Allowed Fields</th>
                  <th className="label-micro whitespace-nowrap p-4">Denied Ops</th>
                  <th className="label-micro whitespace-nowrap p-4 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--color-rule)]">
                {permissions.map((p) => (
                  <tr key={p.id} className="hover:bg-ink-850 transition">
                    <td className="p-4 text-sm font-bold text-ink-200">{p.user_email}</td>
                    <td className="p-4 text-sm text-ink-400 font-semibold">{p.project_name}</td>
                    <td className="p-4 text-xs uppercase font-extrabold tracking-wide">
                      <span className={`px-2.5 py-0.5 rounded-full ${
                        p.role === "admin" 
                          ? "bg-[color-mix(in_srgb,var(--color-failed)_10%,transparent)] border border-[color-mix(in_srgb,var(--color-failed)_25%,transparent)] text-failed"
                          : p.role === "editor"
                          ? "bg-brass-400/10 border border-brass-500/20 text-brass-400"
                          : "bg-zinc-500/10 border border-[var(--color-rule)] text-ink-400"
                      }`}>
                        {p.role}
                      </span>
                    </td>
                    <td className="p-4 text-xs text-ink-500 font-mono truncate max-w-[200px]" title={p.allowed_fields.join(", ")}>
                      {p.allowed_fields.join(", ")}
                    </td>
                    <td className="p-4 text-xs text-ink-500 font-mono truncate max-w-[200px]" title={p.denied_operations.join(", ")}>
                      {p.denied_operations.length === 0 ? "none" : p.denied_operations.join(", ")}
                    </td>
                    <td className="p-4 text-right flex items-center justify-end gap-2.5">
                      <button
                        onClick={() => handleOpenEditModal(p)}
                        className="btn btn-ghost px-2 py-2"
                        title="Edit Permissions"
                      >
                        <Edit2 className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(p.id)}
                        className="btn btn-danger px-2 py-2"
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
      <Modal
        open={isModalOpen}
        onClose={() => setIsModalOpen(false)}
        title={editingPerm ? "Edit User Permissions Policy" : "New User Permission Mapping"}
        icon={<ShieldCheck className="h-5 w-5 shrink-0 text-brass-400" />}
        description="Scoped to one user and one project. Roles gate which tools run; allowed fields gate which columns they can write."
        footer={
          <div className="flex items-center justify-end gap-3">
            <button
              type="button"
              onClick={() => setIsModalOpen(false)}
              className="btn btn-secondary"
            >
              Cancel
            </button>
            {/* `form=` lets the submit button live outside the <form>, which is what
                keeps it pinned in the footer instead of scrolling away with the fields. */}
            <button
              type="submit"
              form="permission-form"
              className="btn btn-primary"
            >
              Save Configuration
            </button>
          </div>
        }
      >
            <form id="permission-form" onSubmit={handleSave} className="space-y-5">
              <div className="space-y-1.5">
                <label className="label-micro">User Email Address</label>
                <input
                  type="email"
                  required
                  disabled={!!editingPerm}
                  value={userEmail}
                  onChange={(e) => setUserEmail(e.target.value)}
                  className="field"
                  placeholder="e.g. consult@company.com"
                />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="space-y-1.5">
                  <label className="label-micro">Project Boundary</label>
                  <select
                    required
                    disabled={!!editingPerm}
                    value={projectId}
                    onChange={(e) => setProjectId(Number(e.target.value))}
                    className="field cursor-pointer"
                  >
                    {projects.map((proj) => (
                      <option key={proj.id} value={proj.id}>
                        {proj.project_name}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="space-y-1.5">
                  <label className="label-micro">Role Assigned</label>
                  <select
                    required
                    value={role}
                    onChange={(e) => setRole(e.target.value)}
                    className="field cursor-pointer"
                  >
                    <option value="viewer">Viewer (Read-only)</option>
                    <option value="editor">Editor (Queue cell updates)</option>
                    <option value="admin">Admin (Full settings control)</option>
                  </select>
                </div>
              </div>

              <div className="space-y-1.5">
                <label className="label-micro">Allowed Columns Fields (Comma Separated)</label>
                <input
                  type="text"
                  required
                  value={allowedFieldsStr}
                  onChange={(e) => setAllowedFieldsStr(e.target.value)}
                  className="field field-mono"
                  placeholder="e.g. *, Dev Status, Comments"
                />
                <span className="text-[10px] text-ink-500 block">Use '*' to allow editing of all sheet columns.</span>
              </div>

              <div className="space-y-1.5">
                <label className="label-micro">Denied Operations (Comma Separated)</label>
                <input
                  type="text"
                  value={deniedOpsStr}
                  onChange={(e) => setDeniedOpsStr(e.target.value)}
                  className="field field-mono"
                  placeholder="e.g. format_row, add_row"
                />
                <span className="text-[10px] text-ink-500 block">List tools to forbid (e.g. format_row, add_row, bulk_update).</span>
              </div>
            </form>
      </Modal>
    </div>
  )
}

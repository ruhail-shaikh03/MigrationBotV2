"use client"

import { useSession } from "next-auth/react"
import { useEffect, useState } from "react"
import { 
  FolderKanban, Plus, Edit2, Trash2, X, Check, AlertTriangle
} from "lucide-react"

interface Project {
  id: number
  project_name: string
  spreadsheet_id: string
  default_tab: string
  company_prefix: string
  is_active: boolean
  schema_config: any
  created_at: string
}

export default function AdminProjects() {
  const { data: session } = useSession()
  const apiToken = (session as any)?.apiToken || ""
  
  // Data lists state
  const [projects, setProjects] = useState<Project[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [errorMsg, setErrorMsg] = useState("")

  // Form Modal states
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [editingProject, setEditingProject] = useState<Project | null>(null)
  
  // Form input fields
  const [projectName, setProjectName] = useState("")
  const [spreadsheetId, setSpreadsheetId] = useState("")
  const [defaultTab, setDefaultTab] = useState("SD")
  const [companyPrefix, setCompanyPrefix] = useState("FFC")
  const [isActive, setIsActive] = useState(true)
  const [schemaConfigStr, setSchemaConfigStr] = useState("{}")

  // Auto-detect fields
  const [spreadsheetUrl, setSpreadsheetUrl] = useState("")
  const [isAnalyzing, setIsAnalyzing] = useState(false)
  const [detectedTabsMap, setDetectedTabsMap] = useState<Record<string, any>>({})
  const [selectedTabs, setSelectedTabs] = useState<Record<string, boolean>>({})
  const [isAutoDetectMode, setIsAutoDetectMode] = useState(true)

  const fetchProjects = async () => {
    try {
      setIsLoading(true)
      const res = await fetch(`${""}/api/admin/projects`, {
        headers: { "Authorization": `Bearer ${apiToken}` }
      })
      if (res.ok) {
        const data = await res.json()
        setProjects(data)
      } else {
        const err = await res.json()
        setErrorMsg(err.detail || "Failed to fetch projects.")
      }
    } catch (err) {
      console.error(err)
      setErrorMsg("Failed to communicate with server.")
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    if (apiToken) {
      fetchProjects()
    }
  }, [apiToken])

  // Helper to dynamically build the JSON schema string based on the selected tabs
  const rebuildSchemaConfig = (tabsMap: Record<string, any>, selected: Record<string, boolean>, prefix: string) => {
    const finalTabs: Record<string, any> = {}
    const activeTabs = Object.keys(tabsMap).filter(tabName => selected[tabName])
    activeTabs.forEach(tabName => {
      finalTabs[tabName] = tabsMap[tabName]
    })

    const config = {
      tabs: finalTabs,
      global: {
        valid_modules: activeTabs,
        company_prefix: prefix
      }
    }
    setSchemaConfigStr(JSON.stringify(config, null, 2))
  }

  // Open modal for creating new project
  const handleOpenCreateModal = () => {
    setEditingProject(null)
    setProjectName("")
    setSpreadsheetId("")
    setSpreadsheetUrl("")
    setDetectedTabsMap({})
    setSelectedTabs({})
    setIsAutoDetectMode(true)
    setDefaultTab("SD")
    setCompanyPrefix("FFC")
    setIsActive(true)
    setSchemaConfigStr(JSON.stringify({
      primary_id_position: "B",
      data_start_row: 3,
      column_map: {
        ricefw_id: "RICEFW ID",
        module: "Module",
        title: "Title",
        status: "Dev Status"
      }
    }, null, 2))
    setIsModalOpen(true)
  }

  // Open modal for editing existing project
  const handleOpenEditModal = (p: Project) => {
    setEditingProject(p)
    setProjectName(p.project_name)
    setSpreadsheetId(p.spreadsheet_id)
    setSpreadsheetUrl(`https://docs.google.com/spreadsheets/d/${p.spreadsheet_id}`)
    setIsAutoDetectMode(false)
    setDefaultTab(p.default_tab)
    setCompanyPrefix(p.company_prefix || "FFC")
    setIsActive(p.is_active)
    setSchemaConfigStr(JSON.stringify(p.schema_config || {}, null, 2))
    setIsModalOpen(true)
  }

  const handleAnalyzeSheet = async () => {
    if (!spreadsheetUrl.trim()) {
      alert("Please enter a Google Sheets URL or ID.")
      return
    }
    setErrorMsg("")
    setIsAnalyzing(true)
    try {
      const googleToken = (session as any)?.googleAccessToken || ""
      const res = await fetch(`/api/admin/projects/detect-metadata`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${apiToken}`,
          "X-Google-Access-Token": googleToken
        },
        body: JSON.stringify({ spreadsheet_url: spreadsheetUrl })
      })

      if (res.ok) {
        const data = await res.json()
        const config = data.detected_config
        const sheetsMap = config.tabs || {}
        
        setSpreadsheetId(data.spreadsheet_id)
        setDetectedTabsMap(sheetsMap)
        
        // Auto-select all tabs initially
        const sel: Record<string, boolean> = {}
        Object.keys(sheetsMap).forEach(t => {
          sel[t] = true
        })
        setSelectedTabs(sel)

        // Try to set default tab
        const keys = Object.keys(sheetsMap)
        if (keys.length > 0) {
          setDefaultTab(keys[0])
        }

        rebuildSchemaConfig(sheetsMap, sel, companyPrefix)
      } else {
        const err = await res.json()
        setErrorMsg(err.detail || "Auto-detection failed. Make sure the spreadsheet exists and is shared.")
      }
    } catch (err) {
      console.error(err)
      setErrorMsg("Failed to communicate with the auto-detection API.")
    } finally {
      setIsAnalyzing(false)
    }
  }

  const handleToggleTab = (tabName: string) => {
    const nextSelected = { ...selectedTabs, [tabName]: !selectedTabs[tabName] }
    setSelectedTabs(nextSelected)
    rebuildSchemaConfig(detectedTabsMap, nextSelected, companyPrefix)
    
    // Auto update default tab if current default is deselected
    if (defaultTab === tabName && !nextSelected[tabName]) {
      const activeKeys = Object.keys(detectedTabsMap).filter(t => nextSelected[t])
      if (activeKeys.length > 0) {
        setDefaultTab(activeKeys[0])
      }
    }
  }

  const handleCompanyPrefixChange = (val: string) => {
    setCompanyPrefix(val)
    try {
      const parsed = JSON.parse(schemaConfigStr)
      if (parsed.global) {
        parsed.global.company_prefix = val
        setSchemaConfigStr(JSON.stringify(parsed, null, 2))
      }
    } catch (e) {}
  }

  // Handle Create / Update Save
  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    setErrorMsg("")

    // 1. Verify schema config is valid JSON
    let parsedSchema = {}
    try {
      parsedSchema = JSON.parse(schemaConfigStr)
    } catch (err) {
      setErrorMsg("Schema Configuration must be valid JSON format.")
      return
    }

    const baseUrl = ""
    const headers = {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${apiToken}`
    }

    try {
      if (editingProject) {
        // Update operation
        const payload = {
          project_name: projectName,
          default_tab: defaultTab,
          company_prefix: companyPrefix,
          is_active: isActive,
          schema_config: parsedSchema
        }
        const res = await fetch(`${baseUrl}/api/admin/projects/${editingProject.id}`, {
          method: "PUT",
          headers,
          body: JSON.stringify(payload)
        })
        if (res.ok) {
          setIsModalOpen(false)
          fetchProjects()
        } else {
          const err = await res.json()
          setErrorMsg(err.detail || "Update failed.")
        }
      } else {
        // Create operation
        const targetId = (spreadsheetId || spreadsheetUrl).strip ? (spreadsheetId || spreadsheetUrl).trim() : (spreadsheetId || spreadsheetUrl)
        if (!targetId) {
          setErrorMsg("Please enter a Google Sheets URL or Spreadsheet ID.")
          return
        }
        const payload = {
          project_name: projectName || "S/4HANA Migration Project",
          spreadsheet_id: targetId,
          default_tab: defaultTab || "SD",
          company_prefix: companyPrefix || "FFC",
          schema_config: (parsedSchema && Object.keys(parsedSchema).length > 0) ? parsedSchema : null
        }
        const googleToken = (session as any)?.googleAccessToken || ""
        const res = await fetch(`${baseUrl}/api/admin/projects`, {
          method: "POST",
          headers: {
            ...headers,
            "X-Google-Access-Token": googleToken
          },
          body: JSON.stringify(payload)
        })
        if (res.ok) {
          const newProj = await res.json()
          setIsModalOpen(false)
          fetchProjects()
        } else {
          const err = await res.json()
          setErrorMsg(err.detail || "Creation failed.")
        }
      }
    } catch (err) {
      console.error(err)
      setErrorMsg("Network request failed.")
    }
  }

  // Handle Delete
  const handleDelete = async (id: number) => {
    if (!confirm("Are you sure you want to delete this project? This will also cascade delete all user permission mappings.")) return
    
    try {
      const res = await fetch(`${""}/api/admin/projects/${id}`, {
        method: "DELETE",
        headers: { "Authorization": `Bearer ${apiToken}` }
      })
      if (res.ok) {
        fetchProjects()
      } else {
        const err = await res.json()
        alert(err.detail || "Deletion failed.")
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
            Projects Configuration Manager
          </h2>
          <p className="text-sm text-zinc-500 mt-1">
            Register S/4HANA tracker spreadsheets and edit columns mappings metadata below.
          </p>
        </div>

        <button
          onClick={handleOpenCreateModal}
          className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-500 text-white font-semibold py-2.5 px-5 rounded-xl text-xs tracking-wider uppercase transition cursor-pointer shadow-lg"
        >
          <Plus className="h-4.5 w-4.5" />
          <span>New Project</span>
        </button>
      </div>

      {errorMsg && (
        <div className="p-4 bg-rose-500/10 border border-rose-500/20 text-rose-400 text-sm rounded-xl flex items-center gap-3">
          <AlertTriangle className="h-5 w-5" />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Projects List Table */}
      {isLoading ? (
        <div className="flex justify-center h-48 items-center">
          <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-indigo-500"></div>
        </div>
      ) : projects.length === 0 ? (
        <div className="glass-panel p-12 text-center text-zinc-500 rounded-2xl border border-white/5">
          No projects registered. Click "New Project" to add one.
        </div>
      ) : (
        <div className="glass-panel rounded-2xl border border-white/5 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-white/5 bg-white/[0.02]">
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Project Name</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Spreadsheet ID</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Default Tab</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Prefix</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400">Status</th>
                  <th className="p-4 text-xs font-bold uppercase tracking-wider text-zinc-400 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {projects.map((p) => (
                  <tr key={p.id} className="hover:bg-white/[0.01] transition">
                    <td className="p-4 text-sm font-bold text-zinc-200">{p.project_name}</td>
                    <td className="p-4 text-xs text-zinc-500 font-mono select-all max-w-[200px] truncate" title={p.spreadsheet_id}>
                      {p.spreadsheet_id}
                    </td>
                    <td className="p-4 text-xs font-semibold text-zinc-300">
                      <span className="px-2 py-1 rounded bg-[#120e2e] border border-white/5">{p.default_tab}</span>
                    </td>
                    <td className="p-4 text-xs font-semibold text-zinc-300">{p.company_prefix}</td>
                    <td className="p-4 text-xs">
                      {p.is_active ? (
                        <span className="px-2.5 py-0.5 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 font-medium">
                          Active
                        </span>
                      ) : (
                        <span className="px-2.5 py-0.5 rounded-full bg-zinc-500/10 border border-white/5 text-zinc-500 font-medium">
                          Inactive
                        </span>
                      )}
                    </td>
                    <td className="p-4 text-right flex items-center justify-end gap-2.5">
                      <button
                        onClick={() => handleOpenEditModal(p)}
                        className="p-2 rounded-lg text-indigo-400 bg-indigo-500/10 hover:bg-indigo-500/20 transition cursor-pointer"
                        title="Edit Project"
                      >
                        <Edit2 className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(p.id)}
                        className="p-2 rounded-lg text-rose-400 bg-rose-500/10 hover:bg-rose-500/20 transition cursor-pointer"
                        title="Delete Project"
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

      {/* Edit / Create Project Modal */}
      {isModalOpen && (
        <div className="fixed inset-0 z-50 overflow-y-auto bg-black/75 backdrop-blur-sm p-4 sm:p-6 flex items-center justify-center">
          <div className="glass-panel w-full max-w-2xl rounded-2xl border border-white/10 shadow-2xl p-6 sm:p-8 relative flex flex-col my-auto max-h-[85vh] overflow-hidden">
            <button
              onClick={() => setIsModalOpen(false)}
              className="absolute top-6 right-6 p-1.5 rounded-lg text-zinc-400 hover:text-zinc-200 hover:bg-white/5 transition cursor-pointer"
            >
              <X className="h-5 w-5" />
            </button>

            <h3 className="text-xl font-bold text-zinc-100 mb-2">
              {editingProject ? "Modify Project Configs" : "Register New Spreadsheet Project"}
            </h3>
            
            {!editingProject && (
              <div className="flex gap-4 border-b border-white/5 pb-3 mb-4">
                <button
                  type="button"
                  onClick={() => setIsAutoDetectMode(true)}
                  className={`text-xs font-semibold uppercase pb-1 tracking-wider border-b-2 transition ${
                    isAutoDetectMode 
                      ? "border-indigo-500 text-indigo-400" 
                      : "border-transparent text-zinc-500 hover:text-zinc-300"
                  }`}
                >
                  Auto-Detect Wizard
                </button>
                <button
                  type="button"
                  onClick={() => setIsAutoDetectMode(false)}
                  className={`text-xs font-semibold uppercase pb-1 tracking-wider border-b-2 transition ${
                    !isAutoDetectMode 
                      ? "border-indigo-500 text-indigo-400" 
                      : "border-transparent text-zinc-500 hover:text-zinc-300"
                  }`}
                >
                  Manual Mode
                </button>
              </div>
            )}

            <form onSubmit={handleSave} className="space-y-5 overflow-y-auto pr-2 flex-1">
              {isAutoDetectMode && !editingProject ? (
                <div className="space-y-5 animate-slide-up">
                  {/* Step 1: URL input */}
                  <div className="space-y-1.5 bg-white/[0.01] border border-white/5 p-4 rounded-xl">
                    <label className="text-xs font-semibold uppercase text-zinc-400 block">Google Sheets URL or ID</label>
                    <div className="flex gap-2">
                      <input
                        type="text"
                        value={spreadsheetUrl}
                        onChange={(e) => setSpreadsheetUrl(e.target.value)}
                        placeholder="https://docs.google.com/spreadsheets/d/..."
                        className="flex-1 bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-indigo-500"
                      />
                      <button
                        type="button"
                        onClick={handleAnalyzeSheet}
                        disabled={isAnalyzing}
                        className="bg-indigo-600 hover:bg-indigo-500 text-white font-semibold px-4 rounded-xl text-xs uppercase tracking-wider transition disabled:opacity-50 flex items-center justify-center min-w-[120px]"
                      >
                        {isAnalyzing ? "Analyzing..." : "Analyze"}
                      </button>
                    </div>
                  </div>

                  {/* Step 2: Display results if found */}
                  {Object.keys(detectedTabsMap).length > 0 ? (
                    <div className="space-y-5 animate-slide-up">
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div className="space-y-1.5">
                          <label className="text-xs font-semibold uppercase text-zinc-400">Project Name</label>
                          <input
                            type="text"
                            required
                            value={projectName}
                            onChange={(e) => setProjectName(e.target.value)}
                            className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 focus:outline-none focus:border-indigo-500"
                            placeholder="e.g. Finance Migration"
                          />
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-xs font-semibold uppercase text-zinc-400">Company Prefix</label>
                          <input
                            type="text"
                            required
                            value={companyPrefix}
                            onChange={(e) => handleCompanyPrefixChange(e.target.value)}
                            className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 focus:outline-none focus:border-indigo-500"
                          />
                        </div>
                      </div>

                      <div className="space-y-2">
                        <label className="text-xs font-semibold uppercase text-zinc-400 block">Select Tracker Tabs & Configure Fields</label>
                        <div className="space-y-3">
                          {Object.keys(detectedTabsMap).map(tab => {
                            const tabSchema = detectedTabsMap[tab] || {}
                            const isTabSelected = !!selectedTabs[tab]
                            const critFields = tabSchema.critical_fields || []

                            return (
                              <div
                                key={tab}
                                className={`rounded-xl border transition p-4 ${
                                  isTabSelected
                                    ? "bg-indigo-600/10 border-indigo-500/30"
                                    : "bg-white/[0.01] border-white/5 opacity-60"
                                }`}
                              >
                                <div className="flex items-center justify-between">
                                  <label className="flex items-center gap-2.5 cursor-pointer select-none">
                                    <input
                                      type="checkbox"
                                      checked={isTabSelected}
                                      onChange={() => handleToggleTab(tab)}
                                      className="h-4 w-4 rounded border-white/10 text-indigo-600 focus:ring-0 focus:ring-offset-0 cursor-pointer"
                                    />
                                    <span className="text-sm font-bold text-zinc-200">{tab} Tab</span>
                                  </label>
                                  {isTabSelected && (
                                    <span className="text-[10px] uppercase font-bold text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20">
                                      Auto-Detected
                                    </span>
                                  )}
                                </div>

                                {isTabSelected && (
                                  <div className="mt-3 pt-3 border-t border-white/5 grid grid-cols-2 gap-2 text-xs text-zinc-400">
                                    <div>
                                      <span className="text-zinc-500">ID Col:</span>{" "}
                                      <span className="text-zinc-200 font-medium">{tabSchema.primary_id_column || "RICEFW ID"} ({tabSchema.primary_id_position || "B"})</span>
                                    </div>
                                    <div>
                                      <span className="text-zinc-500">Status Col:</span>{" "}
                                      <span className="text-zinc-200 font-medium">{tabSchema.status_column || "Dev Status"}</span>
                                    </div>
                                    <div>
                                      <span className="text-zinc-500">Module Col:</span>{" "}
                                      <span className="text-zinc-200 font-medium">{tabSchema.module_column || "Module"}</span>
                                    </div>
                                    <div>
                                      <span className="text-zinc-500">Assignee Col:</span>{" "}
                                      <span className="text-zinc-200 font-medium">{tabSchema.assignee_column || "Technical Resource"}</span>
                                    </div>

                                    {critFields.length > 0 && (
                                      <div className="col-span-2 mt-2">
                                        <span className="text-zinc-500 block mb-1">Critical Display Fields:</span>
                                        <div className="flex flex-wrap gap-1.5">
                                          {critFields.map((f: string) => (
                                            <span key={f} className="px-2 py-0.5 bg-[#120e2e] border border-white/10 rounded text-[11px] text-indigo-300 font-mono">
                                              {f}
                                            </span>
                                          ))}
                                        </div>
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      </div>

                      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div className="space-y-1.5">
                          <label className="text-xs font-semibold uppercase text-zinc-400">Default Tab</label>
                          <select
                            value={defaultTab}
                            onChange={(e) => setDefaultTab(e.target.value)}
                            className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 focus:outline-none focus:border-indigo-500 cursor-pointer"
                          >
                            {Object.keys(selectedTabs).filter(t => selectedTabs[t]).map(t => (
                              <option key={t} value={t}>{t}</option>
                            ))}
                          </select>
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-xs font-semibold uppercase text-zinc-400">Spreadsheet ID</label>
                          <input
                            type="text"
                            disabled
                            value={spreadsheetId}
                            className="w-full bg-[#120e2e]/50 border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-400 select-all font-mono opacity-60"
                          />
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="glass-panel p-6 text-center text-zinc-500 rounded-xl border border-white/5">
                      Enter URL above and click Analyze to discover sheets.
                    </div>
                  )}
                </div>
              ) : (
                <div className="space-y-5 animate-slide-up">
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div className="space-y-1.5">
                      <label className="text-xs font-semibold uppercase text-zinc-400">Project Name</label>
                      <input
                        type="text"
                        required
                        value={projectName}
                        onChange={(e) => setProjectName(e.target.value)}
                        className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-indigo-500"
                        placeholder="e.g. Finance Migration"
                      />
                    </div>

                    <div className="space-y-1.5">
                      <label className="text-xs font-semibold uppercase text-zinc-400">Spreadsheet ID</label>
                      <input
                        type="text"
                        required
                        disabled={!!editingProject}
                        value={spreadsheetId}
                        onChange={(e) => setSpreadsheetId(e.target.value)}
                        className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
                        placeholder="Spreadsheet long hex ID"
                      />
                    </div>

                    <div className="space-y-1.5">
                      <label className="text-xs font-semibold uppercase text-zinc-400">Default Tab</label>
                      <input
                        type="text"
                        required
                        value={defaultTab}
                        onChange={(e) => setDefaultTab(e.target.value)}
                        className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 focus:outline-none focus:border-indigo-500"
                        placeholder="e.g. SD"
                      />
                    </div>

                    <div className="space-y-1.5">
                      <label className="text-xs font-semibold uppercase text-zinc-400">Company Prefix</label>
                      <input
                        type="text"
                        required
                        value={companyPrefix}
                        onChange={(e) => handleCompanyPrefixChange(e.target.value)}
                        className="w-full bg-[#120e2e] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-zinc-100 focus:outline-none focus:border-indigo-500"
                        placeholder="e.g. FFC"
                      />
                    </div>
                  </div>

                  {editingProject && (
                    <div className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        id="isActiveCheck"
                        checked={isActive}
                        onChange={(e) => setIsActive(e.target.checked)}
                        className="h-4 w-4 bg-[#120e2e] border border-white/10 text-indigo-600 rounded focus:ring-indigo-500 focus:ring-offset-0 cursor-pointer"
                      />
                      <label htmlFor="isActiveCheck" className="text-xs font-semibold uppercase text-zinc-300 cursor-pointer">
                        Active & Enable for agent interactions
                      </label>
                    </div>
                  )}

                  {/* JSON Column Mappings Config */}
                  <div className="space-y-1.5">
                    <label className="text-xs font-semibold uppercase text-zinc-400">Schema Mappings (JSON Format)</label>
                    <textarea
                      rows={8}
                      value={schemaConfigStr}
                      onChange={(e) => setSchemaConfigStr(e.target.value)}
                      className="w-full bg-[#030014] border border-white/10 rounded-xl p-4 text-xs font-mono text-indigo-300 focus:outline-none focus:border-indigo-500"
                      placeholder='{"primary_id_position": "B", ...}'
                    />
                  </div>
                </div>
              )}

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
                  disabled={isAutoDetectMode && !editingProject && Object.keys(detectedTabsMap).length === 0}
                  className="px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold tracking-wider uppercase transition cursor-pointer shadow-lg disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Save Changes
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

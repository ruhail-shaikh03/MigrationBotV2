"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import { useSession } from "next-auth/react"
import { X } from "lucide-react"

/**
 * Triage screen for a project's person aliases.
 *
 * After shared cells are split, one person still appears under several spellings —
 * "Madiha" and "Madiha Shah Bukhari", "Abdullah Azfar" and "Abdullah Azfer". The app
 * never merges these on its own: merging hides one person inside another and the error
 * is silent, unlike a wrong split or a wrong role, which are visible immediately.
 *
 * So this screen suggests and a human decides. Where a name matches several people —
 * "Abdullah" matches three — every candidate is rendered identically with no default,
 * because presenting one as the obvious answer would be the app making exactly the
 * judgement it is not entitled to make.
 */

interface Project {
  id: number
  project_name: string
  schema_config?: { tabs?: Record<string, unknown> }
}
interface ObservedName { name: string; count: number }
interface Suggestion { name: string; candidates: string[]; reason: string }
interface AliasRow { id: number; alias: string; canonical: string }
interface PeopleResponse {
  tab: string
  names: ObservedName[]
  suggestions: Suggestion[]
  aliases: AliasRow[]
}

export default function AdminPeoplePage() {
  const { data: session } = useSession()
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState<number | null>(null)
  const [tab, setTab] = useState("")
  const [data, setData] = useState<PeopleResponse | null>(null)
  const [dismissed, setDismissed] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const apiToken = session?.apiToken || ""
  const googleToken = session?.googleAccessToken || ""

  const headers = useMemo(
    () => ({ Authorization: `Bearer ${apiToken}`, "X-Google-Access-Token": googleToken }),
    [apiToken, googleToken]
  )

  useEffect(() => {
    if (!apiToken) return
    fetch("/api/projects", { headers })
      .then((r) => r.json())
      .then((rows: Project[]) => {
        setProjects(rows)
        setProjectId((current) => (current === null && rows.length ? rows[0].id : current))
      })
      .catch(() => setError("Could not load projects."))
  }, [apiToken, headers])

  const tabs = useMemo(() => {
    const project = projects.find((p) => p.id === projectId)
    return Object.keys(project?.schema_config?.tabs || {})
  }, [projects, projectId])

  // Nothing sets state before the first await. The lint rule objects to a synchronous
  // setState inside an effect (it can cascade renders), and clearing the error eagerly at
  // the top — the obvious way to write this — is exactly that.
  const load = useCallback(async () => {
    if (!projectId || !apiToken) return
    try {
      const query = tab ? `?tab=${encodeURIComponent(tab)}` : ""
      const res = await fetch(`/api/projects/${projectId}/people${query}`, { headers })
      if (!res.ok) {
        throw new Error((await res.json().catch(() => ({}))).detail || "Failed to load.")
      }
      const body = await res.json()
      setData(body)
      setError("")
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load.")
      setData(null)
    }
  }, [projectId, tab, headers, apiToken])

  useEffect(() => {
    load()
  }, [load])

  const merge = async (alias: string, canonical: string) => {
    setBusy(true)
    try {
      const res = await fetch(`/api/projects/${projectId}/aliases`, {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify({ alias, canonical }),
      })
      if (!res.ok) {
        throw new Error((await res.json().catch(() => ({}))).detail || "Refused.")
      }
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save that merge.")
    } finally {
      setBusy(false)
    }
  }

  const removeAlias = async (id: number) => {
    setBusy(true)
    try {
      await fetch(`/api/projects/${projectId}/aliases/${id}`, { method: "DELETE", headers })
      await load()
    } catch {
      setError("Could not remove that merge.")
    } finally {
      setBusy(false)
    }
  }

  const visible = (data?.suggestions || []).filter((s) => !dismissed.has(s.name))
  const countFor = (name: string) => data?.names.find((n) => n.name === name)?.count ?? 0

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="display-md">People</h1>
        <p className="mt-1 max-w-2xl text-[13px] text-ink-400">
          Names as this sheet spells them. Merging changes how the app reads the sheet — the
          spreadsheet itself is never modified, and every merge can be undone.
        </p>
      </div>

      <div className="flex flex-wrap gap-3">
        <select
          aria-label="Project"
          value={projectId ?? ""}
          onChange={(e) => {
            setProjectId(Number(e.target.value))
            setTab("")
            setDismissed(new Set())
          }}
          className="field w-auto cursor-pointer py-1.5 text-[13px]"
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>{p.project_name}</option>
          ))}
        </select>

        {tabs.length > 1 && (
          <select
            aria-label="Tab"
            value={tab}
            onChange={(e) => {
              setTab(e.target.value)
              setDismissed(new Set())
            }}
            className="field w-auto cursor-pointer py-1.5 text-[13px]"
          >
            <option value="">Default tab</option>
            {tabs.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        )}
      </div>

      {error && <p className="text-sm text-failed">{error}</p>}

      <section className="space-y-3">
        <h2 className="label-micro">Suggested merges</h2>
        {visible.length === 0 && (
          <p className="text-[12px] italic text-ink-500">
            {data ? "Nothing looks like a duplicate." : "Loading…"}
          </p>
        )}
        {visible.map((s) => (
          <div key={s.name} className="card space-y-2 rounded-lg px-3 py-2.5">
            <div className="flex items-baseline gap-2">
              <span className="text-sm font-medium text-ink-100">{s.name}</span>
              <span className="text-[11px] text-ink-500">
                {countFor(s.name)} {countFor(s.name) === 1 ? "row" : "rows"} · {s.reason}
              </span>
            </div>

            {/* Ambiguity is stated, not hidden behind a best guess. */}
            {s.candidates.length > 1 && (
              <p className="text-[11px] text-ink-500">
                Matches {s.candidates.length} people — pick one, or leave separate.
              </p>
            )}

            <div className="flex flex-wrap gap-2">
              {s.candidates.map((c) => (
                <button
                  key={c}
                  disabled={busy}
                  onClick={() => merge(s.name, c)}
                  className="btn btn-secondary text-[12px] disabled:opacity-40"
                >
                  Merge into &ldquo;{c}&rdquo;
                  <span className="ml-1 text-ink-500">{countFor(c)}</span>
                </button>
              ))}
              <button
                onClick={() => setDismissed((d) => new Set(d).add(s.name))}
                className="btn btn-ghost text-[12px]"
              >
                Leave separate
              </button>
            </div>
          </div>
        ))}
      </section>

      <section className="space-y-2">
        <h2 className="label-micro">Recorded merges</h2>
        {(data?.aliases || []).length === 0 && (
          <p className="text-[12px] italic text-ink-500">None yet.</p>
        )}
        {(data?.aliases || []).map((a) => (
          <div key={a.id} className="flex items-center gap-2 text-[12.5px] text-ink-300">
            <code className="font-mono text-brass-300">{a.alias}</code>
            <span className="text-ink-500">→</span>
            <span>{a.canonical}</span>
            <button
              onClick={() => removeAlias(a.id)}
              disabled={busy}
              aria-label={`Remove merge of ${a.alias}`}
              className="text-ink-500 transition hover:text-failed disabled:opacity-40"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </section>

      <section className="space-y-2">
        <h2 className="label-micro">
          All names on this tab {data ? `(${data.names.length})` : ""}
        </h2>
        <div className="flex flex-wrap gap-1.5">
          {(data?.names || []).map((n) => (
            <span
              key={n.name}
              className="rounded border border-[var(--color-rule)] px-2 py-0.5 text-[12px] text-ink-300"
            >
              {n.name} <span className="tabular-nums text-ink-500">{n.count}</span>
            </span>
          ))}
        </div>
      </section>
    </div>
  )
}

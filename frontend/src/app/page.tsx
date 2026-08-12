"use client"

import { signIn, useSession } from "next-auth/react"
import { useRouter } from "next/navigation"
import { useEffect } from "react"

/**
 * The hero is the product itself: a question asked against a tracker, answered
 * with its source coordinates, and a change entering the write ledger. The three
 * feature cards this replaces described the implementation to the reader
 * ("queue-throttled writes", "RBAC & auditing"); this shows them what they'd do.
 */

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
      <div className="flex h-screen items-center justify-center bg-ink-950">
        <div className="label-micro animate-inflight">Checking your session</div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-ink-950">
      <div className="mx-auto grid max-w-6xl gap-14 px-6 py-16 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)] lg:items-center lg:gap-20 lg:py-24">
        {/* ---------------- Proposition ---------------- */}
        <div className="animate-rise">
          <div className="label-micro">Tracking sheets · plain language</div>

          <h1 className="display-lg mt-5">
            Ask your spreadsheet
            <br />
            what it knows.
          </h1>

          <p className="mt-6 max-w-md text-[15px] leading-relaxed text-ink-400">
            MigrationBot reads any tracking sheet your team keeps, answers questions about
            it, and makes the changes you ask for — with a record of who changed what.
          </p>

          <button
            onClick={() => signIn("google")}
            className="btn btn-invert mt-8 px-5 py-3 text-[14px]"
          >
            <svg className="h-[18px] w-[18px]" viewBox="0 0 24 24" aria-hidden="true">
              <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
              <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
              <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z" />
              <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
            </svg>
            Sign in with Google
          </button>
          <p className="mt-3 text-[12.5px] text-ink-500">
            MigrationBot uses your own Google account, so it can only open the sheets you
            can already open.
          </p>

          {/* Stated from the reader's side of the screen, not as a feature grid. */}
          <dl className="mt-10 space-y-4 border-t border-[var(--color-rule)] pt-8">
            {[
              ["Changes are queued", "Edits are applied one at a time, so a busy sheet never rejects yours."],
              ["Nothing happens silently", "Every change records who made it and what it replaced."],
              ["Scoped to your access", "You see and edit only the sheets and columns you've been given."],
            ].map(([term, detail]) => (
              <div key={term} className="grid gap-1 sm:grid-cols-[180px_minmax(0,1fr)] sm:gap-6">
                <dt className="text-[13px] font-medium text-ink-200">{term}</dt>
                <dd className="text-[13px] leading-relaxed text-ink-500">{detail}</dd>
              </div>
            ))}
          </dl>
        </div>

        {/* ---------------- The thesis: a transcript ---------------- */}
        <div className="panel animate-rise rounded-xl">
          <div className="flex items-center gap-2 border-b border-[var(--color-rule)] px-5 py-3">
            <span className="label-micro">FF Migration Tracker</span>
            <span className="stamp stamp-muted ml-auto">SD</span>
          </div>

          <div className="space-y-5 px-5 py-6">
            <div className="flex justify-end">
              <p className="bubble-user px-4 py-2.5 text-[14px]">which SD items are overdue?</p>
            </div>

            <div className="space-y-3 text-[14px] leading-relaxed">
              <p>
                Four items are past their due date.{" "}
                <strong className="font-semibold">FF-SD-014</strong> is the oldest, 12 days over.
              </p>

              <div className="well overflow-hidden rounded-lg">
                <table className="w-full text-left text-[12.5px]">
                  <thead>
                    <tr className="border-b border-[var(--color-rule)]">
                      <th className="label-micro px-3 py-2">ID</th>
                      <th className="label-micro px-3 py-2">Owner</th>
                      <th className="label-micro px-3 py-2 text-right">Days over</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--color-rule)]">
                    {[
                      ["FF-SD-014", "Sara", "12"],
                      ["FF-SD-031", "Amir", "6"],
                      ["FF-SD-042", "Priya", "3"],
                    ].map(([id, owner, days]) => (
                      <tr key={id}>
                        <td className="px-3 py-2 font-mono text-ink-200">{id}</td>
                        <td className="px-3 py-2 text-ink-400">{owner}</td>
                        <td className="px-3 py-2 text-right text-ink-200">{days}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="flex items-center gap-2">
                <span className="stamp">SD!A3:N145</span>
                <span className="label-micro">read 142 rows</span>
              </div>
            </div>

            <div className="flex justify-end">
              <p className="bubble-user px-4 py-2.5 text-[14px]">mark FF-SD-042 as done</p>
            </div>

            {/* The write ledger — the one thing to remember about this product. */}
            <div className="card rounded-lg px-3.5 py-3">
              <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-[13px]">
                <span className="stamp">SD!H44</span>
                <span className="text-ink-400">Dev Status</span>
                <span className="text-ink-500 line-through">In Progress</span>
                <span className="text-ink-500">→</span>
                <span className="font-medium text-ink-100">Done</span>
                <span className="status status-applied ml-auto">applied</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <footer className="border-t border-[var(--color-rule)]">
        <div className="mx-auto max-w-6xl px-6 py-6">
          <span className="label-micro">MigrationBot · {new Date().getFullYear()}</span>
        </div>
      </footer>
    </div>
  )
}

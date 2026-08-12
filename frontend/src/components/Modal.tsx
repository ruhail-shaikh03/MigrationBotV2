"use client"

import { useCallback, useEffect, useRef } from "react"
import { X } from "lucide-react"

/**
 * Accessible dialog shell.
 *
 * Both admin modals previously hand-rolled their own overlay and each got the
 * scrolling wrong in a different way:
 *
 *   - users/page.tsx centred a panel with no max-height and no scroll container,
 *     so on any viewport shorter than the form the panel overflowed *both* edges
 *     and the top — including the first field — became unreachable.
 *   - projects/page.tsx put `overflow-y-auto` on a flex container that also had
 *     `items-center`. That is the classic centring trap: once the child is taller
 *     than the container, centring pushes its top above the scroll origin, and
 *     no amount of scrolling brings it back.
 *
 * The fix for both is the same and is the only layout here worth memorising: the
 * scroll lives on the OVERLAY, and centring is done by a `min-h-full` flex
 * wrapper *inside* it rather than by the scroll container itself. Then the panel
 * is free to be taller than the viewport — the overlay scrolls to reveal it —
 * while `max-h` plus an internal scroll region keeps the common case pinned.
 */

type ModalSize = "md" | "lg" | "xl"

const SIZE_CLASS: Record<ModalSize, string> = {
  md: "max-w-md",
  lg: "max-w-xl",
  xl: "max-w-2xl",
}

// Elements that can hold focus, for the Tab trap below.
const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'

interface ModalProps {
  open: boolean
  onClose: () => void
  title: string
  /** Optional lucide icon rendered before the title. */
  icon?: React.ReactNode
  /** Small muted line under the title. */
  description?: string
  /** Pinned below the scroll region — action buttons belong here, not in the body. */
  footer?: React.ReactNode
  /** Rendered between the title bar and the footer; this is the part that scrolls. */
  children: React.ReactNode
  size?: ModalSize
  /**
   * Whether clicking the backdrop closes the dialog. Turn it off for long forms
   * where a stray click would silently discard work the user can't easily redo.
   */
  dismissOnBackdrop?: boolean
}

export default function Modal({
  open,
  onClose,
  title,
  icon,
  description,
  footer,
  children,
  size = "lg",
  dismissOnBackdrop = true,
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement | null>(null)
  // Whatever had focus before we opened, so it can be handed back on close.
  const restoreFocusRef = useRef<HTMLElement | null>(null)

  const titleId = `modal-title-${title.replace(/\W+/g, "-").toLowerCase()}`

  // Escape to close, and keep Tab inside the panel while it is open.
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation()
        onClose()
        return
      }
      if (e.key !== "Tab" || !panelRef.current) return

      const focusable = Array.from(
        panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)
      ).filter((el) => el.offsetParent !== null)
      if (focusable.length === 0) return

      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement

      // Wrap around rather than letting focus escape to the page behind.
      if (e.shiftKey && (active === first || !panelRef.current.contains(active))) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    },
    [onClose]
  )

  useEffect(() => {
    if (!open) return

    restoreFocusRef.current = document.activeElement as HTMLElement | null

    // Lock the page behind the overlay. Without this the background scrolls
    // under the modal on wheel/trackpad, which reads as the modal drifting.
    const { overflow: prevOverflow } = document.body.style
    document.body.style.overflow = "hidden"

    document.addEventListener("keydown", handleKeyDown, true)

    // Focus the first real control, falling back to the panel itself so screen
    // readers announce the dialog rather than leaving focus on the page behind.
    const focusTimer = window.setTimeout(() => {
      const target =
        panelRef.current?.querySelector<HTMLElement>(FOCUSABLE) ?? panelRef.current
      target?.focus()
    }, 0)

    return () => {
      window.clearTimeout(focusTimer)
      document.removeEventListener("keydown", handleKeyDown, true)
      document.body.style.overflow = prevOverflow
      restoreFocusRef.current?.focus?.()
    }
  }, [open, handleKeyDown])

  if (!open) return null

  return (
    // Scroll lives here, on the overlay — see the note at the top of this file.
    <div
      className="fixed inset-0 z-50 overflow-y-auto overscroll-contain bg-black/70 backdrop-blur-sm"
      // mousedown, not click: a click that STARTS inside the panel and ends on the
      // overlay (drag-selecting text in a field) would otherwise close the dialog.
      onMouseDown={(e) => {
        if (dismissOnBackdrop && e.target === e.currentTarget) onClose()
      }}
    >
      <div className="flex min-h-full items-start justify-center p-4 sm:items-center sm:p-6">
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          tabIndex={-1}
          className={`glass-panel relative flex w-full ${SIZE_CLASS[size]} max-h-[calc(100dvh-2rem)] flex-col overflow-hidden rounded-2xl border border-white/10 shadow-2xl outline-none sm:max-h-[calc(100dvh-3rem)]`}
        >
          {/* Title bar — fixed, never scrolls away. `pr-12` reserves room for the
              close button so a long title can't run underneath it. */}
          <div className="shrink-0 border-b border-white/5 px-6 py-5 pr-12 sm:px-8">
            <h3
              id={titleId}
              className="flex items-center gap-2 text-lg font-bold text-zinc-100"
            >
              {icon}
              <span className="min-w-0">{title}</span>
            </h3>
            {description && (
              <p className="mt-1 text-xs leading-relaxed text-zinc-500">{description}</p>
            )}
          </div>

          <button
            type="button"
            onClick={onClose}
            aria-label="Close dialog"
            className="absolute right-5 top-5 rounded-lg p-1.5 text-zinc-400 transition hover:bg-white/5 hover:text-zinc-200 sm:right-6"
          >
            <X className="h-5 w-5" />
          </button>

          {/* The only scrolling region. min-h-0 is required — without it a flex
              child refuses to shrink below its content and the scroll never
              engages, which is how the panel ends up taller than the viewport. */}
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5 sm:px-8">{children}</div>

          {footer && (
            <div className="shrink-0 border-t border-white/5 bg-black/20 px-6 py-4 sm:px-8">
              {footer}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

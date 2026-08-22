import { useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

export interface SourceLink {
  label: string
  url: string
}

interface Props {
  text: string
  links?: SourceLink[]
}

const POP_WIDTH = 300
const CLOSE_DELAY_MS = 150

/** Small "?" icon with a hover tooltip explaining the data source, with web links.
 *  Rendered through a portal with fixed positioning so it is never clipped by
 *  the sidebar's overflow or stacked under the chart canvas. */
export default function SourceHint({ text, links }: Props) {
  const iconRef = useRef<HTMLSpanElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const closeTimerRef = useRef<number | undefined>(undefined)
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)

  const cancelClose = () => {
    if (closeTimerRef.current != null) {
      window.clearTimeout(closeTimerRef.current)
      closeTimerRef.current = undefined
    }
  }

  const show = () => {
    cancelClose()
    setOpen(true)
  }

  const scheduleHide = () => {
    cancelClose()
    closeTimerRef.current = window.setTimeout(() => setOpen(false), CLOSE_DELAY_MS)
  }

  // position after mount (popup measured while hidden, then revealed)
  useLayoutEffect(() => {
    if (!open || !iconRef.current) return
    const icon = iconRef.current.getBoundingClientRect()
    const width = Math.min(POP_WIDTH, window.innerWidth - 16)
    let left = icon.right - width
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8))
    const popH = popRef.current?.offsetHeight ?? 0
    const below = icon.bottom + 8
    const fitsBelow = below + popH <= window.innerHeight - 8
    const above = icon.top - 8 - popH
    const top = fitsBelow || above < 8 ? below : above
    setPos({ top, left })
  }, [open, text])

  useLayoutEffect(() => cancelClose, [])

  return (
    <span
      className="source-hint"
      ref={iconRef}
      onMouseEnter={show}
      onMouseLeave={scheduleHide}
    >
      ?
      {open &&
        createPortal(
          <div
            className={`source-hint-pop${pos ? ' source-hint-pop-visible' : ''}`}
            ref={popRef}
            style={pos ? { top: pos.top, left: pos.left } : undefined}
            onMouseEnter={show}
            onMouseLeave={scheduleHide}
          >
            <span className="source-hint-text">{text}</span>
            {links && links.length > 0 && (
              <span className="source-hint-links">
                {links.map((l) => (
                  <a
                    key={l.url}
                    className="source-hint-link"
                    href={l.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {l.label} ↗
                  </a>
                ))}
              </span>
            )}
          </div>,
          document.body,
        )}
    </span>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Segment } from '../api'

const DAY_MS = 24 * 60 * 60 * 1000
const MIN_SPAN = 30_000 // most-zoomed-in window (30s)
const DBLCLICK_SPAN = 10 * 60 * 1000 // double-click-to-zoom target (~2 segments)

interface Props {
  /** Local midnight (epoch ms) of the day being shown. */
  dayStart: number
  segments: Segment[]
  currentTimeMs: number
  /** Fired continuously while dragging (live scrub). */
  onScrub: (timeMs: number) => void
  /** Fired once when the drag/click settles. */
  onScrubEnd: (timeMs: number) => void
}

function fmt(ms: number): string {
  const d = new Date(ms)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// Candidate tick spacings (ms); pick the smallest that yields a readable density.
const TICK_STEPS = [
  30_000, 60_000, 2 * 60_000, 5 * 60_000, 10 * 60_000, 15 * 60_000, 30 * 60_000,
  60 * 60_000, 2 * 60 * 60_000, 3 * 60 * 60_000, 6 * 60 * 60_000,
]
function tickStepFor(span: number): number {
  const target = span / 8 // aim for ~8 ticks across the bar
  return TICK_STEPS.find((s) => s >= target) ?? DAY_MS
}
function tickLabel(ms: number, step: number): string {
  const d = new Date(ms)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  if (step < 60_000) return `${hh}:${mm}:${String(d.getSeconds()).padStart(2, '0')}`
  return `${hh}:${mm}`
}
function spanLabel(span: number): string {
  if (span < 60_000) return `${Math.round(span / 1000)}s`
  if (span < 60 * 60_000) return `${Math.round(span / 60_000)}m`
  return `${+(span / 3_600_000).toFixed(1)}h`
}

export default function Timeline({
  dayStart,
  segments,
  currentTimeMs,
  onScrub,
  onScrubEnd,
}: Props) {
  const barRef = useRef<HTMLDivElement>(null)
  const ovRef = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)
  const ovDragging = useRef(false)
  // Active pointers on the detail bar (id -> clientX); 2 = pinch-zoom.
  const pointers = useRef<Map<number, number>>(new Map())
  const pinch = useRef<{ span: number; dist: number; anchor: number } | null>(null)

  // The visible window into the day. span === DAY_MS means fully zoomed out.
  const [view, setView] = useState({ start: dayStart, span: DAY_MS })
  const zoomed = view.span < DAY_MS - 1
  const viewEnd = view.start + view.span

  // Reset zoom when the day changes.
  useEffect(() => setView({ start: dayStart, span: DAY_MS }), [dayStart])

  const clampView = useCallback(
    (start: number, span: number) => {
      const s = Math.min(DAY_MS, Math.max(MIN_SPAN, span))
      const st = Math.min(dayStart + DAY_MS - s, Math.max(dayStart, start))
      return { start: st, span: s }
    },
    [dayStart],
  )

  // --- detail bar coordinate mapping (uses the zoomed window) ---
  const timeFromClientX = useCallback(
    (clientX: number): number => {
      const el = barRef.current
      if (!el) return view.start
      const rect = el.getBoundingClientRect()
      const frac = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
      return view.start + frac * view.span
    },
    [view.start, view.span],
  )
  const fracOf = (ms: number) => (ms - view.start) / view.span

  // --- zoom: mouse wheel (anchored on the cursor), non-passive so we can preventDefault ---
  useEffect(() => {
    const el = barRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
      const factor = Math.exp(e.deltaY * 0.0015) // up → zoom in, down → zoom out
      setView((v) => {
        const anchor = v.start + frac * v.span
        const span = Math.min(DAY_MS, Math.max(MIN_SPAN, v.span * factor))
        return clampView(anchor - frac * span, span)
      })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [clampView])

  // Keep the playhead in view while it advances (only when zoomed and idle).
  useEffect(() => {
    if (dragging.current) return
    setView((v) => {
      if (v.span >= DAY_MS - 1) return v
      if (currentTimeMs >= v.start && currentTimeMs <= v.start + v.span) return v
      return clampView(currentTimeMs - v.span * 0.75, v.span)
    })
  }, [currentTimeMs, clampView])

  // --- detail bar pointer handling (scrub, or pinch-zoom with two fingers) ---
  const onPointerDown = (e: React.PointerEvent) => {
    barRef.current?.setPointerCapture(e.pointerId)
    pointers.current.set(e.pointerId, e.clientX)
    if (pointers.current.size === 2) {
      dragging.current = false // second finger → pinch, cancel scrub
      const xs = [...pointers.current.values()]
      const midX = (xs[0] + xs[1]) / 2
      pinch.current = { span: view.span, dist: Math.abs(xs[0] - xs[1]) || 1, anchor: timeFromClientX(midX) }
      return
    }
    dragging.current = true
    onScrub(timeFromClientX(e.clientX))
  }
  const onPointerMove = (e: React.PointerEvent) => {
    if (pointers.current.has(e.pointerId)) pointers.current.set(e.pointerId, e.clientX)
    if (pinch.current && pointers.current.size >= 2) {
      const el = barRef.current
      if (!el) return
      const xs = [...pointers.current.values()]
      const dist = Math.abs(xs[0] - xs[1]) || 1
      const rect = el.getBoundingClientRect()
      const frac = Math.min(1, Math.max(0, ((xs[0] + xs[1]) / 2 - rect.left) / rect.width))
      const span = Math.min(DAY_MS, Math.max(MIN_SPAN, pinch.current.span * (pinch.current.dist / dist)))
      setView(() => clampView(pinch.current!.anchor - frac * span, span))
      return
    }
    if (!dragging.current) return
    onScrub(timeFromClientX(e.clientX))
  }
  const onPointerUp = (e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId)
    if (pointers.current.size < 2) pinch.current = null
    if (dragging.current && pointers.current.size === 0) {
      dragging.current = false
      onScrubEnd(timeFromClientX(e.clientX))
    }
  }
  const onDoubleClick = (e: React.MouseEvent) => {
    const t = timeFromClientX(e.clientX)
    setView((v) =>
      v.span < DAY_MS - 1 ? { start: dayStart, span: DAY_MS } : clampView(t - DBLCLICK_SPAN / 2, DBLCLICK_SPAN),
    )
  }

  // --- overview strip (full day): drag to pan the detail window ---
  const ovPanTo = (clientX: number) => {
    const el = ovRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const frac = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
    const t = dayStart + frac * DAY_MS
    setView((v) => clampView(t - v.span / 2, v.span))
  }
  const onOvDown = (e: React.PointerEvent) => {
    ovDragging.current = true
    ovRef.current?.setPointerCapture(e.pointerId)
    ovPanTo(e.clientX)
  }
  const onOvMove = (e: React.PointerEvent) => ovDragging.current && ovPanTo(e.clientX)
  const onOvUp = () => (ovDragging.current = false)

  const dayPct = (ms: number) => `${((ms - dayStart) / DAY_MS) * 100}%`
  const step = tickStepFor(view.span)
  const firstTick = dayStart + Math.ceil((view.start - dayStart) / step) * step
  const ticks: number[] = []
  for (let t = firstTick; t <= viewEnd; t += step) ticks.push(t)
  const playheadInView = currentTimeMs >= view.start && currentTimeMs <= viewEnd

  return (
    <div className="timeline">
      <div className="timeline-time">
        {fmt(currentTimeMs)}
        {zoomed && (
          <button
            className="timeline-zoomreset"
            onClick={() => setView({ start: dayStart, span: DAY_MS })}
            title="Reset zoom (or double-click the bar)"
          >
            ⤢ {spanLabel(view.span)} view · reset
          </button>
        )}
        {!zoomed && <span className="timeline-hint">scroll / pinch to zoom</span>}
      </div>

      {/* Overview: whole day with a draggable viewport box (shown when zoomed). */}
      {zoomed && (
        <div
          className="timeline-overview"
          ref={ovRef}
          onPointerDown={onOvDown}
          onPointerMove={onOvMove}
          onPointerUp={onOvUp}
        >
          {segments.map((s) => (
            <div
              key={s.id}
              className={s.cached ? 'timeline-seg cached' : 'timeline-seg'}
              style={{ left: dayPct(s.start), width: `${((s.end - s.start) / DAY_MS) * 100}%` }}
            />
          ))}
          <div
            className="timeline-viewport"
            style={{ left: dayPct(view.start), width: `${(view.span / DAY_MS) * 100}%` }}
          />
          <div className="timeline-ovplayhead" style={{ left: dayPct(currentTimeMs) }} />
        </div>
      )}

      {/* Detail bar: the zoomed window. Drag = scrub, wheel/pinch = zoom. */}
      <div
        className="timeline-bar"
        ref={barRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={onDoubleClick}
      >
        {segments.map((s) => {
          const left = fracOf(s.start)
          const right = fracOf(s.end)
          if (right < 0 || left > 1) return null // outside the zoom window
          return (
            <div
              key={s.id}
              className={s.cached ? 'timeline-seg cached' : 'timeline-seg'}
              style={{ left: `${left * 100}%`, width: `${(right - left) * 100}%` }}
            />
          )
        })}

        {ticks.map((t) => (
          <div key={t} className="timeline-tick" style={{ left: `${fracOf(t) * 100}%` }}>
            <span>{tickLabel(t, step)}</span>
          </div>
        ))}

        {playheadInView && (
          <div className="timeline-playhead" style={{ left: `${fracOf(currentTimeMs) * 100}%` }} />
        )}
      </div>
    </div>
  )
}

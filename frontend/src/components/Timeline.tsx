import { useCallback, useRef } from 'react'
import type { Segment } from '../api'

const DAY_MS = 24 * 60 * 60 * 1000

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

export default function Timeline({
  dayStart,
  segments,
  currentTimeMs,
  onScrub,
  onScrubEnd,
}: Props) {
  const barRef = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)

  const timeFromClientX = useCallback(
    (clientX: number): number => {
      const el = barRef.current
      if (!el) return dayStart
      const rect = el.getBoundingClientRect()
      const frac = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
      return dayStart + frac * DAY_MS
    },
    [dayStart],
  )

  const onPointerDown = (e: React.PointerEvent) => {
    dragging.current = true
    barRef.current?.setPointerCapture(e.pointerId)
    onScrub(timeFromClientX(e.clientX))
  }
  const onPointerMove = (e: React.PointerEvent) => {
    if (!dragging.current) return
    onScrub(timeFromClientX(e.clientX))
  }
  const onPointerUp = (e: React.PointerEvent) => {
    if (!dragging.current) return
    dragging.current = false
    onScrubEnd(timeFromClientX(e.clientX))
  }

  const pct = (ms: number) => `${((ms - dayStart) / DAY_MS) * 100}%`
  const playheadPct = Math.min(100, Math.max(0, ((currentTimeMs - dayStart) / DAY_MS) * 100))

  return (
    <div className="timeline">
      <div className="timeline-time">{fmt(currentTimeMs)}</div>
      <div
        className="timeline-bar"
        ref={barRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
      >
        {/* Recorded regions */}
        {segments.map((s) => (
          <div
            key={s.id}
            className={s.cached ? 'timeline-seg cached' : 'timeline-seg'}
            style={{ left: pct(s.start), width: `${((s.end - s.start) / DAY_MS) * 100}%` }}
          />
        ))}

        {/* Hour ticks */}
        {Array.from({ length: 25 }, (_, h) => (
          <div key={h} className="timeline-tick" style={{ left: `${(h / 24) * 100}%` }}>
            {h % 3 === 0 && h < 24 && <span>{String(h).padStart(2, '0')}</span>}
          </div>
        ))}

        {/* Playhead */}
        <div className="timeline-playhead" style={{ left: `${playheadPct}%` }} />
      </div>
    </div>
  )
}

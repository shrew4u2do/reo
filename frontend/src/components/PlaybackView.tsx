import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ensureClip,
  fetchRecordings,
  prefetch,
  supportsHevc,
  thumbnailUrl,
  type Camera,
  type Segment,
} from '../api'
import LiveTile from './LiveTile'
import Timeline from './Timeline'

const USE_HEVC = supportsHevc() // native 4K HEVC if the browser can decode it, else 1080p H.264
const QUALITY_LABEL = USE_HEVC ? '4K' : 'HD'

function todayStr(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
function dayStartMs(date: string): number {
  return new Date(`${date}T00:00:00`).getTime()
}

interface Props {
  camera: Camera
}

/**
 * Recorded playback from the NAS. The timeline maps wall-clock time to one of the
 * ~10-min MP4 segments; each is remuxed once into a seekable clip and played in a
 * plain <video>. Seeking within a segment is an instant currentTime change;
 * crossing a boundary loads the next clip. Scrubbing previews the per-minute JPEG.
 */
export default function PlaybackView({ camera }: Props) {
  const [date, setDate] = useState(todayStr())
  const [segments, setSegments] = useState<Segment[]>([])
  const [currentTimeMs, setCurrentTimeMs] = useState(dayStartMs(date))
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [scrubNote, setScrubNote] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [live, setLive] = useState(false) // showing the real-time go2rtc stream

  const videoRef = useRef<HTMLVideoElement>(null)
  const dragging = useRef(false)
  const loadedSeg = useRef<Segment | null>(null) // segment whose clip is in <video>
  const reqId = useRef(0) // guards against out-of-order async loads
  const scrubTargetMs = useRef(0) // latest cursor time while dragging
  const previewSegId = useRef<string | null>(null) // segment currently being loaded
  const previewTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const openAtEnd = useRef(false) // next auto-open should land at the live edge

  const dStart = dayStartMs(date)

  const segAt = useCallback(
    (t: number): Segment | undefined => segments.find((s) => t >= s.start && t <= s.end),
    [segments],
  )

  /** Seek the loaded clip to wherever the cursor is now (instant, in-buffer). */
  const applyPreviewSeek = useCallback(() => {
    const v = videoRef.current
    const seg = loadedSeg.current
    if (!v || !seg) return
    const off = (scrubTargetMs.current - seg.start) / 1000
    if (off >= 0) {
      try {
        v.currentTime = v.duration ? Math.min(off, v.duration - 0.05) : off
      } catch {
        /* not seekable yet */
      }
    }
  }, [])

  /** Load one segment's clip and seek into it. While dragging, seek follows the
   *  cursor (live preview, no autoplay); otherwise seek to `offsetSec` and play. */
  const loadSegment = useCallback(
    (seg: Segment, offsetSec: number, autoplay = true) => {
      const v = videoRef.current
      if (!v) return
      const myId = ++reqId.current
      previewSegId.current = seg.id
      setLoading(true)
      setError(null)
      setScrubNote(null)
      setCurrentTimeMs(seg.start + Math.max(0, offsetSec) * 1000)
      // Warm the neighbours so crossing into them is instant too.
      const i = segments.indexOf(seg)
      prefetch(
        camera.slug,
        [segments[i - 1]?.id, segments[i + 1]?.id].filter(Boolean) as string[],
        USE_HEVC,
      )
      ensureClip(camera.slug, seg.id, USE_HEVC)
        .then((url) => {
          if (myId !== reqId.current) return // superseded by a newer load
          v.src = url
          const onready = () => {
            v.removeEventListener('loadeddata', onready)
            if (myId !== reqId.current) return
            loadedSeg.current = seg
            previewSegId.current = null
            setLoading(false)
            if (dragging.current) {
              applyPreviewSeek() // jump the big player to the live cursor
            } else {
              try {
                v.currentTime = v.duration ? Math.min(offsetSec, v.duration - 0.05) : offsetSec
              } catch {
                /* not seekable yet */
              }
              if (autoplay) v.play().catch(() => {})
            }
          }
          v.addEventListener('loadeddata', onready)
          v.load()
        })
        .catch((e: unknown) => {
          if (myId === reqId.current) {
            previewSegId.current = null
            setLoading(false)
            setError(e instanceof Error ? e.message : String(e))
          }
        })
    },
    [camera.slug, segments, applyPreviewSeek],
  )

  /** Seek to wall-clock t: instant within the loaded segment, else load its clip. */
  const seekTo = useCallback(
    (t: number) => {
      const v = videoRef.current
      if (!v) return
      const seg = segAt(t)
      if (!seg) {
        setScrubNote('no recording here')
        return
      }
      const cur = loadedSeg.current
      if (cur && seg.id === cur.id) {
        const off = (t - seg.start) / 1000
        try {
          v.currentTime = v.duration ? Math.min(off, v.duration - 0.05) : off
        } catch {
          /* not seekable yet */
        }
        setCurrentTimeMs(t)
        v.play().catch(() => {})
      } else {
        loadSegment(seg, (t - seg.start) / 1000)
      }
    },
    [segAt, loadSegment],
  )

  /** Nudge the playhead by ±seconds (instant within a segment; loads across one). */
  const nudge = (deltaSec: number) => {
    if (!ready) return
    setLive(false)
    seekTo(currentTimeMs + deltaSec * 1000)
  }

  // Load the day's segments; open the latest once ready.
  useEffect(() => {
    let cancelled = false
    setReady(false)
    setError(null)
    setSegments([])
    loadedSeg.current = null
    previewSegId.current = null
    if (previewTimer.current) clearTimeout(previewTimer.current)
    reqId.current++ // cancel any in-flight load from the previous day
    fetchRecordings(camera.slug, date)
      .then((d) => {
        if (cancelled) return
        setSegments(d.segments)
        const latest = d.segments[d.segments.length - 1]
        setCurrentTimeMs(latest ? latest.start : dayStartMs(date))
        setReady(true)
      })
      .catch((e) => !cancelled && setError(String(e)))
    return () => {
      cancelled = true
    }
  }, [camera.slug, date])

  // Auto-open the latest segment once the day's list is in (at the live edge if a
  // "Now" jump asked for it, otherwise at the segment start).
  useEffect(() => {
    if (ready && segments.length > 0 && !loadedSeg.current) {
      const last = segments[segments.length - 1]
      loadSegment(last, openAtEnd.current ? Math.max(0, last.durationSec - 5) : 0)
      openAtEnd.current = false
    }
  }, [ready, segments, loadSegment])

  // Clear any pending preview-load timer on unmount.
  useEffect(() => () => { if (previewTimer.current) clearTimeout(previewTimer.current) }, [])

  // While live, keep the playhead pinned to real time (the right edge).
  useEffect(() => {
    if (!live) return
    setCurrentTimeMs(Date.now())
    const id = setInterval(() => setCurrentTimeMs(Date.now()), 1000)
    return () => clearInterval(id)
  }, [live])

  const onScrub = (t: number) => {
    setLive(false) // scrubbing leaves live and returns to recorded
    dragging.current = true
    scrubTargetMs.current = t
    setCurrentTimeMs(t) // move playhead live; Timeline shows the JPEG preview
    const seg = segAt(t)
    if (!seg) {
      setScrubNote('no recording here')
      return
    }
    setScrubNote(null)
    if (loadedSeg.current && seg.id === loadedSeg.current.id) {
      applyPreviewSeek() // big player follows the cursor instantly within the clip
      return
    }
    if (previewSegId.current === seg.id) return // already loading this segment
    // Debounce cross-segment loads: only load a segment the cursor settles on, so a
    // fast sweep across the day doesn't kick off a remux for every segment passed.
    if (previewTimer.current) clearTimeout(previewTimer.current)
    previewTimer.current = setTimeout(() => {
      if (!dragging.current) return
      const cur = segAt(scrubTargetMs.current)
      if (cur && cur.id === seg.id && loadedSeg.current?.id !== seg.id && previewSegId.current !== seg.id) {
        loadSegment(seg, (scrubTargetMs.current - seg.start) / 1000, false)
      }
    }, 150)
  }

  const onScrubEnd = (t: number) => {
    dragging.current = false
    if (previewTimer.current) clearTimeout(previewTimer.current)
    scrubTargetMs.current = t
    seekTo(t)
  }

  const onTimeUpdate = () => {
    if (dragging.current) return
    const v = videoRef.current
    const seg = loadedSeg.current
    if (v && seg) setCurrentTimeMs(seg.start + v.currentTime * 1000)
  }

  // At a segment's end, roll into the next one for continuous playback.
  const onEnded = () => {
    const seg = loadedSeg.current
    if (!seg) return
    const next = segments[segments.indexOf(seg) + 1]
    if (next) loadSegment(next, 0)
  }

  /** Go live: show the real-time go2rtc stream (switching to today's timeline). */
  const goNow = () => {
    const today = todayStr()
    if (date !== today) {
      openAtEnd.current = false
      setDate(today)
    }
    videoRef.current?.pause()
    setError(null)
    setScrubNote(null)
    setCurrentTimeMs(Date.now())
    setLive(true)
  }

  const shiftDay = (delta: number) => {
    setLive(false)
    openAtEnd.current = false
    const d = new Date(dStart + delta * 86400000)
    setDate(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`)
  }

  return (
    <div className="playback">
      <div className="playback-toolbar">
        <button onClick={() => shiftDay(-1)}>‹ Prev</button>
        <input type="date" value={date} onChange={(e) => { setLive(false); openAtEnd.current = false; setDate(e.target.value) }} />
        <button onClick={() => shiftDay(1)}>Next ›</button>
        <span className="playback-camname">{camera.name}</span>
        <span className="hd-toggle on">{live ? 'LIVE' : QUALITY_LABEL}</span>
        {ready && <span className="playback-count">{segments.length} segments</span>}
      </div>

      <div className="playback-video">
        {live ? (
          <LiveTile stream={camera.streams.hd} muted={false} />
        ) : (
          <video ref={videoRef} controls playsInline onTimeUpdate={onTimeUpdate} onEnded={onEnded}
                 onPlaying={() => setLoading(false)} />
        )}
        {!live && loading && !scrubNote && <div className="buffering">loading…</div>}
        {!live && scrubNote && <div className="buffering">{scrubNote}</div>}
        {error && <div className="buffering error">{error}</div>}
        {!live && ready && segments.length === 0 && <div className="buffering">No recordings this day</div>}
      </div>

      <div className="nudge-row">
        <button className="nudge" onClick={() => nudge(-15)} disabled={!ready} title="Back 15 seconds">« 15s</button>
        <button className={live ? 'now-btn on' : 'now-btn'} onClick={goNow} title="Show the live view">
          ● {live ? 'LIVE' : 'Now'}
        </button>
        <button className="nudge" onClick={() => nudge(15)} disabled={!ready} title="Forward 15 seconds">15s »</button>
      </div>

      <Timeline
        dayStart={dStart}
        segments={segments}
        currentTimeMs={currentTimeMs}
        thumbUrl={(ms) => thumbnailUrl(camera.slug, ms)}
        onScrub={onScrub}
        onScrubEnd={onScrubEnd}
      />
    </div>
  )
}

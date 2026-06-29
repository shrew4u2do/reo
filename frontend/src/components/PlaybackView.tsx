import { useCallback, useEffect, useRef, useState } from 'react'
import {
  bcplayUrl,
  clipUrl,
  ensureClip,
  fetchRecordings,
  prefetch,
  seekStream,
  type Camera,
  type Segment,
} from '../api'
import { startMsePlayback, mseSupportsHevc } from '../lib/mse'
import Timeline from './Timeline'

const USE_HEVC = mseSupportsHevc() // native 4K HEVC if the browser's MSE supports it
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
 * Recorded playback over the native Baichuan replay, app/Nest-style: ONE stream
 * stays open and the timeline repositions it in place (instant seek, exact
 * second, full quality).
 */
export default function PlaybackView({ camera }: Props) {
  const [date, setDate] = useState(todayStr())
  const [segments, setSegments] = useState<Segment[]>([])
  const [currentTimeMs, setCurrentTimeMs] = useState(dayStartMs(date))
  const [quality, setQuality] = useState<'main' | 'sub'>('main')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)

  const videoRef = useRef<HTMLVideoElement>(null)
  const dragging = useRef(false)
  const stopStream = useRef<(() => void) | null>(null)
  const streamOpen = useRef(false)
  const watchdog = useRef<ReturnType<typeof setTimeout> | null>(null)
  const attempt = useRef(0)
  // Playhead = anchorWall + (video.currentTime - anchorCt) * 1000.
  const anchorWall = useRef(0)
  const anchorCt = useRef(0)
  // Scrub preview: while dragging we show the cached low-res SUB clip and seek
  // within it (instant once loaded); HD loads on release.
  const previewSegId = useRef<string | null>(null)
  const scrubTargetMs = useRef(0)

  const dStart = dayStartMs(date)

  const segAt = useCallback(
    (t: number): Segment | undefined => segments.find((s) => t >= s.start && t <= s.end),
    [segments],
  )

  // Load the day's recording availability (sub list) for the timeline.
  useEffect(() => {
    let cancelled = false
    setReady(false)
    setError(null)
    fetchRecordings(camera.slug, date, 'sub')
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

  /** (Re)open the single playback stream starting at wall-clock t. */
  const openStream = useCallback(
    (t: number, q: 'main' | 'sub', isRetry = false) => {
      const video = videoRef.current
      if (!video) return
      stopStream.current?.()
      if (watchdog.current) clearTimeout(watchdog.current)
      if (!isRetry) attempt.current = 0
      anchorWall.current = t
      anchorCt.current = 0
      setCurrentTimeMs(t)
      setLoading(true)
      setError(null)
      stopStream.current = startMsePlayback(video, bcplayUrl(camera.slug, t, q, USE_HEVC), USE_HEVC)
      streamOpen.current = true
      video.play().catch(() => {})
      // Watchdog: if the Hub serves no media for this open, retry rather than hang.
      watchdog.current = setTimeout(() => {
        const v = videoRef.current
        if (v && v.currentTime > anchorCt.current + 0.1) return
        if (attempt.current < 3) {
          attempt.current += 1
          openStream(t, q, true)
        } else {
          setLoading(false)
          setError('Hub busy — click the timeline to retry')
        }
      }, 8000)
    },
    [camera.slug],
  )

  /** Seek: reposition the open stream in place (or open it if not yet started). */
  const seekTo = useCallback(
    (t: number, q: 'main' | 'sub') => {
      const video = videoRef.current
      if (!video || !streamOpen.current) {
        openStream(t, q)
        return
      }
      anchorWall.current = t
      anchorCt.current = video.currentTime
      setCurrentTimeMs(t)
      setLoading(true)
      // Try in-place (instant); if it crossed a file boundary, reopen the stream.
      seekStream(t).then((inplace) => {
        if (!inplace) openStream(t, q)
      })
    },
    [openStream],
  )

  // Auto-open at the latest moment once recordings load.
  useEffect(() => {
    if (ready && segments.length > 0 && !streamOpen.current) {
      openStream(segments[segments.length - 1].start, quality)
    }
  }, [ready, segments, quality, openStream])

  useEffect(
    () => () => {
      stopStream.current?.()
      if (watchdog.current) clearTimeout(watchdog.current)
    },
    [],
  )

  /** Seek the loaded preview clip to wherever the cursor is now (instant). */
  const applyPreviewSeek = useCallback(() => {
    const v = videoRef.current
    if (!v) return
    const seg = segAt(scrubTargetMs.current)
    if (!seg || seg.id !== previewSegId.current) return
    const off = (scrubTargetMs.current - seg.start) / 1000
    if (off >= 0) {
      try {
        v.currentTime = v.duration ? Math.min(off, v.duration - 0.05) : off
      } catch {
        /* not seekable yet */
      }
    }
  }, [segAt])

  const onScrub = (t: number) => {
    dragging.current = true
    scrubTargetMs.current = t
    setCurrentTimeMs(t) // move playhead live
    const seg = segAt(t)
    const v = videoRef.current
    if (!seg || !v) return
    if (previewSegId.current === seg.id) {
      applyPreviewSeek() // same clip already loaded → instant scrub
      return
    }
    // New segment: tear down HD, load this segment's cached SUB clip for preview.
    previewSegId.current = seg.id
    stopStream.current?.()
    stopStream.current = null
    streamOpen.current = false
    if (watchdog.current) clearTimeout(watchdog.current)
    setLoading(true)
    setError(null)
    // Prefetch neighbours so crossing into them is instant too.
    const i = segments.indexOf(seg)
    prefetch(
      camera.slug,
      [segments[i - 1]?.id, segments[i + 1]?.id].filter(Boolean) as string[],
      'sub',
    )
    ensureClip(camera.slug, seg.id, 'sub')
      .then(() => {
        if (previewSegId.current !== seg.id) return // user moved to another segment
        v.src = clipUrl(camera.slug, seg.id, 'sub')
        const onready = () => {
          v.removeEventListener('loadeddata', onready)
          setLoading(false)
          applyPreviewSeek()
        }
        v.addEventListener('loadeddata', onready)
        v.load()
      })
      .catch(() => {
        if (previewSegId.current === seg.id) setError('preview unavailable')
      })
  }

  const onScrubEnd = (t: number) => {
    dragging.current = false
    previewSegId.current = null
    seekTo(t, quality) // settle → HD at the exact moment
  }

  const onTimeUpdate = () => {
    setLoading(false)
    attempt.current = 0
    if (watchdog.current) {
      clearTimeout(watchdog.current)
      watchdog.current = null
    }
    if (dragging.current) return
    const video = videoRef.current
    if (video) setCurrentTimeMs(anchorWall.current + (video.currentTime - anchorCt.current) * 1000)
  }

  const setQ = (q: 'main' | 'sub') => {
    setQuality(q)
    openStream(currentTimeMs, q) // quality change needs a fresh stream (codec)
  }

  const shiftDay = (delta: number) => {
    const d = new Date(dStart + delta * 86400000)
    setDate(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`)
    streamOpen.current = false // new day → reopen on next auto-open
  }

  return (
    <div className="playback">
      <div className="playback-toolbar">
        <button onClick={() => shiftDay(-1)}>‹ Prev</button>
        <input type="date" value={date} onChange={(e) => { streamOpen.current = false; setDate(e.target.value) }} />
        <button onClick={() => shiftDay(1)}>Next ›</button>
        <span className="playback-camname">{camera.name}</span>
        <button className={quality === 'main' ? 'hd-toggle on' : 'hd-toggle'} onClick={() => setQ('main')}>
          {QUALITY_LABEL}
        </button>
        <button className={quality === 'sub' ? 'hd-toggle on' : 'hd-toggle'} onClick={() => setQ('sub')}>
          SD
        </button>
        {ready && <span className="playback-count">{segments.length} segments</span>}
      </div>

      <div className="playback-video">
        <video ref={videoRef} controls playsInline onTimeUpdate={onTimeUpdate}
               onPlaying={() => setLoading(false)} />
        {loading && <div className="buffering">loading…</div>}
        {error && <div className="buffering error">{error}</div>}
        {ready && segments.length === 0 && <div className="buffering">No recordings this day</div>}
      </div>

      <Timeline
        dayStart={dStart}
        segments={segments}
        currentTimeMs={currentTimeMs}
        onScrub={onScrub}
        onScrubEnd={onScrubEnd}
      />
    </div>
  )
}

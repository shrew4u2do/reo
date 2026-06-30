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
  const [segments, setSegments] = useState<Segment[]>([]) // sub list (timeline + scrub preview)
  const [mainSegments, setMainSegments] = useState<Segment[]>([]) // main list (HD replay anchor)
  const [currentTimeMs, setCurrentTimeMs] = useState(dayStartMs(date))
  const [quality, setQuality] = useState<'main' | 'sub'>('main')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [scrubNote, setScrubNote] = useState<string | null>(null) // shown while scrubbing over a blank (uncached/no-recording) region
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
  const previewSegId = useRef<string | null>(null) // segment we're trying to preview
  const loadedSegId = useRef<string | null>(null) // segment whose clip is actually loaded
  const scrubTargetMs = useRef(0)

  const dStart = dayStartMs(date)

  const segAt = useCallback(
    (t: number): Segment | undefined => segments.find((s) => t >= s.start && t <= s.end),
    [segments],
  )

  // Load the day's recording availability: sub (timeline + scrub preview) and main
  // (the HD replay's segment boundaries, used to anchor the playhead clock).
  useEffect(() => {
    let cancelled = false
    setReady(false)
    setError(null)
    setMainSegments([])
    fetchRecordings(camera.slug, date, 'main')
      .then((d) => !cancelled && setMainSegments(d.segments))
      .catch(() => {}) // non-fatal: anchor falls back to the sub boundary
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
      // The replay begins at the covering 5-min SEGMENT START, not at t, so anchor
      // the playhead clock there — otherwise the timeline runs ahead of the video's
      // real (burned-in) time by however far into the segment you clicked. Anchor on
      // the stream actually being played: main/sub segment boundaries differ by ~30s.
      const segs = q === 'main' ? mainSegments : segments
      const seg = segs.find((s) => t >= s.start && t <= s.end)
      const startMs = seg ? seg.start : t
      anchorWall.current = startMs
      anchorCt.current = 0
      setCurrentTimeMs(startMs)
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
    [camera.slug, segments, mainSegments],
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

  /** Nudge the playhead by ±seconds. Within the current segment this is an instant
   *  in-buffer seek; crossing the segment boundary reopens the stream. Forward is
   *  capped at what's streamed so far (the replay arrives at realtime pace). */
  const nudge = (deltaSec: number) => {
    const v = videoRef.current
    if (!v || !streamOpen.current) return
    const targetMs = currentTimeMs + deltaSec * 1000
    const segs = quality === 'main' ? mainSegments : segments
    const seg = segs.find((s) => currentTimeMs >= s.start && currentTimeMs <= s.end)
    if (!seg || targetMs < seg.start || targetMs > seg.end) {
      seekTo(targetMs, quality) // crosses the segment → reopen at that segment's start
      return
    }
    let ct = (targetMs - anchorWall.current) / 1000 + anchorCt.current
    const buf = v.buffered
    const liveEdge = buf.length ? buf.end(buf.length - 1) : v.currentTime
    ct = Math.max(0, Math.min(ct, liveEdge))
    try {
      v.currentTime = ct
      setCurrentTimeMs(anchorWall.current + (ct - anchorCt.current) * 1000)
    } catch {
      /* not seekable yet */
    }
  }

  // Auto-open at the latest moment once recordings load. Wait for the main list too
  // when playing HD, so the playhead anchors on the right (main) segment boundary.
  useEffect(() => {
    const haveAnchor = quality !== 'main' || mainSegments.length > 0
    if (ready && segments.length > 0 && haveAnchor && !streamOpen.current) {
      openStream(segments[segments.length - 1].start, quality)
    }
  }, [ready, segments, mainSegments, quality, openStream])

  useEffect(
    () => () => {
      stopStream.current?.()
      if (watchdog.current) clearTimeout(watchdog.current)
    },
    [],
  )

  /** Drop whatever the video is showing (so we never display a stale clip). */
  const blankPreview = () => {
    const v = videoRef.current
    if (!v) return
    v.removeAttribute('src')
    v.srcObject = null
    loadedSegId.current = null
    try {
      v.load()
    } catch {
      /* ignore */
    }
  }

  /** Seek the loaded preview clip to wherever the cursor is now (instant). Only
   *  acts when the clip currently loaded matches the segment under the cursor. */
  const applyPreviewSeek = useCallback(() => {
    const v = videoRef.current
    if (!v) return
    const seg = segAt(scrubTargetMs.current)
    if (!seg || seg.id !== loadedSegId.current) return
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
    if (!v) return
    if (seg && seg.id === loadedSegId.current) {
      applyPreviewSeek() // this segment's clip is loaded → instant scrub
      return
    }
    if (seg && seg.id === previewSegId.current) return // already handling this segment
    // Entered a new segment (or a gap): drop the old clip immediately so we never
    // loop the last cached section over an uncached one.
    previewSegId.current = seg ? seg.id : null
    stopStream.current?.()
    stopStream.current = null
    streamOpen.current = false
    if (watchdog.current) clearTimeout(watchdog.current)
    setError(null)
    blankPreview()
    if (!seg || !seg.cached) {
      // No recording, or not mirrored locally → no instant preview. Stay blank;
      // the full-res replay loads when you settle (onScrubEnd). Don't download
      // here — scrubbing across uncached regions shouldn't hammer the Hub.
      setLoading(false)
      setScrubNote(seg ? 'not cached — release to load' : 'no recording here')
      return
    }
    setScrubNote(null)
    setLoading(true)
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
        v.srcObject = null // iOS: a leftover (Managed)MediaSource would shadow .src
        v.src = clipUrl(camera.slug, seg.id, 'sub')
        const onready = () => {
          v.removeEventListener('loadeddata', onready)
          loadedSegId.current = seg.id
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
    loadedSegId.current = null
    setScrubNote(null)
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
        {loading && !scrubNote && <div className="buffering">loading…</div>}
        {scrubNote && <div className="buffering">{scrubNote}</div>}
        {error && <div className="buffering error">{error}</div>}
        {ready && segments.length === 0 && <div className="buffering">No recordings this day</div>}
      </div>

      <div className="nudge-row">
        <button className="nudge" onClick={() => nudge(-15)} disabled={!ready} title="Back 15 seconds">« 15s</button>
        <button className="nudge" onClick={() => nudge(15)} disabled={!ready} title="Forward 15 seconds">15s »</button>
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

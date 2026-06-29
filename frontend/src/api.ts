// API client + URL helpers for talking to the backend and go2rtc.

export interface Camera {
  slug: string
  name: string
  channel: number
  online: boolean
  streams: { hd: string; sd: string }
}

export async function fetchCameras(): Promise<Camera[]> {
  const r = await fetch('/api/cameras')
  if (!r.ok) throw new Error(`cameras: ${r.status}`)
  return r.json()
}

let _go2rtcPort = 1984
export async function fetchInfo(): Promise<void> {
  try {
    const r = await fetch('/api/info')
    if (r.ok) {
      const j = await r.json()
      if (j.go2rtcPort) _go2rtcPort = j.go2rtcPort
    }
  } catch {
    /* keep default */
  }
}

// go2rtc runs on the same host the browser used to reach the app (works for
// localhost on PC and the LAN IP on a phone), on its own port.
export function go2rtcWsUrl(stream: string): string {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.hostname}:${_go2rtcPort}/api/ws?src=${encodeURIComponent(stream)}`
}

// ---- Recordings / playback ----

export interface Segment {
  id: string
  start: number // epoch ms
  end: number // epoch ms
  durationSec: number
  clock: string
  cached: boolean
  bytes: number
}

export interface RecordingsDay {
  date: string
  stream: string
  availableDates: string[]
  segments: Segment[]
}

export async function fetchRecordings(
  camera: string,
  date: string,
  stream = 'sub',
): Promise<RecordingsDay> {
  const r = await fetch(
    `/api/recordings?camera=${encodeURIComponent(camera)}&date=${date}&stream=${stream}`,
  )
  if (!r.ok) throw new Error(`recordings: ${r.status}`)
  return r.json()
}

export function clipUrl(camera: string, id: string, stream = 'sub'): string {
  return `/api/clip?camera=${encodeURIComponent(camera)}&id=${id}&stream=${stream}`
}

/**
 * Live Baichuan replay URL — streams a fragmented MP4 from epoch-ms `t`, like
 * the Reolink app (instant seek, realtime, quality-switchable). stream: main|sub.
 * Native HEVC for capable browsers, else server transcodes to H.264.
 */
function backendBase(): string {
  // Hit the backend directly (:8000) — the dev proxy buffers streaming responses,
  // which breaks live playback. Same-origin in production (frontend served on :8000).
  return `${location.protocol}//${location.hostname}:8000`
}

export function bcplayUrl(camera: string, t: number, stream: 'main' | 'sub', hevc: boolean): string {
  return `${backendBase()}/api/bcplay?camera=${encodeURIComponent(camera)}&t=${Math.floor(t)}&stream=${stream}&hevc=${hevc ? 1 : 0}`
}

/**
 * Reposition the open playback stream in place (epoch ms). Returns true if it
 * was handled in place (instant); false means the caller should reopen the
 * stream (the target crossed a 5-minute file boundary).
 */
export async function seekStream(t: number): Promise<boolean> {
  try {
    const r = await fetch(`${backendBase()}/api/bcplay/seek?t=${Math.floor(t)}`)
    const j = await r.json()
    return !!j.ok
  } catch {
    return false
  }
}

// Does this browser/device decode H.265? If so we can serve native 4K (just
// remuxed) instead of transcoding to 1080p H.264. (Chrome 107+ on a device with
// a hardware HEVC decoder, Safari, etc.)
let _hevc: boolean | null = null
export function supportsHevc(): boolean {
  if (_hevc === null) {
    const v = document.createElement('video')
    const probe = (c: string) => v.canPlayType(c) !== ''
    _hevc =
      probe('video/mp4; codecs="hvc1.1.6.L93.B0"') ||
      probe('video/mp4; codecs="hev1.1.6.L93.B0"')
  }
  return _hevc
}

/** Ask the backend to start caching a list of segments (best-effort). */
export function prefetch(camera: string, ids: string[], stream = 'sub'): void {
  if (ids.length === 0) return
  fetch(
    `/api/prefetch?camera=${encodeURIComponent(camera)}&stream=${stream}&ids=${ids.join(',')}`,
  ).catch(() => {})
}

/**
 * Wait until a segment is cached and seekable. Polls the clip endpoint with a
 * tiny Range request (cheap) until it returns 200/206 instead of 202.
 * Resolves to the playable clip URL.
 */
export async function ensureClip(
  camera: string,
  id: string,
  stream = 'sub',
  signal?: AbortSignal,
  onProgress?: (bytes: number) => void,
): Promise<string> {
  const url = clipUrl(camera, id, stream)
  // Client-side throughput watchdog: give up if the Hub delivers slower than a
  // floor over a window. This separates a healthy download (~150 KB/s) from the
  // Hub's drip/stall failure mode (<10 KB/s) — which a "any-progress" check can't.
  const WINDOW_MS = 35_000
  // ~43 KB/s floor over the window. A healthy Hub download (~150 KB/s) clears it
  // easily; a degraded Hub dribbling tens of KB/s (which would take 20+ min for a
  // 4K segment, i.e. unusable) trips it and we report "too slow".
  const MIN_GROWTH = 1_500 * 1024
  let winStart = Date.now()
  let winBytes = -1
  for (;;) {
    if (signal?.aborted) throw new DOMException('aborted', 'AbortError')
    const r = await fetch(url, { headers: { Range: 'bytes=0-1' }, signal })
    if (r.status === 200 || r.status === 206) return url
    if (r.status !== 202) {
      let detail = `clip: ${r.status}`
      try {
        const j = await r.json()
        if (j.detail) detail = j.detail
      } catch {
        /* ignore */
      }
      throw new Error(detail)
    }
    let bytes = -1
    try {
      const j = await r.json()
      if (typeof j.bytes === 'number') bytes = j.bytes
    } catch {
      /* ignore */
    }
    if (bytes >= 0) {
      onProgress?.(bytes)
      if (winBytes < 0) winBytes = bytes
      if (Date.now() - winStart >= WINDOW_MS) {
        if (bytes - winBytes < MIN_GROWTH) {
          throw new Error('Hub is busy / too slow — try again in a bit')
        }
        winStart = Date.now()
        winBytes = bytes
      }
    }
    await new Promise((res) => setTimeout(res, 1500))
  }
}

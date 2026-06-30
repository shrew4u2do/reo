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
  availableDates: string[]
  segments: Segment[]
}

export async function fetchRecordings(camera: string, date: string): Promise<RecordingsDay> {
  const r = await fetch(`/api/recordings?camera=${encodeURIComponent(camera)}&date=${date}`)
  if (!r.ok) throw new Error(`recordings: ${r.status}`)
  return r.json()
}

// One seekable MP4 per ~10-min NAS segment. hevc=true serves the native 4K HEVC
// (stream-copied); hevc=false serves a 1080p H.264 transcode for browsers that
// can't decode HEVC. FileResponse forwards HTTP Range, so a plain <video> seeks it.
export function clipUrl(camera: string, id: string, hevc = true): string {
  return `/api/clip?camera=${encodeURIComponent(camera)}&id=${id}&hevc=${hevc ? 1 : 0}`
}

// The per-minute JPEG snapshot nearest wall-clock t — used for scrub preview.
export function thumbnailUrl(camera: string, t: number): string {
  return `/api/thumbnail?camera=${encodeURIComponent(camera)}&t=${Math.floor(t)}`
}

// Can this browser decode H.265 in a plain <video>? If so we serve the native 4K
// (stream-copied); otherwise the backend transcodes to 1080p H.264. The Hub HEVC
// is Main/Main10 L186 — probe that (and a generic level as a fallback).
let _hevc: boolean | null = null
export function supportsHevc(): boolean {
  if (_hevc === null) {
    const v = document.createElement('video')
    const probe = (c: string) => v.canPlayType(c) !== ''
    _hevc =
      probe('video/mp4; codecs="hvc1.1.6.L186.B0"') ||
      probe('video/mp4; codecs="hvc1.1.6.L93.B0"') ||
      probe('video/mp4; codecs="hev1.1.6.L93.B0"')
  }
  return _hevc
}

/** Ask the backend to pre-remux a list of segments (best-effort). */
export function prefetch(camera: string, ids: string[], hevc = true): void {
  if (ids.length === 0) return
  fetch(
    `/api/prefetch?camera=${encodeURIComponent(camera)}&hevc=${hevc ? 1 : 0}&ids=${ids.join(',')}`,
  ).catch(() => {})
}

/**
 * Wait until a segment has been remuxed and is seekable. Polls the clip endpoint
 * with a tiny Range request (cheap) until it returns 200/206 instead of 202.
 * Resolves to the playable clip URL.
 *
 * Remuxing is a local stream-copy (seconds); an HEVC→H.264 transcode of a 4K
 * segment is slower. The watchdog only trips if no progress is made for a long
 * stretch (a stuck/failed ffmpeg), so normal transcodes are never cut off.
 */
export async function ensureClip(
  camera: string,
  id: string,
  hevc = true,
  signal?: AbortSignal,
  onProgress?: (bytes: number) => void,
): Promise<string> {
  const url = clipUrl(camera, id, hevc)
  const STALL_MS = 90_000 // no output growth for this long ⇒ treat ffmpeg as stuck
  let lastGrowth = Date.now()
  let lastBytes = -1
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
      if (bytes > lastBytes) {
        lastBytes = bytes
        lastGrowth = Date.now()
      } else if (Date.now() - lastGrowth >= STALL_MS) {
        throw new Error('clip is taking too long — try again in a bit')
      }
    }
    await new Promise((res) => setTimeout(res, 1000))
  }
}

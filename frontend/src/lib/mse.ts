// Minimal MSE player for a live fragmented-MP4 stream (the Baichuan replay).
// A plain <video src> won't render a never-ending progressive fMP4; feeding the
// bytes into a MediaSource SourceBuffer does.

import { supportsHevc } from '../api'

// The Hub reports HEVC Main level 6.2 (L186); list it first, with fallbacks.
const HEVC_CODECS = [
  'video/mp4; codecs="hvc1.1.6.L186.B0"',
  'video/mp4; codecs="hvc1.1.6.L180.B0"',
  'video/mp4; codecs="hvc1.1.6.L153.B0"',
  'video/mp4; codecs="hvc1.1.6.L150.B0"',
]
const H264_CODECS = ['video/mp4; codecs="avc1.640028"', 'video/mp4; codecs="avc1.4d4028"', 'video/mp4; codecs="avc1.42e01e"']

// iPhone Safari has NO `window.MediaSource` — it exposes `ManagedMediaSource`
// (iOS 17.1+) instead. Referencing a missing `MediaSource` at module load would
// throw and black out the whole app, so resolve whichever exists (or neither).
type MSCtor = { new (): MediaSource; isTypeSupported(type: string): boolean }
const g = globalThis as unknown as { MediaSource?: MSCtor; ManagedMediaSource?: MSCtor }
const MS: MSCtor | undefined = g.MediaSource || g.ManagedMediaSource
const IS_MANAGED = !!g.ManagedMediaSource && MS === g.ManagedMediaSource

/** True if the browser's MSE can play this stream's HEVC level. */
export function mseSupportsHevc(): boolean {
  if (!MS) return false
  try {
    return supportsHevc() && HEVC_CODECS.some((c) => MS.isTypeSupported(c))
  } catch {
    return false
  }
}

function pickMime(hevc: boolean): string {
  const list = hevc ? HEVC_CODECS : H264_CODECS
  return (MS && list.find((c) => MS.isTypeSupported(c))) || list[0]
}

/** Start streaming `url` (fMP4) into `video` via MSE. Returns a stop function. */
export function startMsePlayback(video: HTMLVideoElement, url: string, hevc: boolean): () => void {
  const abort = new AbortController()
  let stopped = false
  if (!MS) return () => {} // no MSE/ManagedMediaSource (older iOS) — can't play here
  const ms = new MS()
  if (IS_MANAGED) {
    // ManagedMediaSource must be attached via srcObject, with remote playback off.
    // Drop any leftover preview .src, which would otherwise shadow srcObject.
    video.removeAttribute('src')
    ;(video as unknown as { disableRemotePlayback: boolean }).disableRemotePlayback = true
    ;(video as unknown as { srcObject: MediaSource | null }).srcObject = ms
  } else {
    video.srcObject = null
    video.src = URL.createObjectURL(ms)
  }

  ms.addEventListener('sourceopen', () => {
    let sb: SourceBuffer
    try {
      sb = ms.addSourceBuffer(pickMime(hevc))
      sb.mode = 'sequence'
    } catch {
      return
    }
    const queue: BufferSource[] = []
    let ended = false

    const pump = () => {
      if (stopped || sb.updating || queue.length === 0) return
      try {
        sb.appendBuffer(queue.shift()!)
      } catch (e) {
        // QuotaExceeded: drop the oldest buffered data and retry.
        if ((e as DOMException).name === 'QuotaExceededError' && sb.buffered.length) {
          try {
            sb.remove(sb.buffered.start(0), Math.max(sb.buffered.start(0), video.currentTime - 10))
          } catch {
            /* ignore */
          }
        }
      }
    }
    sb.addEventListener('updateend', () => {
      pump()
      if (ended && !sb.updating && queue.length === 0 && ms.readyState === 'open') {
        try {
          ms.endOfStream()
        } catch {
          /* ignore */
        }
      }
    })

    fetch(url, { signal: abort.signal })
      .then((res) => {
        const reader = res.body!.getReader()
        const read = (): Promise<void> =>
          reader.read().then(({ done, value }) => {
            if (stopped) return
            if (done) {
              ended = true
              pump()
              return
            }
            queue.push(value as BufferSource)
            pump()
            return read()
          })
        return read()
      })
      .catch(() => {
        /* aborted / network */
      })
  })

  return () => {
    stopped = true
    abort.abort()
    try {
      if (ms.readyState === 'open') ms.endOfStream()
    } catch {
      /* ignore */
    }
  }
}

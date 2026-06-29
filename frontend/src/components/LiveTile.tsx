import { useEffect, useRef } from 'react'
import { VideoRTC } from '../lib/video-rtc.js'
import { go2rtcWsUrl } from '../api'

// Register the go2rtc web component once.
if (!customElements.get('video-rtc')) {
  customElements.define('video-rtc', VideoRTC)
}

declare module 'react' {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace JSX {
    interface IntrinsicElements {
      'video-rtc': React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>
    }
  }
}

interface Props {
  stream: string
  /** webrtc has lowest latency; mse is the broad fallback. */
  mode?: string
  muted?: boolean
}

/** A single low-latency live video element backed by go2rtc. */
export default function LiveTile({ stream, mode = 'webrtc,mse,hls', muted = true }: Props) {
  const ref = useRef<HTMLElement & { src: string; mode: string; background: boolean }>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.mode = mode
    el.src = go2rtcWsUrl(stream)
  }, [stream, mode])

  return (
    <video-rtc
      ref={ref as never}
      className="live-video"
      // muted/playsinline so mobile autoplays without user gesture
      {...(muted ? { muted: true } : {})}
    />
  )
}

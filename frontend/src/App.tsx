import { useEffect, useState } from 'react'
import { fetchCameras, fetchInfo, type Camera } from './api'
import LiveTile from './components/LiveTile'
import PlaybackView from './components/PlaybackView'
import './App.css'

export default function App() {
  const [cameras, setCameras] = useState<Camera[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<Camera | null>(null)

  useEffect(() => {
    ;(async () => {
      try {
        await fetchInfo()
        setCameras(await fetchCameras())
      } catch (e) {
        setError(String(e))
      } finally {
        setLoading(false)
      }
    })()
  }, [])

  return (
    <div className="app">
      <header className="topbar">
        <h1 onClick={() => setSelected(null)} style={{ cursor: 'pointer' }}>
          reo
        </h1>
        <span className="subtitle">{selected ? 'playback' : 'live'}</span>
        {selected && (
          <button className="back" onClick={() => setSelected(null)}>
            ← all cameras
          </button>
        )}
      </header>

      {loading && <p className="status">Connecting to Hub…</p>}
      {error && <p className="status error">Error: {error}</p>}

      {!loading && !error && selected && <PlaybackView camera={selected} />}

      {!loading && !error && !selected && (
        <div className="grid">
          {cameras.map((cam) => (
            <div className="tile" key={cam.slug}>
              <div className="tile-video">
                <LiveTile stream={cam.streams.sd} />
                {!cam.online && <div className="offline">offline</div>}
              </div>
              <div className="tile-label">
                <span>{cam.name}</span>
                <button className="tile-playback" onClick={() => setSelected(cam)}>
                  timeline ›
                </button>
              </div>
            </div>
          ))}
          {cameras.length === 0 && <p className="status">No cameras found.</p>}
        </div>
      )}
    </div>
  )
}

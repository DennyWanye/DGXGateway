// From FreeCut 4d62e8082c5eb387a96275bcbd323d28f6e41a62. MIT; see LICENSE.
/** Ruler clock label: HH:MM:SS. */
export function formatMiniTimelineClock(frame: number, fps: number): string {
  const safeFps = fps > 0 ? fps : 30
  const totalSeconds = Math.max(0, Math.floor(frame / safeFps))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return [hours, minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':')
}

/** Film-tile timecode label: HH:MM:SS:FF. */
export function formatMiniTimelineTimecode(frame: number, fps: number): string {
  const safeFps = Math.max(1, Math.round(fps > 0 ? fps : 30))
  const clampedFrame = Math.max(0, Math.round(frame))
  const totalSeconds = Math.floor(clampedFrame / safeFps)
  const frames = clampedFrame % safeFps
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return [hours, minutes, seconds, frames].map((part) => String(part).padStart(2, '0')).join(':')
}


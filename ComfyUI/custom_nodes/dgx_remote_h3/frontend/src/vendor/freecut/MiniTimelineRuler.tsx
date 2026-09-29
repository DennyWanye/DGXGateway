// Adapted from FreeCut 4d62e8082c5eb387a96275bcbd323d28f6e41a62. MIT; see LICENSE.
import { memo } from 'react'
const MINI_TIMELINE_RULER_HEIGHT = 24
import { formatMiniTimelineClock } from './timecode'

const RULER_RATIOS = [0, 0.25, 0.5, 0.75, 1] as const

/** Quarter-mark time ruler aligned to the track content (inset by labelWidth). */
export const MiniTimelineRuler = memo(function MiniTimelineRuler({
  labelWidth,
  maxFrame,
  fps,
  height = MINI_TIMELINE_RULER_HEIGHT,
  formatTime = formatMiniTimelineClock,
}: {
  labelWidth: number
  maxFrame: number
  fps: number
  height?: number
  formatTime?: (frame: number, fps: number) => string
}) {
  return (
    <div className="relative border-b border-black/40" style={{ height }}>
      <div className="absolute inset-y-0 right-0" style={{ left: labelWidth }}>
        {RULER_RATIOS.map((ratio) => (
          <div
            key={ratio}
            className="absolute top-0 h-full border-l border-zinc-500/45 pl-1 pt-0.5 text-[10px] text-zinc-500"
            style={{ left: `${ratio * 100}%` }}
          >
            {formatTime(Math.round(ratio * maxFrame), fps)}
          </div>
        ))}
      </div>
    </div>
  )
})

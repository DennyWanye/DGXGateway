import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { mediaUrl } from '@/lib/api';
import type { AudioClip, SubtitleSegment } from './PostproductionPanel';
import { validateAudioClips } from './PostproductionPanel';
import { PreviewAudioMixer } from '@/lib/preview-audio';
type Clip = { clipId: string; takeId: string; inFrame: number | string; frames: number | string; keepSourceAudio: boolean; fit: string; crop?: {x: number; y: number} };
export function timelinePosition(clips: Clip[], frame: number) {
  let from = 0;
  for (let index = 0; index < clips.length; index++) { const length = Number(clips[index].frames); if (frame < from + length) return { index, from, sourceFrame: Number(clips[index].inFrame) + Math.max(0, frame - from) }; from += length; }
  return null;
}
export function audioGain(clip: AudioClip, frame: number) {
  const local = frame - Number(clip.startFrame), length = Number(clip.frames);
  if (local < 0 || local >= length) return 0;
  const fadeIn = Number(clip.fadeInFrames), fadeOut = Number(clip.fadeOutFrames);
  return Math.pow(10, Number(clip.gainDb) / 20) * Math.min(1, fadeIn ? local / fadeIn : 1, fadeOut ? (length - local) / fadeOut : 1);
}
export function TimelinePreview({ pid, clips, takes, assets = [], audioClips, subtitles, fps, width, height, active }: { pid: string; clips: Clip[]; takes: any[]; assets?: any[]; audioClips: AudioClip[]; subtitles: SubtitleSegment[]; fps: number; width: number; height: number; active: boolean }) {
  const [frame, setFrame] = useState(0), [playing, setPlaying] = useState(false), [error, setError] = useState('');
  const video = useRef<HTMLVideoElement>(null), audios = useRef(new Map<string, HTMLAudioElement>());
  const mixer = useRef(new PreviewAudioMixer());
  const play = useRef(false), clock = useRef(0), seek = useRef(0);
  const total = clips.reduce((n,c) => n + Math.max(0, Number(c.frames) || 0), 0);
  let audioProblem = '';
  try { validateAudioClips(audioClips, total, assets, fps); } catch (e) { audioProblem = (e as Error).message; }
  const position = timelinePosition(clips, Math.min(frame, Math.max(0, total - 1))), clip = position && clips[position.index];
  const take = clip && takes.find(t => t.id === clip.takeId);
  const stop = () => { play.current = false; setPlaying(false); video.current?.pause(); for (const audio of audios.current.values()) audio.pause(); };
  const jump = (next: number) => { next = Math.max(0, Math.min(total - 1, next)); const p = timelinePosition(clips, next); seek.current = next; clock.current = next; setFrame(next); if (p && position?.index === p.index && video.current?.readyState) video.current.currentTime = p.sourceFrame / fps; };
  useEffect(() => { stop(); jump(0); setError(''); }, [clips, fps, active]);
  useEffect(() => { if (audioProblem) stop(); }, [audioProblem]);
  useEffect(() => { const exclusive = (event: Event) => { if (event.target instanceof HTMLVideoElement && event.target !== video.current) stop(); }; document.addEventListener('play', exclusive, true); return () => document.removeEventListener('play', exclusive, true); }, []);
  useEffect(() => { const v = video.current; return () => { play.current = false; if (v) { v.pause(); v.removeAttribute('src'); v.load(); } for (const a of audios.current.values()) { a.pause(); a.removeAttribute('src'); a.load(); } void mixer.current.close(); }; }, [active]);
  useEffect(() => {
    if (!active || !position || !clip || audioProblem) return;
    let id = 0;
    const update = () => {
      const v = video.current;
      if (v && play.current && v.readyState >= 2) {
        const next = position.from + (v.currentTime * fps - Number(clip.inFrame));
        if (v.ended || next >= position.from + Number(clip.frames) - .15) {
          const end = position.from + Number(clip.frames);
          if (end >= total) { stop(); clock.current = total - 1; v.currentTime = (Number(clip.inFrame) + Number(clip.frames) - 1) / fps; setFrame(total - 1); }
          else { clock.current = end; seek.current = end; setFrame(end); }
        } else { clock.current = Math.max(position.from, next); setFrame(Math.floor(clock.current)); }
      }
      for (const item of audioClips) {
        const a = audios.current.get(item.audioClipId); if (!a) continue;
        const gain = audioGain(item, clock.current), desired = (Number(item.sourceInFrame) + clock.current - Number(item.startFrame)) / fps;
        mixer.current.setGain(a, gain);
        if (play.current && gain > 0 && desired >= 0) { if (Math.abs(a.currentTime - desired) > .12) a.currentTime = desired; mixer.current.play(a, () => { if (play.current && audios.current.get(item.audioClipId) === a) { stop(); setError('音频无法播放，请检查素材后重试。'); } }); }
        else a.pause();
      }
      mixer.current.retain(audios.current.values());
      id = requestAnimationFrame(update);
    };
    id = requestAnimationFrame(update); return () => cancelAnimationFrame(id);
  }, [active, position?.index, clip, fps, total, audioClips, audioProblem]);
  const toggle = async () => { if (playing) return stop(); setError(''); if (frame >= total - 1) jump(0); play.current = true; setPlaying(true); try { await Promise.all([audioClips.length ? mixer.current.resume() : Promise.resolve(), video.current?.play()]); } catch { stop(); setError('当前音视频无法播放，请检查素材及浏览器声音权限。'); } };
  const valid = !audioProblem && clips.every(c => Number(c.frames) > 0 && Number(c.inFrame) >= 0);
  return <section className="space-y-3 rounded-xl border bg-card p-4" aria-label="整条时间线预览">
    <div className="flex justify-between"><h3 className="font-semibold">整片预览</h3><span className="text-xs text-muted-foreground">{(frame / fps).toFixed(2)} / {(total / fps).toFixed(2)} 秒</span></div>
    <div className="relative mx-auto max-w-3xl overflow-hidden rounded-lg bg-black" style={{aspectRatio: `${width}/${height}`}}>
      {active && take?.assetId && <video key={clip?.clipId} ref={video} src={mediaUrl(pid, take.assetId)} preload="metadata" muted={!clip?.keepSourceAudio} className="h-full w-full" style={{objectFit: clip?.fit === 'cover' ? 'cover' : 'contain', objectPosition: `${(clip?.crop?.x ?? .5)*100}% ${(clip?.crop?.y ?? .5)*100}%`}} aria-label="整片连续预览播放器" onLoadedMetadata={e => { const p = timelinePosition(clips, seek.current); if (p) e.currentTarget.currentTime = p.sourceFrame / fps; if (play.current) void e.currentTarget.play().catch(() => { stop(); setError('请点击继续播放。'); }); }} onError={() => { stop(); setError('当前素材无法解码，请核查或下载原视频。'); }} />}
      <div className="pointer-events-none absolute inset-x-8 bottom-[6%] text-center text-lg font-semibold text-white [text-shadow:1px_1px_3px_black]">{subtitles.filter(s => Number(s.startFrame) <= frame && frame < Number(s.endFrame)).map((s,i) => <p key={i}>{s.text}</p>)}</div>
    </div>
    <input type="range" aria-label="整片播放位置" min={0} max={Math.max(0,total-1)} step={1} value={frame} disabled={!valid || !total} onChange={e => jump(Number(e.target.value))} className="w-full accent-primary" />
    <div className="flex gap-2"><Button size="sm" disabled={!active || !take || !valid} onClick={() => void toggle()}>{playing ? '暂停整片' : '播放整片'}</Button><Button size="sm" variant="outline" onClick={() => { stop(); jump(0); }}>回到开头</Button></div>
    {(error || audioProblem) && <p role="alert" className="text-sm text-destructive">{audioProblem || error}</p>}
    <p className="text-xs text-muted-foreground">预览按当前草稿的入点、长度、顺序和原声开关播放，独立音轨保留设定音量与淡入淡出。切换素材可能有短暂加载；最终逐帧拼接与字幕样式以导出为准。</p>
    {active && audioClips.map(a => <audio key={a.audioClipId} ref={el => { if(el) audios.current.set(a.audioClipId, el); else audios.current.delete(a.audioClipId); }} src={mediaUrl(pid,a.assetId)} preload="none" />)}
    <div className="space-y-2" aria-label="音频与字幕轨道">
      {['voiceover','ambience','bgm','subtitle'].map(track => <div key={track} className="grid grid-cols-[76px_1fr] items-center gap-2 text-xs"><span className="text-muted-foreground">{({voiceover:'对白 / 旁白',ambience:'环境声',bgm:'背景音乐',subtitle:'字幕'} as any)[track]}</span><div className="relative h-7 overflow-hidden rounded bg-muted/40">{(track==='subtitle' ? subtitles.map((s,i)=>({id:i,start:Number(s.startFrame),length:Number(s.endFrame)-Number(s.startFrame),label:s.text})) : audioClips.filter(a=>a.track===track).map(a=>({id:a.audioClipId,start:Number(a.startFrame),length:Number(a.frames),label:({voiceover:'对白 / 旁白',ambience:'环境声',bgm:'背景音乐'} as any)[track]}))).map(a=><button key={a.id} type="button" onClick={()=>{stop();jump(a.start);}} title={`${a.label} ${(a.start/fps).toFixed(2)}秒`} className="absolute inset-y-0 truncate rounded border border-primary/30 bg-primary/20 px-1 text-left" style={{left:`${100*a.start/(total||1)}%`,width:`${100*a.length/(total||1)}%`}}>{a.label}</button>)}</div></div>)}
    </div>
  </section>;
}

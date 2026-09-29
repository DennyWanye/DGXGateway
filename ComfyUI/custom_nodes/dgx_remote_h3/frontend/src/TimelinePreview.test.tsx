// @vitest-environment jsdom
import { describe,it,expect } from 'vitest';
import { timelinePosition,audioGain } from './TimelinePreview';
import { buildVideoBatch } from './VideoPanel';
const lengths=[96,96,72,96,96,96,72,96];
const clips=lengths.map((frames,i)=>({clipId:`c${i}`,takeId:`t${i}`,inFrame:i*3,frames,keepSourceAudio:i!==3,fit:'contain'}));
describe('整片时间与候选边界',()=>{
 it('八片720帧，各切点只应用一次源入点',()=>{expect(lengths.reduce((n,x)=>n+x,0)).toBe(720);let cursor=0;clips.forEach((c,index)=>{expect(timelinePosition(clips,cursor)).toEqual({index,from:cursor,sourceFrame:c.inFrame});expect(timelinePosition(clips,cursor+c.frames-1)?.sourceFrame).toBe(c.inFrame+c.frames-1);cursor+=c.frames;});expect(timelinePosition(clips,720)).toBeNull();});
 it('分割后顺序预览保持源帧连续',()=>{const split=[{...clips[0],frames:24},{...clips[0],clipId:'right',inFrame:24,frames:72},...clips.slice(1)];expect(timelinePosition(split,24)?.sourceFrame).toBe(24);expect(timelinePosition(split,95)?.sourceFrame).toBe(95);expect(timelinePosition(split,96)?.sourceFrame).toBe(3);});
 it('独立音轨入点与淡入淡出不越界',()=>{const a={audioClipId:'a',assetId:'x',track:'bgm',startFrame:24,sourceInFrame:100,frames:96,gainDb:0,fadeInFrames:24,fadeOutFrames:24};expect(audioGain(a,23)).toBe(0);expect(audioGain(a,36)).toBe(.5);expect(audioGain(a,60)).toBe(1);expect(audioGain(a,108)).toBe(.5);expect(audioGain(a,120)).toBe(0);});
 it('视频候选总数按展开计算，默认仍只生成一个',()=>{const d={shotId:'s',shotRevisionId:'r',audioAssetId:'a',imageAssetId:'i',approved:true,prompt:'punch',seed:42};expect(buildVideoBatch([d]).shots[0]).not.toHaveProperty('candidateCount');expect(buildVideoBatch([{...d,candidateCount:4}]).shots[0].candidateCount).toBe(4);expect(()=>buildVideoBatch(Array.from({length:9},(_,i)=>({...d,shotId:String(i),candidateCount:4})))).toThrow('32');});
});

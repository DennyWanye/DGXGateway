#!/usr/bin/env python3
"""Build local-only H3 requests; refuse path traversal and task/profile mismatches."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from urllib.parse import urlparse, unquote

TASKS={"fl2va-official":{"t2va","fl2va"},"ref2va-official":{"ref2va"},"ref2va-heretic":{"ref2va"},"hybrid-experimental":{"t2va","fl2va","ref2va"}}


def asset_path(uri: str, root: Path) -> Path:
    u=urlparse(uri)
    if u.scheme!="file" or u.netloc or u.query or u.fragment:
        raise ValueError("V2 accepts server-local file:///data/h3/... only; do not fetch arbitrary external URIs")
    p=Path(unquote(u.path))
    if ".." in p.parts or not p.is_relative_to('/data/h3'):
        raise ValueError("Material URI is outside /data/h3")
    local=(root/p.relative_to('/data/h3')).resolve()
    if not local.is_relative_to(root.resolve()) or not local.is_file():
        raise ValueError(f"Missing/out-of-scope local material: {local}")
    return local


def validate(payload: dict, profile: str, asset_root: Path | None=None) -> None:
    if profile not in TASKS or payload.get("task") not in TASKS[profile]:raise ValueError("Request task does not match selected profile")
    if not isinstance(payload.get("prompt"),str) or not payload["prompt"].strip():raise ValueError("Empty prompt")
    t=payload.get("target",{})
    if t.get("duration_seconds")!=4 or t.get("short_edge") not in (480,768):raise ValueError("V2 baseline is 4 seconds, short edge 480(smoke) or 768(quality)")
    if t.get("aspect_ratio") not in ('16:9','9:16','1:1','auto'):raise ValueError("Unreviewed aspect ratio")
    if payload.get("num_inference_steps") not in (5,50):raise ValueError("Use baseline grid=50; grid=5 is smoke only, not Turbo")
    if payload.get("num_outputs_per_prompt",1)!=1 or payload.get("quality")!='lossless':raise ValueError("One output and lossless request policy required")
    if not isinstance(payload.get("seed"),int):raise ValueError("Record an integer seed")
    conditions=payload.get("conditions")
    if not isinstance(conditions,list) or len(conditions)>12:raise ValueError("Invalid conditions")
    counts={"image":0,"video":0,"audio":0};keyframes=[];refs=0
    for c in conditions:
        kind=c.get("type");role=c.get("role")
        if kind not in counts or role not in ('reference','keyframe'):raise ValueError("Unsupported material type/role")
        counts[kind]+=1
        if role=='keyframe':
            if kind!='image' or c.get('frame_index') not in (0,-1):raise ValueError("Keyframe must be image at 0 or -1")
            keyframes.append(c['frame_index'])
        else:
            refs+=1
        if asset_root is not None:asset_path(c['uri'],asset_root)
    if counts['image']>9 or counts['video']>3 or counts['audio']>3:raise ValueError("Input count exceeds conservative model-card limits")
    if len(set(keyframes))!=len(keyframes) or (len(keyframes)==2 and keyframes!=[0,-1]):raise ValueError("First/last frames must be unique and ordered")
    task=payload['task']
    if task=='t2va' and conditions:raise ValueError("t2va requires no conditions")
    if task=='fl2va' and (not keyframes or refs or len(conditions)>2):raise ValueError("fl2va requires first/last image keyframes only")
    if task=='ref2va' and refs<1:raise ValueError("ref2va needs at least one reference; not interchangeable with pure text generation")


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile',choices=TASKS,required=True)
    p.add_argument('--task',choices=['t2va','fl2va','ref2va'],required=True)
    p.add_argument('--assets',type=Path,default=Path('/srv/h3-ring/assets'))
    p.add_argument('--prompt-file',type=Path,required=True)
    for name in ('image','video','audio'):p.add_argument('--'+name,action='append',default=[])
    p.add_argument('--first');p.add_argument('--last')
    p.add_argument('--seed',type=int,default=17)
    p.add_argument('--aspect-ratio',default='16:9',choices=['16:9','9:16','1:1','auto'])
    p.add_argument('--smoke',action='store_true')
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();conditions=[]
    def uri(s):
        rel=Path(s)
        if rel.is_absolute() or '..' in rel.parts:raise ValueError('Use a relative path under assets')
        result=(Path('/data/h3')/rel).as_uri();asset_path(result,a.assets);return result
    for kind in ('image','video','audio'):
        for s in getattr(a,kind):conditions.append({'type':kind,'uri':uri(s),'role':'reference'})
    for source,idx in ((a.first,0),(a.last,-1)):
        if source:conditions.append({'type':'image','uri':uri(source),'role':'keyframe','frame_index':idx})
    payload={'task':a.task,'prompt':a.prompt_file.read_text(), 'conditions':conditions,
             'target':{'short_edge':480 if a.smoke else 768,'aspect_ratio':a.aspect_ratio,'duration_seconds':4},
             'num_outputs_per_prompt':1,'seed':a.seed,'num_inference_steps':5 if a.smoke else 50,
             'flow_shift':12.0,'audio_flow_shift':3.0,'quality':'lossless'}
    validate(payload,a.profile,a.assets)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    with a.out.open('x') as f:json.dump(payload,f,ensure_ascii=False,indent=2)
    print(json.dumps({'request':str(a.out),'classification':'smoke_not_quality' if a.smoke else 'base_quality',
                      'pending':'ffprobe each media input; sync identical assets to all nodes; actual runtime schema acceptance'},ensure_ascii=False))
if __name__=='__main__':main()

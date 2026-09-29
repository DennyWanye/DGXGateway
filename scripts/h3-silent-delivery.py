#!/usr/bin/env python3
"""Make an explicitly silent delivery copy while preserving every video frame.

For MV shots whose generated audio is deliberately replaced in editing. Never
use this implicitly for all silent-reference requests: they may want ambience.
The raw generated result is preserved, and the destination must be new.
"""
import argparse, hashlib, json, pathlib, subprocess

def run(args):
    p=subprocess.run(args,capture_output=True)
    if p.returncode:raise RuntimeError(p.stderr.decode(errors='replace')[-2000:])
    return p.stdout

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source',type=pathlib.Path);p.add_argument('destination',type=pathlib.Path)
    args=p.parse_args();source=args.source.resolve(strict=True);destination=args.destination.resolve()
    if source==destination or destination.exists():p.error('Destination must be a new file; raw output is never overwritten.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    run(['ffmpeg','-n','-v','error','-i',str(source),'-map','0:v:0','-c:v','copy','-an','-movflags','+faststart',str(destination)])
    probe=json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(destination)]))
    if len(probe['streams'])!=1 or probe['streams'][0]['codec_type']!='video':
        raise RuntimeError('Unexpected output streams; delivery is not verified')
    hashes=[]
    for file in [source,destination]:
        decoded=run(['ffmpeg','-v','error','-i',str(file),'-map','0:v:0','-f','framemd5','-'])
        hashes.append(hashlib.sha256(decoded).hexdigest())
    if hashes[0]!=hashes[1]:
        raise RuntimeError('Video frame/timing preservation check failed; delivery is not verified')
    receipt={'source':str(source),'destination':str(destination),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
             'destination_sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
             'audio_stream_count':0,'decoded_video_and_timing_identical':True,'video_frame_hash':hashes[0],
             'probe':probe,'scope':'Explicit delivery derivative. Does not improve raw model audio adherence or modify the original result.'}
    destination.with_suffix('.verification.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in receipt.items() if k!='probe'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()

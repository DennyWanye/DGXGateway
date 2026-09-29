"""Authenticated asynchronous Avatar API and locally archived video delivery."""
from pathlib import Path
import re
from urllib.parse import urlparse
from .text_jobs import strict_json


def handle(handler):
    path=urlparse(handler.path).path
    if path!='/api/v1/avatar-capabilities' and path!='/api/v1/avatar-jobs' and not path.startswith('/api/v1/avatar-jobs/'):return False
    app=handler.server.application
    if not app.authorized(handler.headers.get('Cookie')) or (handler.command!='GET' and not handler._origin_allowed()):handler._json(403,{'error':'forbidden'});return True
    jobs=getattr(app,'avatar_jobs',None)
    if jobs is None:handler._json(503,{'error':'Avatar service not installed'});return True
    try:
        if handler.command=='GET':
            if path=='/api/v1/avatar-capabilities':
                handler._json(200,{'model_id':'longcat-video-avatar-1.5','segments':[1,5,19],'durations_seconds':[3.72,16.52,61.32],
                    'audio':'mono 16kHz PCM16 WAV, enough duration, at most 120 seconds','reference_image':'PNG/JPEG/WebP, required',
                    'generation':'offline INT8+DMD, fixed 480p area, shared exclusive GPU lease','active_job':jobs.active});return True
            if path=='/api/v1/avatar-jobs':handler._json(200,{'jobs':jobs.list()});return True
            parts=path.removeprefix('/api/v1/avatar-jobs/').split('/');row=jobs.get(parts[0])
            if len(parts)==1:handler._json(200,{'job':row});return True
            if parts[1]!='video' or len(parts)!=2 or not row.get('artifact'):raise ValueError('video unavailable')
            video=jobs.root/parts[0]/'result.mp4'
            if video.is_symlink() or not video.is_file() or video.stat().st_size!=row['artifact']['size_bytes']:raise ValueError('archived output unavailable')
            size=video.stat().st_size;start,end=0,size-1;status=200
            requested=handler.headers.get('Range')
            if requested:
                match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested)
                if not match or not any(match.groups()):handler._json(416,{'error':'invalid_range'});return True
                if match[1]:start=int(match[1]);end=min(int(match[2]),end) if match[2] else end
                else:start=max(0,size-int(match[2]))
                if start>end or start>=size:handler._json(416,{'error':'invalid_range'});return True
                status=206
            handler.send_response(status);handler.send_header('Content-Type','video/mp4');handler.send_header('Accept-Ranges','bytes');handler.send_header('Content-Length',str(end-start+1))
            if status==206:handler.send_header('Content-Range',f'bytes {start}-{end}/{size}')
            handler.end_headers()
            with video.open('rb') as stream:
                stream.seek(start);remaining=end-start+1
                while remaining:
                    data=stream.read(min(1024*1024,remaining))
                    if not data:break
                    handler.wfile.write(data);remaining-=len(data)
            return True
        if handler.command!='POST':handler._json(405,{'error':'method_not_allowed'});return True
        if handler.headers.get_content_type()!='application/json':raise ValueError('JSON required')
        length=int(handler.headers.get('Content-Length','-1'))
        if not 2<=length<=18*1024*1024:raise ValueError('request too large')
        body=strict_json(handler.rfile.read(length).decode())
        if path=='/api/v1/avatar-jobs':row=jobs.submit(body)
        else:
            parts=path.removeprefix('/api/v1/avatar-jobs/').split('/')
            if body or len(parts)!=2 or parts[1] not in {'cancel','reconcile'}:raise ValueError('invalid action')
            row=getattr(jobs,parts[1])(parts[0])
        handler._json(202,{'job':row})
    except BlockingIOError:handler._json(409,{'error':'DGX busy; another job or recovery owns resources'})
    except FileNotFoundError:handler._json(404,{'error':'not_found'})
    except (ValueError,TypeError,KeyError,UnicodeError) as error:handler._json(422,{'error':str(error)[:200]})
    except Exception:handler._json(503,{'error':'Avatar operation unavailable; inspect retained job'})
    return True

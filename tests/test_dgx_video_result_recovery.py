import hashlib, tempfile, threading, unittest, urllib.request
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from DGXGateway.dgx_gateway.video_jobs import VideoJobs
class RecoveryTests(unittest.TestCase):
    def test_real_socket_partial_download_resumes_original_id_without_post(self):
        data=b'\0\0\0\x18ftypisom'+b'x'*190000; digest=hashlib.sha256(data).hexdigest(); requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                requests.append((self.command,self.headers.get('Range'),self.headers.get('X-H3-Request-ID')))
                offset=int(self.headers.get('Range','bytes=0-').split('=')[1].split('-')[0]); self.send_response(206 if offset else 200)
                self.send_header('Content-Type','video/mp4');self.send_header('X-H3-Result-SHA256',digest)
                self.send_header('Content-Length',str(len(data)-offset))
                if offset:self.send_header('Content-Range',f'bytes {offset}-{len(data)-1}/{len(data)}')
                self.end_headers();self.wfile.write(data[offset:] if offset else data[:70000]);self.close_connection=True
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler); thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                jobs=VideoJobs.__new__(VideoJobs);jobs.root=Path(tmp);jobs.endpoint=f'http://127.0.0.1:{server.server_port}/v1/videos/sync';jobs._token=lambda:'test-local-token';jobs.http=urllib.request.build_opener(urllib.request.ProxyHandler({}));jobs.save=Mock()
                shot={'request_id':'b'*32}
                with patch('DGXGateway.dgx_gateway.video_jobs.time.sleep'):
                    result=jobs._wait_original_result({'job_id':'a'*32},shot,wait_seconds=3)
                self.assertEqual(result,('video/mp4',data));self.assertTrue(shot['resultRecovered'])
                self.assertEqual(requests,[('GET',None,'b'*32),('GET','bytes=70000-','b'*32)])
        finally:server.shutdown();server.server_close();thread.join()

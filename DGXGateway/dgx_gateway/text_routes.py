"""Small authenticated Gateway text-job API, sharing the exact manager with MCP."""
from urllib.parse import urlparse, parse_qs
from .text_jobs import TextJobError, strict_json


def handle(handler):
    path = urlparse(handler.path)
    if path.path != '/api/v1/text-capabilities' and path.path != '/api/v1/text-jobs' and not path.path.startswith('/api/v1/text-jobs/'):
        return False
    app = handler.server.application
    if not app.authorized(handler.headers.get('Cookie')) or (handler.command != 'GET' and not handler._origin_allowed()):
        handler._json(403, {'error': {'code': 'FORBIDDEN', 'message': '请求未授权'}}); return True
    jobs = getattr(app, 'text_jobs', None)
    if jobs is None:
        handler._json(503, {'error': {'code': 'TEXT_UNAVAILABLE', 'message': '文本任务未配置'}}); return True
    try:
        if handler.command == 'GET':
            if path.path == '/api/v1/text-capabilities': result = jobs.capabilities()
            elif path.path == '/api/v1/text-jobs': result = {'jobs': jobs.list()}
            elif path.path == '/api/v1/text-jobs/find':
                values = parse_qs(path.query)
                if set(values) != {'key'} or len(values['key']) != 1: raise TextJobError('TEXT_INPUT_INVALID', '原请求编号无效')
                result = {'job': jobs.find(values['key'][0])}
            else: result = {'job': jobs.get(path.path.removeprefix('/api/v1/text-jobs/'))}
            handler._json(200, result)
        elif handler.command == 'POST':
            if handler.headers.get_content_type() != 'application/json': raise TextJobError('TEXT_INPUT_INVALID', '需要JSON请求')
            length = int(handler.headers.get('Content-Length', '-1'))
            if not 2 <= length <= 300 * 1024: raise TextJobError('TEXT_INPUT_INVALID', '请求长度无效')
            body = strict_json(handler.rfile.read(length).decode())
            if not isinstance(body, dict): raise TextJobError('TEXT_INPUT_INVALID', '请求必须是对象')
            if path.path == '/api/v1/text-jobs':
                if set(body) != {'task_kind', 'inputs', 'idempotency_key'}: raise TextJobError('TEXT_INPUT_INVALID', '文本任务字段无效')
                result = jobs.submit(body['task_kind'], body['inputs'], body['idempotency_key'])
            else:
                parts = path.path.removeprefix('/api/v1/text-jobs/').split('/')
                if body or len(parts) != 2 or parts[1] not in ('cancel', 'reconcile'): raise TextJobError('TEXT_INPUT_INVALID', '任务动作无效')
                result = getattr(jobs, parts[1])(parts[0])
            handler._json(202, {'job': result})
        else: handler._json(405, {'error': {'code': 'METHOD_NOT_ALLOWED', 'message': '请求方法不支持'}})
    except TextJobError as exc: handler._json(exc.status, {'error': {'code': exc.code, 'message': str(exc)}})
    except BlockingIOError: handler._json(409, {'error': {'code': 'TEXT_BUSY', 'message': '资源繁忙'}})
    except (ValueError, TypeError, UnicodeError, RecursionError): handler._json(400, {'error': {'code': 'TEXT_INPUT_INVALID', 'message': '文本请求无效'}})
    except Exception: handler._json(503, {'error': {'code': 'TEXT_UNAVAILABLE', 'message': '文本任务暂时不可用'}})
    return True

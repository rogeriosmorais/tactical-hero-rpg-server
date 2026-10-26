"""Version 1 JSON application API. No game-client wire protocol here."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from urllib.parse import urlsplit

from .catalog import DomainError, fail
from .service import canonical


def make_server(service, host='127.0.0.1', port=0, token=None):
    if host not in ('127.0.0.1', '::1', 'localhost') and not token:
        raise ValueError('A service token is required for non-loopback binding')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            try:
                path = urlsplit(self.path)
                if path.query:
                    fail('invalid_query', 'This API does not accept query-string commands')
                if token and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                    fail('unauthorized', 'Service authentication is required', 401)
                parts = path.path.strip('/').split('/')
                body = None
                if self.command == 'POST':
                    try:
                        length = int(self.headers.get('Content-Length', '-1'))
                    except ValueError:
                        fail('invalid_length', 'Valid content length is required')
                    if not 0 <= length <= 1024 * 1024:
                        fail('invalid_length', 'Request body must be at most 1 MiB', 413)
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        fail('invalid_content_type', 'JSON content type is required', 415)
                    try:
                        body = json.loads(self.rfile.read(length))
                    except (ValueError, UnicodeError):
                        fail('invalid_json', 'Request body is not valid JSON')
                if self.command == 'GET' and path.path == '/health':
                    result = {'status': 'ok', 'api_version': 1}
                elif self.command == 'GET' and parts == ['api', 'v1', 'catalog']:
                    result = {'catalog': service.catalog, 'digest': service.catalog_digest}
                elif self.command == 'POST' and parts == ['api', 'v1', 'players']:
                    result = {'player': service.create_player(body)}
                elif len(parts) == 4 and parts[:3] == ['api', 'v1', 'players'] and self.command == 'GET':
                    result = {'player': service.get_player(parts[3])}
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'commands' and self.command == 'POST':
                    result = service.execute(parts[3], body)
                elif len(parts) == 6 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'battles' and self.command == 'GET':
                    result = {'battle': service.get_battle(parts[3], parts[5])}
                else:
                    fail('not_found', 'Route is not part of API v1', 404)
                self.send_json(200, result)
            except DomainError as error:
                self.send_json(error.status, {'error': {'code': error.code, 'message': str(error)}})
            except Exception:
                self.send_json(500, {'error': {'code': 'internal_error', 'message': 'Request failed'}})

        def send_json(self, status, value):
            payload = canonical(value).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer((host, port), Handler)

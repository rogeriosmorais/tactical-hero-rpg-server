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
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'raid-energy' and self.command == 'GET':
                    result = service.get_raid_energy(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'roguelite' and self.command == 'GET':
                    result = service.get_roguelite(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'free-crates' and self.command == 'GET':
                    result = service.get_free_crates(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'squads' and self.command == 'GET':
                    result = service.get_squads(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'preferences' and self.command == 'GET':
                    result = service.get_preferences(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'client-settings' and self.command == 'GET':
                    result = service.get_client_settings(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'challenge-limits' and self.command == 'GET':
                    result = service.get_challenge_limits(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'augmentations' and self.command == 'GET':
                    result = service.get_augmentations(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'objectives' and self.command == 'GET':
                    result = service.get_objectives(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'passes' and self.command == 'GET':
                    result = service.get_passes(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'access' and self.command == 'GET':
                    result = service.get_access(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'progression' and self.command == 'GET':
                    result = service.get_progression(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'resources' and self.command == 'GET':
                    result = service.get_resources(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'calendars' and self.command == 'GET':
                    result = service.get_calendars(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'milestones' and self.command == 'GET':
                    result = service.get_milestones(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'alliances' and self.command == 'GET':
                    result = service.get_alliances(parts[3])
                elif len(parts) in (5, 6) and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'raids' and self.command == 'GET':
                    result = service.get_raids(parts[3], parts[5] if len(parts) == 6 else None)
                elif len(parts) == 6 and parts[:3] == ['api', 'v1', 'players'] and parts[4:] == ['alliances', 'search'] and self.command == 'POST':
                    result = service.search_alliances(parts[3], body)
                elif len(parts) == 6 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'alliances' and self.command == 'GET':
                    result = service.get_alliances(parts[3], parts[5])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'receipt-lookup' and self.command == 'POST':
                    result = service.lookup_receipt(parts[3], body)
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'mail' and self.command == 'GET':
                    result = service.get_mail(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'raid-schedule' and self.command == 'GET':
                    result = service.get_raid_schedule(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'ranked-arena' and self.command == 'GET':
                    result = service.get_ranked_arena(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'seasonal-boost' and self.command == 'GET':
                    result = service.get_seasonal_boost(parts[3])
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'blitz' and self.command == 'GET':
                    result = service.get_blitz(parts[3])
                elif len(parts) in (5, 6) and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'dark-dimension' and self.command == 'GET':
                    result = service.get_dark_dimension(parts[3], parts[5] if len(parts) == 6 else None)
                elif len(parts) == 5 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'commands' and self.command == 'POST':
                    result = service.execute(parts[3], body)
                elif len(parts) == 6 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'battles' and self.command == 'GET':
                    result = {'battle': service.get_battle(parts[3], parts[5])}
                elif len(parts) == 6 and parts[:3] == ['api', 'v1', 'players'] and parts[4] == 'towers' and self.command == 'GET':
                    result = service.get_tower(parts[3], parts[5])
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

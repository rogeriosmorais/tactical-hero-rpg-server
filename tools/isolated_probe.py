"""Cold public-service process with access limited to its exported tree."""
import json
from pathlib import Path
import sys
import threading
import tempfile
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
stdlib = Path(sys.base_prefix).resolve()
opened = []
scratch = ROOT / 'scratch'
scratch.mkdir()
tempfile.tempdir = str(scratch)


def audit(event, arguments):
    if event in ('open', 'sqlite3.connect') and isinstance(arguments[0], (str, bytes)):
        path = Path(arguments[0]).resolve()
        if not path.is_relative_to(ROOT) and not path.is_relative_to(stdlib):
            raise RuntimeError('Unexpected external filesystem dependency: ' + str(path))
        opened.append(str(path))
    if event == 'socket.connect':
        address = arguments[1]
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
            raise RuntimeError('Unexpected external network dependency')


sys.addaudithook(audit)
from game_server.http_api import make_server
from game_server.service import GameService

core_tests = 0
if '--core-tests' in sys.argv:
    import unittest
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful() or result.skipped or result.testsRun == 0 or result.testsRun != suite.countTestCases():
        raise RuntimeError('Isolated core suite failed')
    core_tests = result.testsRun

service = GameService(ROOT / 'probe.sqlite')
server = make_server(service)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = 'http://127.0.0.1:' + str(server.server_port)
try:
    with urlopen(base + '/health') as response:
        assert json.load(response)['status'] == 'ok'
    request = Request(base + '/api/v1/players', json.dumps({'player_id': 'isolated', 'display_name': 'Independent'}).encode(),
                      {'Content-Type': 'application/json'})
    with urlopen(request) as response:
        assert json.load(response)['player']['wallet']['credits'] == 100
    print(json.dumps({'status': 'passed', 'private_files_available': False, 'external_site_packages_enabled': not sys.flags.no_site,
                      'filesystem_accesses': len(opened), 'http_started': True, 'core_tests': core_tests}))
finally:
    server.shutdown()
    server.server_close()
    thread.join()

"""Player-scoped receipt inspection under the server authentication policy."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from game_server.http_api import make_server
from game_server.service import GameService, canonical
from test_loot_products import product_catalog


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = GameService(Path(self.temp.name) / 'game.sqlite', product_catalog())
        for player in ('a', 'b'):
            self.service.create_player({'player_id': player, 'display_name': player})
        self.service.execute('a', {'command': 'shop.purchase', 'idempotency_key': 'fund',
            'parameters': {'offer_id': 'fragments', 'quantity': 1}})
        self.request = {'command': 'loot.open', 'idempotency_key': 'paid',
            'parameters': {'product_id': 'pack', 'revision': 'release-1', 'quantity': 1,
                           'request_fingerprint': 'a' * 64}}
        self.result = self.service.execute('a', self.request)
        self.digest = hashlib.sha256(canonical(self.request).encode()).hexdigest()
        self.server = make_server(self.service, token='test-service')
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def lookup(self, player='a', token='test-service', **changes):
        body = {'idempotency_key': 'paid', 'request_hash': self.digest, **changes}
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        request = Request('http://127.0.0.1:' + str(self.server.server_port) +
            '/api/v1/players/' + player + '/receipt-lookup', json.dumps(body).encode(),
            headers)
        with urlopen(request) as response:
            return json.load(response)

    def test_exact_conflict_missing_player_scope_and_restart_are_read_only(self):
        before = self.service.get_player('a')
        self.assertEqual(self.lookup(), {'status': 'match', 'result': self.result})
        self.assertEqual(self.lookup(request_hash='b' * 64), {'status': 'conflict'})
        self.assertEqual(self.service.lookup_receipt('a', {'idempotency_key': 'paid'}), {'status': 'conflict'})
        self.assertEqual(self.lookup(idempotency_key='fund'), {'status': 'conflict'})
        self.assertEqual(self.lookup(idempotency_key='absent'), {'status': 'missing'})
        self.assertEqual(self.lookup(player='b'), {'status': 'missing'})
        restarted = GameService(self.service.database, self.service.catalog)
        self.assertEqual(restarted.lookup_receipt('a', {'idempotency_key': 'paid',
            'request_hash': self.digest}), {'status': 'match', 'result': self.result})
        self.assertEqual(self.service.get_player('a'), before)
        with closing(self.service.connect()) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 2)

    def test_http_authentication_and_closed_schema(self):
        for arguments, status in (({'token': None}, 401), ({'token': 'wrong'}, 401),
                                  ({'extra': {}}, 400),
                                  ({'request_hash': 'bad'}, 400),
                                  ({'idempotency_key': []}, 400), ({'player': 'unknown'}, 404)):
            with self.subTest(arguments=arguments), self.assertRaises(HTTPError) as error:
                self.lookup(**arguments)
            self.assertEqual(error.exception.code, status)
    def test_tokenless_loopback_lookup(self):
        unprotected = make_server(self.service)
        try:
            thread = threading.Thread(target=unprotected.serve_forever, daemon=True)
            thread.start()
            protected = self.server
            self.server = unprotected
            try:
                self.assertEqual(self.lookup(token=None),
                                 {'status': 'match', 'result': self.result})
            finally:
                self.server = protected
        finally:
            unprotected.shutdown()
            thread.join()
            unprotected.server_close()

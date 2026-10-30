"""Real loopback HTTP/authentication tests with independently authored content."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from game_server.http_api import make_server
from game_server.service import GameService
from test_roguelite import authored_catalog


class RogueliteHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = GameService(Path(self.temp.name) / 'game.sqlite', authored_catalog())
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.server = make_server(self.service, port=0, token='authored-test-token')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = f'http://127.0.0.1:{self.server.server_port}/api/v1/players/a'

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, suffix, body=None, token='authored-test-token'):
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
        request = Request(self.url + suffix,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def command(self, command, key, **parameters):
        return self.request('/commands', {'command': command, 'idempotency_key': key, 'parameters': parameters})

    def test_read_is_authenticated_and_pure_and_player_creation_stays_level_one(self):
        before = self.service.get_player('a')
        self.assertEqual(self.request('/roguelite', token='wrong')[0], 401)
        status, view = self.request('/roguelite')
        self.assertEqual(status, 200)
        self.assertIsNone(view['roguelite']['active_run'])
        self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(before['level'], 1)

    def test_http_single_actor_snapshot_rewards_and_replay(self):
        status, result = self.command('roguelite.start', 'start')
        self.assertEqual(status, 200)
        run = result['roguelite']['active_run']
        status, result = self.command('roguelite.begin', 'begin', run_id=run['run_id'],
            encounter_id=run['encounter']['encounter_id'], team=['scout'], formation=[7])
        self.assertEqual(status, 200)
        battle = result['battle']
        self.assertEqual(self.request('/battles/' + battle['battle_id']), (200, {'battle': battle}))
        self.assertEqual(len(battle['team']), 1)
        self.assertEqual(len(battle['enemies']), 5)
        status, result = self.command('roguelite.complete', 'win', battle_id=battle['battle_id'],
            outcome='victory', stars=3)
        self.assertEqual(status, 200)
        self.assertEqual(result['player']['experience'], 10)
        choice = result['roguelite']['active_run']['pending_choice']
        params = {'run_id': run['run_id'], 'choice_id': choice['choice_id'],
                  'option_id': choice['options'][0]['option_id']}
        before = self.service.get_player('a')
        self.assertEqual(self.command('roguelite.choose', 'forged', **params, quantity=999)[0], 400)
        self.assertEqual(self.service.get_player('a'), before)
        granted = self.command('roguelite.choose', 'claim', **params)
        self.assertEqual(granted[0], 200)
        self.assertEqual(self.command('roguelite.choose', 'claim', **params), granted)
        self.assertEqual(self.command('roguelite.choose', 'claim-again', **params)[0], 409)
        self.assertEqual(self.service.get_player('a'), granted[1]['player'])


if __name__ == '__main__':
    unittest.main()

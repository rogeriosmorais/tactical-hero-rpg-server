from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.http_api import make_server
from game_server.service import GameService


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'game.sqlite'
        self.service = GameService(self.database)
        self.player = self.service.create_player({'player_id': 'player-a', 'display_name': 'Player A'})

    def tearDown(self):
        self.temp.cleanup()

    def command(self, name, key='operation-1', **parameters):
        return self.service.execute('player-a', {'command': name, 'idempotency_key': key, 'parameters': parameters})

    def win(self):
        started = self.command('mission.begin', 'begin', mission_id='training.1', team=['scout'])
        return self.command('mission.complete', 'complete', battle_id=started['battle']['battle_id'],
                            mission_id='training.1', outcome='victory', stars=3)

    def test_create_does_not_reset_an_existing_player(self):
        self.command('character.train', character_id='scout', levels=2)
        actual = self.service.create_player({'player_id': 'player-a', 'display_name': 'Different'})
        self.assertEqual(actual['wallet']['credits'], 80)
        self.assertEqual(actual['display_name'], 'Player A')

    def test_training_cost_and_exact_replay(self):
        first = self.command('character.train', character_id='scout', levels=2)
        replay = self.command('character.train', character_id='scout', levels=2)
        self.assertEqual(first, replay)
        self.assertEqual(first['player']['wallet']['credits'], 80)
        self.assertEqual(first['player']['characters']['scout']['level'], 3)
        self.assertEqual(first['player']['characters']['scout']['power'], 36)
        self.assertEqual(first['player']['version'], 1)

    def test_key_reuse_with_changed_arguments_is_rejected(self):
        self.command('character.train', character_id='scout', levels=1)
        with self.assertRaisesRegex(DomainError, 'different command'):
            self.command('character.train', character_id='scout', levels=2)
        self.assertEqual(self.service.get_player('player-a')['wallet']['credits'], 90)

    def test_purchase_limit_and_rollback(self):
        result = self.command('shop.purchase', offer_id='training-kit', quantity=2)
        self.assertEqual(result['player']['inventory']['training-kit'], 2)
        self.assertEqual(result['rewards']['inventory']['training-kit'], 2)
        before = self.service.get_player('player-a')
        with self.assertRaises(DomainError):
            self.command('shop.purchase', 'second', offer_id='training-kit', quantity=1)
        self.assertEqual(self.service.get_player('player-a'), before)

    def test_negative_and_boolean_counts_are_rejected_without_mutation(self):
        for value in (-1, 0, True, 1.0, 1000):
            with self.subTest(value=value), self.assertRaises(DomainError):
                self.command('shop.purchase', offer_id='training-kit', quantity=value)
        self.assertEqual(self.service.get_player('player-a'), self.player)

    def test_insufficient_funds_rolls_back(self):
        self.command('character.train', character_id='scout', levels=9)
        before = self.service.get_player('player-a')
        with self.assertRaises(DomainError):
            self.command('shop.purchase', 'second', offer_id='training-kit', quantity=1)
        self.assertEqual(self.service.get_player('player-a'), before)

    def test_mission_requires_owned_distinct_team_and_unlock(self):
        for team in ([], ['absent'], ['scout', 'scout'], ['scout'] * 6):
            with self.subTest(team=team), self.assertRaises(DomainError):
                self.command('mission.begin', mission_id='training.1', team=team)
        with self.assertRaises(DomainError):
            self.command('mission.begin', mission_id='training.2', team=['scout'])
        self.assertEqual(self.service.get_player('player-a'), self.player)

    def test_battle_first_clear_and_sweep_rewards_are_authored_and_atomic(self):
        result = self.win()
        self.assertEqual(result['player']['wallet'], {'credits': 105, 'energy': 18})
        self.assertEqual(result['player']['inventory'], {'badge': 1})
        self.assertEqual(result['rewards']['inventory'], {'badge': 1})
        swept = self.command('mission.sweep', 'sweep', mission_id='training.1', quantity=2)
        self.assertEqual(swept['rewards']['wallet']['credits'], 10)
        self.assertEqual(swept['player']['wallet'], {'credits': 115, 'energy': 14})
        self.assertEqual(swept['player']['experience'], 30)
        self.assertEqual(swept['player']['inventory'], {'badge': 1})

    def test_battle_cannot_pay_a_different_player_or_mission(self):
        start = self.command('mission.begin', mission_id='training.1', team=['scout'])
        battle_id = start['battle']['battle_id']
        self.service.create_player({'player_id': 'player-b', 'display_name': 'B'})
        for player_id, mission in (('player-b', 'training.1'), ('player-a', 'training.2')):
            with self.subTest(player_id=player_id), self.assertRaises(DomainError):
                self.service.execute(player_id, {'command': 'mission.complete', 'idempotency_key': 'finish',
                    'parameters': {'battle_id': battle_id, 'mission_id': mission, 'outcome': 'victory', 'stars': 3}})
        self.assertEqual(self.service.get_player('player-a')['wallet']['credits'], 100)

    def test_cannot_complete_twice_under_a_new_key(self):
        result = self.win()
        with self.assertRaises(DomainError):
            self.command('mission.complete', 'different', battle_id=result['battle']['battle_id'],
                         mission_id='training.1', outcome='victory', stars=3)
        self.assertEqual(self.service.get_player('player-a')['wallet']['credits'], 105)

    def test_failed_battle_grants_nothing_and_cannot_sweep(self):
        start = self.command('mission.begin', mission_id='training.1', team=['scout'])
        result = self.command('mission.complete', 'finish', battle_id=start['battle']['battle_id'],
                              mission_id='training.1', outcome='defeat', stars=0)
        self.assertEqual(result['player']['wallet'], {'credits': 100, 'energy': 18})
        with self.assertRaises(DomainError):
            self.command('mission.sweep', 'sweep', mission_id='training.1', quantity=1)

    def test_concurrent_duplicate_commands_charge_once(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.command('shop.purchase', offer_id='training-kit', quantity=1), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.service.get_player('player-a')['wallet']['credits'], 80)

    def test_restart_preserves_state_and_receipts(self):
        first = self.command('shop.purchase', offer_id='training-kit', quantity=1)
        self.service = GameService(self.database)
        self.assertEqual(self.command('shop.purchase', offer_id='training-kit', quantity=1), first)
        self.assertEqual(self.service.get_player('player-a'), first['player'])

    def test_changed_catalog_requires_explicit_migration(self):
        changed = deepcopy(DEMO_CATALOG)
        changed['starting_wallet']['credits'] += 1
        with self.assertRaises(DomainError):
            GameService(self.database, changed)
        self.assertEqual(GameService(self.database).get_player('player-a'), self.player)

    def test_foreign_database_is_rejected_without_schema_changes(self):
        path = Path(self.temp.name) / 'unrelated.sqlite'
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE unrelated (value TEXT)')
            connection.execute("INSERT INTO unrelated VALUES ('preserve')")
        with self.assertRaises(DomainError):
            GameService(path)
        with closing(sqlite3.connect(path)) as connection, connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertEqual(tables, ['unrelated'])
            self.assertEqual(connection.execute('SELECT value FROM unrelated').fetchone()[0], 'preserve')

    def test_catalog_negative_rewards_cycles_and_unknown_fields_rejected(self):
        for kind in ('negative', 'cycle', 'wire'):
            catalog = deepcopy(DEMO_CATALOG)
            if kind == 'negative':
                catalog['missions']['training.1']['rewards']['wallet']['credits'] = -1
            elif kind == 'cycle':
                catalog['missions']['training.1']['prerequisite'] = 'training.2'
            else:
                catalog['request_body'] = {}
            with self.subTest(kind=kind), self.assertRaises(DomainError):
                validate_catalog(catalog)

    def test_unknown_command_and_client_supplied_reward_are_rejected(self):
        for command, parameters in (('/unsupported/action', {}), ('shop.purchase', {'offer_id': 'training-kit', 'quantity': 1, 'rewards': {'credits': 999}})):
            with self.assertRaises(DomainError):
                self.command(command, **parameters)
        self.assertEqual(self.service.get_player('player-a'), self.player)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(GameService(Path(self.temp.name) / 'http.sqlite'), token='own-service-token')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, token='own-service-token'):
        request = Request(self.url + path, json.dumps(body).encode() if body is not None else None,
                          {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
        with urlopen(request) as response:
            return json.load(response)

    def test_real_http_roundtrip_and_unknown_wire_route(self):
        self.assertEqual(self.request('/health')['api_version'], 1)
        player = self.request('/api/v1/players', {'player_id': 'http-player', 'display_name': 'HTTP'})['player']
        self.assertEqual(self.request('/api/v1/players/http-player')['player'], player)
        with self.assertRaises(HTTPError) as error:
            self.request('/unsupported/route', {})
        self.assertEqual(error.exception.code, 404)

    def test_authentication_and_closed_json_contract(self):
        with self.assertRaises(HTTPError) as error:
            self.request('/api/v1/catalog', token='wrong')
        self.assertEqual(error.exception.code, 401)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/v1/players', {'player_id': 'p', 'display_name': 'P', 'response_body': {}})
        self.assertEqual(error.exception.code, 400)

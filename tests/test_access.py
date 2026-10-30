"""Access grants are atomic application state, not client-side level decisions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['missions']['training.1']['rewards'] = {'experience': 200}
        self.catalog['access_rules'] = {
            'initial': {'required_level': 1, 'mission_ids': [], 'item_id': 'initial-license'},
            'earned': {'required_level': 2, 'mission_ids': ['training.1'], 'item_id': 'earned-license'},
            'later': {'required_level': 4, 'mission_ids': [], 'item_id': 'later-license'}}
        self.service = GameService(self.path, self.catalog, clock=lambda: 1234)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def command(self, name, key, **parameters):
        return self.service.execute('a', {'command': name, 'idempotency_key': key, 'parameters': parameters})

    def test_creation_reads_and_level_and_mission_grants_are_persistent_once(self):
        before = self.service.get_player('a')
        self.assertEqual(before['inventory']['initial-license'], 1)
        self.assertTrue(self.service.get_access('a')['access']['initial']['unlocked'])
        self.assertFalse(self.service.get_access('a')['access']['earned']['unlocked'])
        self.assertEqual(self.service.get_player('a'), before)
        battle = self.command('mission.begin', 'begin', mission_id='training.1', team=['scout'])['battle']
        request = dict(battle_id=battle['battle_id'], mission_id='training.1', outcome='victory', stars=3)
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.command('mission.complete', 'win', **request), range(4)))
        self.assertTrue(all(reply == replies[0] for reply in replies))
        result = replies[0]
        self.assertEqual(result['access_grants'], ['earned'])
        self.assertEqual(result['access']['earned']['granted_at'], 1234)
        self.assertFalse(result['access']['later']['unlocked'])
        self.assertEqual(result['player']['inventory']['earned-license'], 1)
        restarted = GameService(self.path, self.catalog)
        self.assertEqual(restarted.get_access('a'), self.service.get_access('a'))
        self.assertEqual(restarted.create_player({'player_id': 'a', 'display_name': 'Changed'}), result['player'])
        next_result = self.command('character.train', 'train', character_id='scout', levels=1)
        self.assertEqual(next_result['access_grants'], [])
        self.assertEqual(next_result['player']['inventory']['earned-license'], 1)

    def test_failed_commands_and_forged_unlocks_do_not_grant_anything(self):
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('access.unlock', 'forge', access_id='earned')
        with self.assertRaises(DomainError):
            self.command('mission.sweep', 'uncleared', mission_id='training.1', quantity=1)
        self.assertEqual(self.service.get_player('a'), before)

    def test_entitlement_reward_unlocks_server_access_without_double_payment(self):
        # Legacy gent rewards award the access token even before the normal
        # level/mission condition becomes eligible.
        catalog = deepcopy(self.catalog)
        catalog['access_rules']['earned']['required_level'] = 4
        catalog['missions']['training.1']['rewards'] = {
            'experience': 200, 'inventory': {'earned-license': 1}}
        path = Path(self.temp.name) / 'earned-token.sqlite'
        service = GameService(path, catalog, clock=lambda: 1234)
        service.create_player({'player_id': 'gift', 'display_name': 'Gift'})
        self.assertFalse(service.get_access('gift')['access']['earned']['unlocked'])
        battle = service.execute('gift', {
            'command': 'mission.begin', 'idempotency_key': 'begin',
            'parameters': {'mission_id': 'training.1', 'team': ['scout']}})['battle']
        operation = {'command': 'mission.complete', 'idempotency_key': 'complete',
                     'parameters': {'battle_id': battle['battle_id'],
                                    'mission_id': 'training.1', 'outcome': 'victory',
                                    'stars': 3}}
        result = service.execute('gift', operation)
        self.assertIn('earned', result['access_grants'])
        self.assertEqual(result['player']['inventory']['earned-license'], 1)
        self.assertEqual(result['access']['earned'], {
            'unlocked': True, 'granted_at': 1234, 'eligible': False})
        self.assertEqual(service.execute('gift', operation), result)
        after = service.get_player('gift')
        self.assertEqual(after['inventory']['earned-license'], 1)
        reloaded = GameService(path, catalog)
        self.assertEqual(reloaded.get_access('gift')['access']['earned']['granted_at'], 1234)
        self.assertEqual(reloaded.get_player('gift'), after)

    def test_invalid_requirement_and_duplicate_grant_items_are_rejected(self):
        for update in ({'mission_ids': ['missing']}, {'required_level': True}, {'required_level': 99999},
                       {'mission_ids': ['training.1', 'training.1']}, {'item_id': 'initial-license'}):
            catalog = deepcopy(self.catalog)
            catalog['access_rules']['earned'].update(update)
            with self.assertRaises(DomainError):
                validate_catalog(catalog)

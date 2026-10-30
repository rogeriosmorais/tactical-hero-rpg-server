"""Saved teams use authored gates and earned capacity, with atomic receipts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.profile_cutover import import_complete_player
from game_server.service import GameService


class SquadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['squad_groups'] = {
            'favorites': {'required_level': 2, 'capacity': 3, 'team_size': 5, 'capacity_item': 'team-slot'},
            'last-team': {'required_level': 1, 'capacity': 1, 'team_size': 5}}
        self.catalog['offers']['slot'] = {'cost': {'credits': 1}, 'rewards': {'inventory': {'team-slot': 1}},
                                         'purchase_limit': 3}
        self.catalog['missions']['training.1']['rewards'] = {'experience': 100}
        self.database = Path(self.temp.name) / 'game.sqlite'
        self.service = GameService(self.database, self.catalog)
        for player in ('a', 'b'):
            self.service.create_player({'player_id': player, 'display_name': player})

    def command(self, name, key, **parameters):
        return self.service.execute('a', {'command': name, 'idempotency_key': key, 'parameters': parameters})

    def unlock(self):
        battle = self.command('mission.begin', 'begin', mission_id='training.1', team=['scout'])['battle']
        self.command('mission.complete', 'win', battle_id=battle['battle_id'], mission_id='training.1',
                     outcome='victory', stars=3)

    def test_earned_slots_gates_clear_and_restart(self):
        before = self.service.get_player('a')
        self.assertFalse(self.service.get_squads('a')['squads']['favorites']['available'])
        with self.assertRaisesRegex(DomainError, 'level requirement'):
            self.command('squad.save', 'save', groups={'favorites': [['scout']]})
        self.assertEqual(self.service.get_player('a'), before)
        self.unlock()
        with self.assertRaisesRegex(DomainError, 'capacity'):
            self.command('squad.save', 'save', groups={'favorites': [['scout']]})
        self.command('shop.purchase', 'slot', offer_id='slot', quantity=1)
        result = self.command('squad.save', 'save', groups={'favorites': [['scout', None]], 'last-team': [['scout']]})
        self.assertEqual(result['wallet_changes'], [])
        restarted = GameService(self.database, self.catalog)
        self.assertEqual(restarted.get_squads('a')['squads']['favorites']['teams'], [['scout', None]])
        self.assertEqual(self.command('squad.save', 'save', groups={'favorites': [['scout', None]], 'last-team': [['scout']]}), result)
        self.command('squad.save', 'clear', groups={'favorites': []})
        self.assertEqual(self.service.get_squads('a')['squads']['favorites']['teams'], [])
        self.assertEqual(self.service.get_squads('b')['squads']['last-team']['teams'], [])

    def test_invalid_batch_is_atomic_and_reads_do_not_change_player(self):
        for teams in ([['scout', 'scout']], [['undefined']], [[True]], [[{}]], [['scout'] * 6], 'invalid'):
            before = self.service.get_player('a')
            with self.subTest(teams=teams), self.assertRaises(DomainError):
                self.command('squad.save', 'bad', groups={'last-team': teams})
            self.assertEqual(self.service.get_player('a'), before)
        with self.assertRaises(DomainError):
            self.command('squad.save', 'batch', groups={'last-team': [['scout']], 'undefined': []})
        self.assertEqual(self.service.get_squads('a')['squads']['last-team']['teams'], [])
        before = self.service.get_player('a')
        self.service.get_squads('a')
        self.assertEqual(self.service.get_player('a'), before)

    def test_concurrent_retry_and_changed_key(self):
        def save(_):
            return self.command('squad.save', 'same', groups={'last-team': [['scout']]})
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save, range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.service.get_player('a')['version'], 1)
        with self.assertRaisesRegex(DomainError, 'different command'):
            self.command('squad.save', 'same', groups={'last-team': []})

    def test_catalog_rejects_native_fields_and_invalid_limits(self):
        for change in ({'capacity': True}, {'team_size': 6}, {'capacity': -1},
                       {'capacity_item': 'bad/id'}, {'native_category': 'wire'}):
            catalog = deepcopy(self.catalog)
            catalog['squad_groups']['last-team'].update(change)
            with self.subTest(change=change), self.assertRaises(DomainError):
                validate_catalog(catalog)

    def test_trusted_saved_squad_cutover_preserves_earned_slots_and_rejects_invalid_history(self):
        original = GameService(Path(self.temp.name) / 'source.sqlite', self.catalog)
        original.create_player({'player_id': 'migrant', 'display_name': 'Migrant'})
        def command(name, key, **parameters):
            return original.execute('migrant', {'command': name,
                'idempotency_key': key, 'parameters': parameters})
        battle = command('mission.begin', 'begin', mission_id='training.1', team=['scout'])['battle']
        command('mission.complete', 'win', battle_id=battle['battle_id'],
                mission_id='training.1', outcome='victory', stars=3)
        command('shop.purchase', 'slot', offer_id='slot', quantity=1)
        snapshot = original.get_player('migrant')
        snapshot['squads'] = {'favorites': [['scout', None]], 'last-team': [['scout']]}
        snapshot['squads_updated_at'] = 12345
        provenance = {'source_digest': 'b' * 64,
            'source_sections': ['normalized-fixture', 'receipts'],
            'section_map': {'normalized-fixture': sorted(snapshot),
                            'receipts': ['legacy_receipts']},
            'used_command_keys': ['prior-squad-save']}
        cases = []
        for change in (
            lambda s: s['squads'].update(favorites=[['scout'], ['scout']]),
            lambda s: s['squads'].update(favorites=[['unowned-character']]),
            lambda s: s['squads'].update(favorites=[['scout', 'scout']]),
            lambda s: s['squads'].update(unknown=[['scout']]),
            lambda s: s.update(squads_updated_at=-1),
            lambda s: s.pop('squads_updated_at'),
            lambda s: s.update(level=1, experience=0),
        ):
            candidate = deepcopy(snapshot)
            change(candidate)
            cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(DomainError):
                import_complete_player(self.service, candidate, provenance)
            with self.assertRaises(DomainError) as missing:
                self.service.get_player('migrant')
            self.assertEqual(missing.exception.code, 'player_not_found')
        self.assertEqual(import_complete_player(self.service, snapshot, provenance)['status'], 'imported')
        self.assertEqual(self.service.get_squads('migrant')['squads']['favorites']['teams'],
                         [['scout', None]])
        self.assertEqual(self.service.get_squads('migrant')['squads']['favorites']['capacity'], 1)
        self.assertEqual(self.service.get_player('migrant')['squads_updated_at'], 12345)
        self.assertEqual(import_complete_player(self.service, snapshot, provenance)['status'], 'already_imported')

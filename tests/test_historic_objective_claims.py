"""Legacy claim-window cutover without duplicate objective reward grants."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DomainError
from game_server.historic_objectives import import_claims
from game_server.service import GameService
from tools.import_historic_objective_claims import import_file
from test_objectives import objective_catalog
import json


class HistoricObjectiveClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = objective_catalog()
        self.now = 172800
        self.db = Path(self.temp.name) / 'player.sqlite'
        self.service = GameService(self.db, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def test_import_blocks_duplicate_grants_and_replay_does_not_increment_version(self):
        before = self.service.get_player('a')
        result = import_claims(self.service, 'a', {'win': ['1', '2'], 'purchase': ['2']})
        self.assertEqual(result['imported'], {'win': ['1', '2'], 'purchase': ['2']})
        player = self.service.get_player('a')
        self.assertEqual(player['wallet'], before['wallet'])
        self.assertEqual(player['inventory'], before['inventory'])
        self.assertEqual(self.service.get_objectives('a')['objectives']['win']['claimed'], True)
        self.assertEqual(import_claims(self.service, 'a', {'win': ['1', '2'], 'purchase': ['2']})['imported'], {})
        self.assertEqual(self.service.get_player('a'), player)
        self.service = GameService(self.db, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.service.get_objectives('a')['objectives']['purchase']['claimed'], True)
        self.assertEqual(self.service.get_player('a'), player)
        with self.assertRaises(DomainError):
            self.service.execute('a', {'command': 'objective.claim', 'idempotency_key': 'new-claim',
                                       'parameters': {'objective_ids': ['win']}})
        self.assertEqual(self.service.get_player('a'), player)

    def test_earned_unclaimed_legacy_objective_cannot_pay_twice_after_import(self):
        def issue(name, key, **params):
            return self.service.execute('a', {'command': name, 'idempotency_key': key,
                                              'parameters': params})
        battle = issue('mission.begin', 'legacy-start', mission_id='training.1',
                       team=['scout'])['battle']
        issue('mission.complete', 'legacy-win', battle_id=battle['battle_id'],
              mission_id='training.1', outcome='victory', stars=3)
        issue('mission.sweep', 'legacy-sweep', mission_id='training.1', quantity=1)
        self.assertTrue(self.service.get_objectives('a')['objectives']['win']['claimable'])
        before = self.service.get_player('a')
        import_claims(self.service, 'a', {'win': ['2']})
        after = self.service.get_player('a')
        self.assertEqual(after['wallet'], before['wallet'])
        self.assertEqual(after['inventory'], before['inventory'])
        self.assertFalse(self.service.get_objectives('a')['objectives']['win']['claimable'])
        with self.assertRaises(DomainError):
            issue('objective.claim', 'double-pay', objective_ids=['win'])
        self.assertEqual(self.service.get_player('a'), after)

    def test_import_is_monotonic_across_windows_and_keeps_real_claim(self):
        import_claims(self.service, 'a', {'win': ['0']})
        self.assertFalse(self.service.get_objectives('a')['objectives']['win']['claimed'])
        self.now = 259200
        import_claims(self.service, 'a', {'win': ['2', '3']})
        self.assertEqual(self.service.get_player('a')['objective_claims']['win'],
                         {'0': True, '2': True, '3': True})
        self.assertTrue(self.service.get_objectives('a')['objectives']['win']['claimed'])
        with ThreadPoolExecutor(max_workers=3) as pool:
            receipts = list(pool.map(lambda _: import_claims(self.service, 'a', {'win': ['0', '2', '3']}), range(3)))
        self.assertTrue(all(item['imported'] == {} for item in receipts))
        self.now = 345600
        self.assertFalse(self.service.get_objectives('a')['objectives']['win']['claimed'])

    def test_failed_import_never_partially_updates_existing_player(self):
        before = self.service.get_player('a')
        bad = ({'win': ['2'], 'missing': ['2']}, {'win': ['2', '2']},
               {'win': ['2'], 'purchase': [True]},
               {'win': ['1.5']}, {'win': ['lifetime']}, {'tags': ['not-a-number']},
               {'win': ['02']}, ['invalid'])
        for claim in bad:
            with self.assertRaises(DomainError):
                import_claims(self.service, 'a', claim)
            self.assertEqual(self.service.get_player('a'), before)

    def test_neutral_export_file_replay_is_executable_and_does_not_award(self):
        catalog_file = Path(self.temp.name) / 'catalog.json'
        catalog_file.write_text(json.dumps(self.catalog), encoding='utf-8')
        exported = Path(self.temp.name) / 'neutral.json'
        exported.write_text(json.dumps({'schema_version': 1,
                                        'claims': {'win': ['2'], 'purchase': ['2']}}), encoding='utf-8')
        before = self.service.get_player('a')
        self.assertEqual(import_file(self.db, catalog_file, 'a', exported)['imported'],
                         {'win': ['2'], 'purchase': ['2']})
        after = self.service.get_player('a')
        self.assertEqual(before['wallet'], after['wallet'])
        self.assertEqual(before['inventory'], after['inventory'])
        self.assertEqual(import_file(self.db, catalog_file, 'a', exported)['imported'], {})
        self.assertEqual(self.service.get_player('a'), after)
        with self.assertRaises(ValueError):
            wrong = Path(self.temp.name) / 'invalid.json'
            wrong.write_text(json.dumps({'schema_version': 2,
                                         'claims': {'win': ['2']}}), encoding='utf-8')
            import_file(self.db, catalog_file, 'a', wrong)


if __name__ == '__main__':
    unittest.main()

"""Migration of legacy event-tag receipt history into server-owned signals."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DomainError
from game_server.historic_signals import import_historic_reward_signals
from game_server.service import GameService
from test_objectives import objective_catalog
from tools.import_historic_signals import import_file
import json


class HistoricSignalImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1010
        self.service = GameService(Path(self.temp.name) / 'signals.sqlite',
                                   objective_catalog(), clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.service.create_player({'player_id': 'b', 'display_name': 'B'})
        self.original = {'receipt_id': 'old-a9', 'at': 1015,
                         'labels': ['alpha', 'beta'], 'quantity': 3}

    def test_exact_once_restart_and_evidence_in_objective_counter(self):
        prior = self.service.get_player('a')
        result = import_historic_reward_signals(self.service, 'a', [self.original])
        self.assertEqual(result['imported_count'], 1)
        self.assertEqual(result['journal_count'], 1)
        self.assertEqual(result['imported'], [
            {'signal_id': 'legacy.old-a9', 'at': 1015, 'labels': ['alpha', 'beta'],
             'quantity': 3}])
        after = self.service.get_player('a')
        self.assertEqual(after['version'], prior['version'] + 1)
        self.assertEqual(after['wallet'], prior['wallet'])
        self.assertEqual(after['inventory'], prior['inventory'])
        self.assertEqual(self.service.get_player('b')['reward_signals'] if
                         'reward_signals' in self.service.get_player('b') else [], [])
        self.now = 1020
        self.assertEqual(self.service.get_objectives('a')['counters']['tags']['value'], 3)
        self.now = 1061
        late = self.service.get_objectives('a')
        self.assertEqual(late['counters']['tags']['value'], 3)
        self.assertFalse(late['counters']['tags']['period']['active'])
        self.service = GameService(self.service.database, self.service.catalog, clock=lambda: self.now)
        self.assertEqual(import_historic_reward_signals(self.service, 'a', [self.original])['imported_count'], 0)
        self.assertEqual(self.service.get_player('a'), after)
        self.assertEqual(import_historic_reward_signals(self.service, 'b', [self.original])['imported_count'], 1)
        self.assertEqual(len(self.service.get_player('a')['reward_signals']), 1)

    def test_conflicting_replay_is_atomic_and_does_not_mint(self):
        import_historic_reward_signals(self.service, 'a', [self.original])
        before = self.service.get_player('a')
        replacement = {**self.original, 'quantity': 999}
        with self.assertRaises(DomainError) as error:
            import_historic_reward_signals(self.service, 'a', [
                {'receipt_id': 'different', 'at': 1014, 'labels': ['alpha'], 'quantity': 1},
                replacement])
        self.assertEqual(error.exception.code, 'historic_signal_conflict')
        self.assertEqual(self.service.get_player('a'), before)
        with self.assertRaises(DomainError):
            import_historic_reward_signals(self.service, 'a', [self.original, deepcopy(self.original)])
        self.assertEqual(self.service.get_player('a'), before)

    def test_validation_fails_closed_with_no_partial_imports(self):
        cases = [
            [{'receipt_id': 'old-a9', 'at': True, 'labels': ['alpha'], 'quantity': 1}],
            [{'receipt_id': 'old-a9', 'at': -1, 'labels': ['alpha'], 'quantity': 1}],
            [{'receipt_id': 'old-a9', 'at': 1, 'labels': ['alpha', 'alpha'], 'quantity': 1}],
            [{'receipt_id': 'old-a9', 'at': 1, 'labels': ['alpha'], 'quantity': 0}],
            [{'receipt_id': 'bad/id', 'at': 1, 'labels': ['alpha'], 'quantity': 1}],
            [{'receipt_id': 'old-a9', 'at': 1, 'labels': ['alpha'],
              'quantity': 1, 'wallet': {'credits': 999}}],
        ]
        original = self.service.get_player('a')
        for case in cases:
            with self.assertRaises(DomainError):
                import_historic_reward_signals(self.service, 'a', case)
            self.assertEqual(self.service.get_player('a'), original)
        with self.assertRaises(DomainError):
            import_historic_reward_signals(self.service, 'missing', [self.original])

    def test_concurrent_import_retries_cannot_double_count(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda _: import_historic_reward_signals(self.service, 'a', [self.original]),
                range(8)))
        self.assertEqual(sum(row['imported_count'] for row in results), 1)
        self.assertEqual(len(self.service.get_player('a')['reward_signals']), 1)
        self.assertEqual(self.service.get_player('a')['version'], 1)

    def test_standalone_cutover_command_imports_neutral_export_once(self):
        parent = Path(self.temp.name)
        catalog = parent / 'catalog.json'
        source = parent / 'neutral.json'
        catalog.write_text(json.dumps(self.service.catalog), encoding='utf-8')
        source.write_text(json.dumps({'schema_version': 1, 'signals': [self.original]}), encoding='utf-8')
        result = import_file(self.service.database, catalog, 'a', source)
        self.assertEqual(result['imported_count'], 1)
        self.assertEqual(import_file(self.service.database, catalog, 'a', source)['imported_count'], 0)
        self.assertEqual(len(self.service.get_player('a')['reward_signals']), 1)
        source.write_text(json.dumps({'schema_version': 2, 'signals': [self.original]}), encoding='utf-8')
        with self.assertRaises(ValueError):
            import_file(self.service.database, catalog, 'a', source)
        self.assertEqual(len(self.service.get_player('a')['reward_signals']), 1)

if __name__ == '__main__':
    unittest.main()

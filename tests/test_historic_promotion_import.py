"""Legacy Diamond migration without accepting client-side mint requests."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError
from game_server.promotions import import_historic_advanced_items
from game_server.service import GameService


class HistoricPromotionImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Path(self.tmp.name) / 'public.sqlite'
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['promotions'] = {'required_level': 1, 'maximum_base_rank': 7,
            'characters': {'scout': {'items': {}, 'advanced_items': {
                'diamond.scout.1': {'rank': 1}, 'diamond.scout.2': {'rank': 2}}}}}
        self.service = GameService(self.database, self.catalog)
        self.service.create_player({'player_id': 'owner', 'display_name': 'Owner'})

    def tearDown(self):
        self.tmp.cleanup()

    def test_historic_cumulative_import_is_monotone_restart_safe_and_does_not_regrant(self):
        before = self.service.get_player('owner')
        first = import_historic_advanced_items(self.service, 'owner',
            {'diamond.scout.1': 3, 'diamond.scout.2': 1,
             'unknown-historic-item': 98, 'diamond-invalid': -1})
        self.assertEqual(first['imported'], {'diamond.scout.1': 3, 'diamond.scout.2': 1})
        current = self.service.get_player('owner')
        self.assertEqual(current['inventory']['diamond.scout.1'], 3)
        self.assertEqual(current['inventory']['diamond.scout.2'], 1)
        self.assertNotIn('unknown-historic-item', current['inventory'])
        self.assertEqual(current['version'], before['version'] + 1)
        self.service = GameService(self.database, self.catalog)
        self.assertEqual(import_historic_advanced_items(self.service, 'owner',
            {'diamond.scout.1': 3, 'diamond.scout.2': 1})['imported'], {})
        self.assertEqual(self.service.get_player('owner'), current)
        self.assertEqual(import_historic_advanced_items(self.service, 'owner',
            {'diamond.scout.1': 2, 'diamond.scout.2': 4})['imported'], {'diamond.scout.2': 3})
        latest = self.service.get_player('owner')
        self.assertEqual(latest['inventory']['diamond.scout.1'], 3)
        self.assertEqual(latest['inventory']['diamond.scout.2'], 4)
        self.assertEqual(latest['historic_promotion_imports'],
                         {'diamond.scout.1': 3, 'diamond.scout.2': 4})
        with self.assertRaises(DomainError):
            self.service.execute('owner', {'command': 'promotion.import_legacy',
                                         'idempotency_key': 'forged',
                                         'parameters': {'balances': {'diamond.scout.1': 1000}}})
        self.assertEqual(self.service.get_player('owner'), latest)

    def test_invalid_and_cross_player_imports_cannot_corrupt_account(self):
        self.service.create_player({'player_id': 'other', 'display_name': 'Other'})
        before = self.service.get_player('owner')
        with self.assertRaises(DomainError):
            import_historic_advanced_items(self.service, 'owner', ['not-a-map'])
        with self.assertRaises(DomainError):
            import_historic_advanced_items(self.service, 'nonexistent',
                                           {'diamond.scout.1': 42})
        self.assertEqual(self.service.get_player('owner'), before)
        self.assertNotIn('historic_promotion_imports', self.service.get_player('other'))


if __name__ == '__main__':
    unittest.main()

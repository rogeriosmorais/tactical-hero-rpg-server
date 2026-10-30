"""Migrate original Supplies one-batch-per-UTC-day stock to public transactions."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


class DailySuppliesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'shop.sqlite'
        self.now = 15 * 86400 + 35
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['offers']['training-kit']['purchase_limit'] = 10
        self.catalog['offers']['training-kit']['reset_policy'] = 'utc_day_once'
        self.service = GameService(self.database, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def tearDown(self):
        self.temp.cleanup()

    def purchase(self, nonce, count):
        return self.service.execute('a', {'command': 'shop.purchase', 'idempotency_key': nonce,
                                          'parameters': {'offer_id': 'training-kit', 'quantity': count}})

    def test_single_batch_daily_reset_replay_restart_and_independent_player(self):
        start = self.service.get_player('a')
        first = self.purchase('original-shop-nonce', 2)
        self.assertEqual(first['player']['wallet']['credits'], start['wallet']['credits'] - 40)
        self.assertEqual(first['player']['inventory']['training-kit'], 2)
        self.assertEqual(first['player']['offer_daily_purchases']['training-kit'],
                         {'day': 15, 'count': 2})
        self.assertEqual(self.purchase('original-shop-nonce', 2), first)
        snapshot = self.service.get_player('a')
        for quantity in (1, 2, 10):
            with self.assertRaises(DomainError) as error:
                self.purchase('another-' + str(quantity), quantity)
            self.assertEqual(error.exception.code, 'purchase_limit')
        self.assertEqual(self.service.get_player('a'), snapshot)
        self.service.create_player({'player_id': 'b', 'display_name': 'B'})
        other = self.service.execute('b', {'command': 'shop.purchase', 'idempotency_key': 'b-batch',
            'parameters': {'offer_id': 'training-kit', 'quantity': 1}})
        self.assertEqual(other['player']['inventory']['training-kit'], 1)
        self.now += 86400
        self.service = GameService(self.database, self.catalog, clock=lambda: self.now)
        second = self.purchase('new-day', 1)
        self.assertEqual(second['player']['offer_daily_purchases']['training-kit'],
                         {'day': 16, 'count': 1})
        self.assertEqual(second['player']['inventory']['training-kit'], 3)
        self.assertEqual(self.purchase('original-shop-nonce', 2), first)
        self.assertEqual(self.service.get_player('a'), second['player'])

    def test_invalid_batch_or_insufficient_funds_is_atomic(self):
        before = self.service.get_player('a')
        for quantity in (0, -1, True, 11, 999):
            with self.assertRaises(DomainError):
                self.purchase('invalid-' + str(quantity), quantity)
        self.assertEqual(self.service.get_player('a'), before)
        self.purchase('spent', 5)
        self.now += 86400
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.purchase('insufficient', 1)
        self.assertEqual(self.service.get_player('a'), before)

    def test_old_lifetime_offer_behavior_unchanged_and_source_policy_closed(self):
        legacy = deepcopy(DEMO_CATALOG)
        self.assertEqual(validate_catalog(legacy), legacy)
        for value in ('local_day', 'weekly', False, None):
            catalog = deepcopy(self.catalog)
            catalog['offers']['training-kit']['reset_policy'] = value
            with self.assertRaises(DomainError):
                validate_catalog(catalog)


if __name__ == '__main__':
    unittest.main()

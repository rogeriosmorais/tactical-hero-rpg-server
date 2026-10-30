"""Crossed-level payouts share command rollback, replay and durable receipts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


class PlayerProgressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog.update(maximum_player_level=4, experience_thresholds=[0, 50, 130, 240],
            level_rewards={'policy_id': 'authored-v1', 'awards': {
                '2': {'wallet': {'credits': 7}}, '3': {'inventory': {'badge': 2}},
                '4': {'wallet': {'credits': 11}}}})
        self.catalog['offers']['training-kit']['rewards'] = {'experience': 150}
        self.catalog['offers']['training-kit']['purchase_limit'] = 10
        self.service = GameService(self.path, self.catalog, clock=lambda: 1000)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def command(self, name, key, **parameters):
        return self.service.execute('a', {'command': name, 'idempotency_key': key, 'parameters': parameters})

    def purchase(self, key='buy', quantity=1):
        return self.command('shop.purchase', key, offer_id='training-kit', quantity=quantity)

    def test_multilevel_crossing_rewards_once_concurrent_retry_and_restart(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.purchase(), range(4)))
        self.assertTrue(all(reply == replies[0] for reply in replies))
        result = replies[0]
        self.assertEqual(result['levels_gained'], [2, 3])
        self.assertEqual([row['level'] for row in result['level_rewards']], [2, 3])
        self.assertEqual(result['player']['wallet']['credits'], 87)
        self.assertEqual(result['player']['inventory']['badge'], 2)
        self.assertEqual(result['progression']['next_level_experience'], 240)
        restarted = GameService(self.path, self.catalog)
        self.assertEqual(restarted.get_progression('a'), {'progression': result['progression']})
        self.assertEqual(restarted.execute('a', {'command': 'shop.purchase', 'idempotency_key': 'buy',
                         'parameters': {'offer_id': 'training-kit', 'quantity': 1}}), result)
        self.assertEqual(restarted.create_player({'player_id': 'a', 'display_name': 'Changed'}), result['player'])

    def test_cap_grants_once_and_reads_do_not_backfill_or_change_player(self):
        first = self.purchase()
        second = self.purchase('buy-more', 2)
        self.assertEqual(second['levels_gained'], [4])
        self.assertEqual([row['level'] for row in second['level_rewards']], [4])
        self.assertIsNone(second['progression']['next_level_experience'])
        capped = self.purchase('at-cap')
        self.assertEqual(capped['levels_gained'], [])
        self.assertEqual(capped['level_rewards'], [])
        before = self.service.get_player('a')
        self.service.get_progression('a')
        self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(first['progression']['awarded_levels']['2'], {'at': 1000, 'policy_id': 'authored-v1'})

    def test_failed_award_rolls_back_xp_payment_and_receipts(self):
        before = self.service.get_player('a')
        real_reward = self.service._reward
        def failing(player, bundle, count=1):
            real_reward(player, bundle, count)
            if bundle.get('inventory', {}).get('badge'):
                raise RuntimeError('simulate later award failure')
        self.service._reward = failing
        with self.assertRaises(RuntimeError):
            self.purchase()
        self.assertEqual(self.service.get_player('a'), before)
        self.service._reward = real_reward
        self.assertEqual(self.purchase()['levels_gained'], [2, 3])

    def test_bad_curves_recursive_xp_rewards_and_client_claims_rejected(self):
        for curve in ([0, 50, 50, 100], [0, 50], [False, 50, 100, 150]):
            catalog = deepcopy(self.catalog)
            catalog['experience_thresholds'] = curve
            with self.assertRaises(DomainError):
                validate_catalog(catalog)
        catalog = deepcopy(self.catalog)
        catalog['level_rewards']['awards']['2']['experience'] = 1
        with self.assertRaises(DomainError):
            validate_catalog(catalog)
        with self.assertRaises(DomainError):
            self.command('player.claim_level_reward', 'forge', level=4)

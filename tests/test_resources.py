"""Unlimited resources preserve nominal activity and rewards, with actual balances."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.level_award_policy import estimated_policy
from game_server.service import GameService


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['resource_rules'] = {'energy': {'mode': 'unlimited', 'display_balance': 999,
            'capacity': 70, 'level_capacities': {'2': 80}}, 'credits': {'mode': 'finite', 'capacity': 10}}
        self.catalog['offers']['training-kit'].update(cost={'energy': 1000, 'credits': 20}, purchase_limit=10,
            rewards={'wallet': {'energy': 70, 'credits': 5}, 'experience': 100})
        self.catalog['objectives'] = {'entries': {}, 'counters': {'used': {'source': {'kind': 'activity',
            'rules': [{'command': 'shop.purchase', 'measure': 'wallet_spent', 'currency_id': 'energy'}]}}}}
        self.service = GameService(self.path, self.catalog)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def purchase(self, key, quantity=1):
        return self.service.execute('a', {'command': 'shop.purchase', 'idempotency_key': key,
            'parameters': {'offer_id': 'training-kit', 'quantity': quantity}})

    def test_unlimited_spending_nominal_rewards_capacity_and_restart(self):
        before = self.service.get_player('a')
        self.assertEqual(before['wallet']['energy'], 999)
        self.assertEqual(self.service.get_resources('a')['resources']['energy']['capacity'], 70)
        self.assertEqual(self.service.get_player('a'), before)
        result = self.purchase('buy', 2)
        self.assertEqual(result['player']['wallet']['energy'], 999)
        self.assertEqual(result['rewards']['wallet']['energy'], 140)
        self.assertEqual(result['objective_state']['counters']['used']['value'], 2000)
        self.assertEqual(result['resources']['energy']['capacity'], 80)
        self.assertEqual(result['player']['wallet']['credits'], 70)
        self.assertEqual([row['currency_id'] for row in result['wallet_changes']], ['credits'])
        self.assertEqual(self.purchase('buy', 2), result)
        restarted = GameService(self.path, self.catalog)
        self.assertEqual(restarted.get_resources('a'), self.service.get_resources('a'))

    def test_finite_currency_failure_rolls_back_nominal_activity_and_xp(self):
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.purchase('expensive', 10)
        self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(self.service.get_objectives('a')['counters']['used']['value'], 0)

    def test_invalid_resource_modes_and_decreasing_capacity_are_rejected(self):
        for rule in ({'mode': 'unlimited'}, {'mode': 'finite', 'display_balance': 5}, {'mode': []},
                     {'mode': 'finite', 'capacity': 80, 'level_capacities': {'2': 70}}):
            catalog = deepcopy(self.catalog)
            catalog['resource_rules']['energy'] = rule
            with self.assertRaises(DomainError):
                validate_catalog(catalog)

    def test_estimated_policy_is_explicit_neutral_configuration_and_keeps_special_award(self):
        currencies = {key: key for key in ('credits', 'premium', 'energy', 'shop', 'charges', 'donations')}
        policy = estimated_policy(currencies, 'veteran-fragments')
        self.assertEqual(policy['policy_id'], 'estimated-level-awards-v1')
        self.assertEqual(policy['awards']['21'], {'wallet': {'energy': 70, 'premium': 20, 'shop': 9000,
            'charges': 200, 'donations': 50}, 'inventory': {'veteran-fragments': 2000}})
        self.assertEqual(set(policy['awards']), {str(level) for level in range(4, 111)})
        catalog = deepcopy(self.catalog)
        catalog.update(maximum_player_level=110, level_rewards=policy)
        validate_catalog(catalog)

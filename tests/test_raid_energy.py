"""Raid refills retain source prices, whole-price conversion and daily history."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import unittest

from game_server.catalog import DomainError, validate_catalog
from game_server.service import GameService
import test_raids as fixtures


class RaidEnergyTests(unittest.TestCase):
    mutate_fixture = fixtures.RaidTests.mutate_fixture

    def setUp(self):
        fixtures.RaidTests.setUp(self)
        self.catalog['starting_wallet'].update(stamina=20, tickets=0, premium=2000)
        self.catalog['resource_rules'] = {'stamina': {'mode': 'finite', 'capacity': 60}}
        self.catalog['raid_energy'] = {'required_level': 1, 'currency_id': 'stamina',
            'payment_currency_id': 'tickets', 'prices': [50, 50, 100], 'quantities': [10],
            'conversion': {'currency_id': 'premium', 'rate': 1.0}}
        self.path = self.path.with_name('refills.sqlite')
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def buy(self, key='refill', cost=50, **extra):
        return self.service.execute('a', {'command': 'raid.refill', 'idempotency_key': key,
            'parameters': {'expected_cost': cost, **extra}})

    def test_concurrent_retry_day_history_and_restart_keep_saved_balances(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.buy(), range(3)))
        self.assertTrue(all(row == results[0] for row in results))
        self.assertEqual(results[0]['player']['wallet']['stamina'], 30)
        self.assertEqual(results[0]['player']['wallet']['premium'], 1950)
        self.buy('second')
        self.buy('third', 100)
        before = self.service.get_player('a')
        with self.assertRaisesRegex(DomainError, 'exhausted'):
            self.buy('exhausted', 100)
        self.assertEqual(self.service.get_player('a'), before)
        day = str(self.now // 86400)
        self.now += 86400
        self.assertEqual(self.service.get_raid_energy('a')['count'], 0)
        result = self.buy('next-day')
        self.assertEqual(result['player']['raid_energy_refills'][day], 3)
        self.assertEqual(result['raid_energy']['count'], 1)
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.buy(), results[0])

    def test_primary_funds_or_whole_converted_price_never_spend_a_partial_balance(self):
        self.mutate_fixture(lambda p: p['wallet'].update(tickets=50))
        primary = self.buy()
        self.assertEqual(primary['payment'], {'currency_id': 'tickets', 'quantity': 50})
        self.mutate_fixture(lambda p: p['wallet'].update(tickets=49))
        converted = self.buy('converted')
        self.assertEqual(converted['player']['wallet']['tickets'], 49)
        self.assertEqual(converted['payment'], {'currency_id': 'premium', 'quantity': 50})

    def test_failed_access_prices_full_energy_and_funds_are_atomic(self):
        for change in (lambda p: p['wallet'].update(premium=49),
                       lambda p: p['wallet'].update(stamina=60),
                       lambda p: p.update(alliance_id=None)):
            original = self.service.get_player('a')
            self.mutate_fixture(change)
            before = self.service.get_player('a')
            with self.assertRaises(DomainError):
                self.buy()
            self.assertEqual(self.service.get_player('a'), before)
            self.mutate_fixture(lambda p: p.update(original))
        for extra in ({'cost': 49}, {'cost': True}, {'quantity': 500}, {'conversion': 0}):
            before = self.service.get_player('a')
            with self.assertRaises(DomainError):
                self.buy(**extra)
            self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(self.buy()['raid_energy']['count'], 1)

    def test_soft_capacity_does_not_truncate_a_valid_refill(self):
        self.mutate_fixture(lambda p: p['wallet'].update(stamina=59))
        result = self.buy()
        self.assertEqual(result['player']['wallet']['stamina'], 69)
        with self.assertRaises(DomainError):
            self.buy('full')

    def test_catalog_rejects_invalid_schedules_conversion_and_unlimited_energy(self):
        for change in (lambda c: c['raid_energy'].update(prices=[]),
                       lambda c: c['raid_energy'].update(quantities=[0]),
                       lambda c: c['raid_energy']['conversion'].update(rate=float('nan')),
                       lambda c: c['resource_rules']['stamina'].update(mode='unlimited', display_balance=999)):
            catalog = deepcopy(self.catalog)
            change(catalog)
            with self.assertRaises(DomainError):
                validate_catalog(catalog)

    def test_conversion_uses_ceil_and_disabled_conversion_cannot_spend_premium(self):
        for name, conversion, expected in [('rounded', {'currency_id': 'premium', 'rate': 1.01}, 1949),
                                            ('disabled', None, None)]:
            catalog = deepcopy(self.catalog)
            catalog['raid_energy']['conversion'] = conversion
            service = GameService(self.path.with_name(name + '.sqlite'), catalog)
            service.create_player({'player_id': 'a', 'display_name': 'A'})
            request = {'command': 'raid.refill', 'idempotency_key': 'r', 'parameters': {'expected_cost': 50}}
            if expected is None:
                before = service.get_player('a')
                with self.assertRaises(DomainError):
                    service.execute('a', request)
                self.assertEqual(service.get_player('a'), before)
            else:
                self.assertEqual(service.execute('a', request)['player']['wallet']['premium'], expected)

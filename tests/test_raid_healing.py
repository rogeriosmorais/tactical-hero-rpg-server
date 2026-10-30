"""Recovery payment, batch atomicity and historical replay on the raid ledger."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import unittest

from game_server.catalog import DomainError, validate_catalog
from game_server.service import GameService
import test_raid_combat as combat
import test_raids as fixtures


def healing_catalog():
    catalog = combat.combat_catalog()
    catalog['starting_wallet'].update(medical=15, premium=1000)
    catalog['raids']['expedition']['healing'] = {'limit': 5,
        'heal': {'currency_id': 'medical', 'prices': [10, 20],
                 'conversion': {'currency_id': 'premium', 'rate': 1.25}},
        'revive': {'currency_id': 'medical', 'prices': [30], 'conversion': None}}
    return catalog


class RaidHealingTests(unittest.TestCase):
    command = fixtures.RaidTests.command
    mutate_fixture = fixtures.RaidTests.mutate_fixture
    begin = combat.RaidCombatTests.begin
    finish = combat.RaidCombatTests.finish

    def setUp(self):
        fixtures.RaidTests.setUp(self)
        combat.RaidCombatTests.configure(self, healing_catalog())
        self.finish(self.begin()['battle'], victory=False, health=40, enemies={'enemy-one': 100, 'enemy-two': 200})
        self.parameters = {key: value for key, value in self.context.items() if key != 'team_index'}
        self.parameters.update(characters=[{'character_id': 'scout', 'revive': False, 'maximum_health': 90,
                                           'expected_cost': 10}], request_fingerprint='a' * 64, projection_fingerprint='c' * 64)

    def heal(self, key='heal', **changes):
        return self.command('raid.heal', key, **{**deepcopy(self.parameters), **changes})

    def test_atomic_concurrent_payment_energy_base_hp_and_receipt_after_expiry_restart(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(lambda _: self.heal(), range(3)))
        self.assertTrue(all(row == rows[0] for row in rows))
        result = rows[0]
        self.assertEqual(result['player']['wallet']['medical'], 5)
        self.assertEqual(result['characters']['scout'], {'health': 90, 'maximum_health': 90, 'energy': [0, 2, 3]})
        self.assertEqual(result['raid']['heal_counts']['scout'], {'heal': 1})
        self.assertEqual(result['raid']['heal_count'], 1)
        self.now += 100000
        self.mutate_fixture(lambda p: p.update(alliance_id=None))
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.heal(), result)
        lookup = {'idempotency_key': 'heal', 'command': 'raid.heal', 'request_fingerprint': 'a' * 64}
        self.assertEqual(self.service.lookup_receipt('a', lookup), {'status': 'match', 'result': result})
        self.assertEqual(self.service.lookup_receipt('a', {**lookup, 'request_fingerprint': 'b' * 64}), {'status': 'conflict'})
        self.assertEqual(self.service.lookup_receipt('a', {**lookup, 'idempotency_key': 'missing'}), {'status': 'missing'})
        self.assertEqual(self.service.lookup_receipt('a', {**lookup, 'idempotency_key': 'launch'}), {'status': 'conflict'})
        self.service.create_player({'player_id': 'other', 'display_name': 'Other'})
        self.assertEqual(self.service.lookup_receipt('other', lookup), {'status': 'missing'})

    def test_escalating_full_conversion_preserves_primary_and_repeat_last(self):
        self.heal()
        def injury(p):
            p['raids'][self.context['instance_id']]['character_states']['scout']['health'] = 20
        self.mutate_fixture(injury)
        self.parameters['characters'][0]['expected_cost'] = 20
        result = self.heal('second')
        self.assertEqual(result['player']['wallet']['medical'], 5)
        self.assertEqual(result['player']['wallet']['premium'], 975)
        self.mutate_fixture(injury)
        self.assertEqual(self.heal('third')['player']['wallet']['premium'], 950)

    def test_bad_second_hero_rolls_back_first_spend_and_counter(self):
        before = self.service.get_player('a')
        first = self.parameters['characters'][0]
        for second in (deepcopy(first), {'character_id': 'guardian', 'revive': True, 'maximum_health': 100},
                       {'character_id': 'missing', 'revive': False, 'maximum_health': 100}):
            with self.assertRaises(DomainError):
                self.heal(characters=[first, second])
            self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(self.heal()['raid']['heal_count'], 1)

    def test_revive_counters_limits_and_active_battle_do_not_change_state(self):
        self.command('raid.lock', 'relock', **self.context)
        battle = self.begin('new')['battle']
        before = self.service.get_player('a')
        with self.assertRaises(DomainError) as caught:
            self.heal()
        self.assertEqual(caught.exception.code, 'battle_already_active')
        self.assertEqual(self.service.get_player('a'), before)
        self.finish(battle, 'dead', victory=False, health=0, enemies={'enemy-one': 100, 'enemy-two': 200})
        def funds(p):
            p['wallet']['medical'] = 100
        self.mutate_fixture(funds)
        with self.assertRaises(DomainError):
            self.heal()
        self.parameters['characters'][0].update(revive=True, expected_cost=30)
        result = self.heal()
        self.assertEqual(result['raid']['heal_counts']['scout'], {'revive': 1})
        def exhausted(p):
            raid = p['raids'][self.context['instance_id']]
            raid['heal_count'] = 5
            raid['character_states']['scout']['health'] = 0
        self.mutate_fixture(exhausted)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError) as caught:
            self.heal('over-limit')
        self.assertEqual(caught.exception.code, 'raid_heal_limit')
        self.assertEqual(self.service.get_player('a'), before)

    def test_malformed_prices_rates_and_lookup_identity_reject(self):
        for mutation in (lambda p: p.update(limit=True), lambda p: p['heal'].update(prices=[]),
                         lambda p: p['heal']['conversion'].update(rate=float('inf')),
                         lambda p: p['revive'].update(prices=[False])):
            catalog = healing_catalog()
            mutation(catalog['raids']['expedition']['healing'])
            with self.assertRaises(DomainError):
                validate_catalog(catalog)
        for request in ({'command': 'raid.heal'}, {'request_fingerprint': 'a' * 64},
                        {'command': 'loot.open', 'request_fingerprint': 'a' * 64},
                        {'command': 'raid.heal', 'request_fingerprint': 'a' * 64, 'request_hash': 'b' * 64}):
            with self.assertRaises(DomainError):
                self.service.lookup_receipt('a', {'idempotency_key': 'heal', **request})

    def test_legacy_unknown_energy_default_is_saved_with_the_successful_receipt(self):
        def unknown_energy(p):
            del p['raids'][self.context['instance_id']]['character_states']['scout']['energy']
        self.mutate_fixture(unknown_energy)
        result = self.heal()
        self.assertEqual(result['characters']['scout']['energy'], [0, -1, -1])
        self.assertEqual(self.heal(), result)


if __name__ == '__main__':
    unittest.main()

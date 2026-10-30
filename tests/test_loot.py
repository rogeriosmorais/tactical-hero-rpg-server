"""Weighted draw semantics and durable raid reward receipts."""
from copy import deepcopy
import unittest

from game_server.catalog import DomainError, validate_catalog
from game_server.loot import roll, validate
import test_raid_combat as combat_fixtures


def tables():
    return {'prize': {'groups': [[
        {'weight': 3, 'multiplier': 2, 'items': [
            {'weight': 1, 'minimum': 1, 'maximum': 3, 'multiplier': 1, 'rewards': {'wallet': {'credits': 1}}},
            {'weight': 2, 'minimum': 0, 'maximum': 0, 'multiplier': 0, 'rewards': None}]},
        {'weight': 1, 'multiplier': 1, 'items': [
            {'weight': 1, 'minimum': 2, 'maximum': 4, 'multiplier': 3, 'rewards': {'inventory': {'badge': 1}}}]}],
        [{'weight': 1, 'multiplier': 1, 'items': [
            {'weight': 1, 'minimum': 1, 'maximum': 2, 'multiplier': 1, 'rewards': {'experience': 1}}]}]]}}


class LootTests(unittest.TestCase):
    def test_one_of_each_large_authored_source_box_with_bounded_group_limit(self):
        selection = {'weight': 1, 'multiplier': 1, 'items': [
            {'weight': 1, 'minimum': 1, 'maximum': 1, 'multiplier': 1,
             'rewards': {'inventory': {'source-item': 1}}}]}
        allowed = {'source-box': {'groups': [[deepcopy(selection)] for _ in range(178)]}}
        validate(allowed)
        awarded = roll(allowed, 'source-box', 'same-original-encounter')
        self.assertEqual(awarded['rewards']['inventory'], {'source-item': 178})
        self.assertEqual(len(awarded['selections']), 178)
        oversized = {'source-box': {'groups': [[deepcopy(selection)] for _ in range(257)]}}
        with self.assertRaises(DomainError):
            validate(oversized)

    def test_preserved_seed_goldens_include_empty_groups_and_two_stage_multipliers(self):
        # Generated from the retained LootRules.roll before implementing this schema.
        expected = [(12, 1), (9, 2), (6, 1), (9, 2), (0, 2), (0, 1), (0, 1), (9, 1), (0, 1), (6, 1)]
        definitions = tables()
        validate(definitions)
        for index, (badge, xp) in enumerate(expected):
            result = roll(definitions, 'prize', 'seed-' + str(index))
            self.assertEqual(result['rewards']['inventory'].get('badge', 0), badge)
            self.assertEqual(result['rewards']['experience'], xp)
            self.assertEqual(len(result['selections']), 2)
        self.assertEqual(definitions, tables())

    def test_inclusive_quantities_and_multipliers_and_empty_table(self):
        definitions = {'range': {'groups': [[{'weight': 1, 'multiplier': 2, 'items': [
            {'weight': 1, 'minimum': 2, 'maximum': 4, 'multiplier': 3, 'rewards': {'wallet': {'credits': 1}}}]}]]},
            'empty': {'groups': []}}
        validate(definitions)
        values = {roll(definitions, 'range', str(seed))['rewards']['wallet']['credits'] for seed in range(100)}
        self.assertEqual(values, {12, 18, 24})
        self.assertEqual(roll(definitions, 'empty', 'seed')['rewards'], {'wallet': {}, 'inventory': {}, 'experience': 0})

    def test_invalid_weights_ranges_and_impossible_overflow_rejected(self):
        for mutate in (lambda d: d['groups'][0][0].update(weight=-1),
                       lambda d: d['groups'][0][0]['items'][0].update(minimum=4),
                       lambda d: d['groups'][0][0]['items'][1].update(multiplier=1),
                       lambda d: d['groups'][0][0].update(multiplier=9_000_000_000_000_000),
                       lambda d: d['groups'][0].clear()):
            definitions = tables()
            mutate(definitions['prize'])
            with self.assertRaises(DomainError):
                validate(definitions)


class RaidLootTests(unittest.TestCase):
    command = combat_fixtures.RaidCombatTests.command
    mutate_fixture = combat_fixtures.RaidCombatTests.mutate_fixture
    configure = combat_fixtures.RaidCombatTests.configure
    begin = combat_fixtures.RaidCombatTests.begin
    finish = combat_fixtures.RaidCombatTests.finish

    def setUp(self):
        combat_fixtures.fixtures.RaidTests.setUp(self)
        catalog = combat_fixtures.combat_catalog()
        catalog['loot_tables'] = tables()
        catalog['loot_tables']['prize']['groups'][1][0]['items'][0]['counters'] = {'local-contribution': 1}
        catalog['raids']['expedition']['combat']['nodes']['guard'][0]['reward_tables'] = ['prize', 'prize']
        catalog['raids']['expedition']['combat']['nodes']['guard'][0]['contribution_counter_id'] = 'local-contribution'
        self.configure(catalog)

    def test_victory_rolls_are_saved_once_and_reproducible_from_issued_battle(self):
        battle = self.begin()['battle']
        result = self.finish(battle)
        expected = roll(self.catalog['loot_tables'], 'prize', battle['battle_id'] + ':prize')
        self.assertEqual(result['loot_rolls'], [expected, expected])
        self.assertEqual(result['raid']['contribution_points'], 50 + 2 * expected['counters']['local-contribution'])
        self.assertEqual(self.finish(battle), result)
        self.assertEqual(result['rewards']['inventory']['badge'], 2 + 2 * expected['rewards']['inventory'].get('badge', 0))

    def test_defeat_does_not_roll_and_missing_table_cannot_start_service(self):
        battle = self.begin()['battle']
        result = self.finish(battle, victory=False, enemies={'enemy-one': 50, 'enemy-two': 200})
        self.assertEqual(result['loot_rolls'], [])
        broken = deepcopy(self.catalog)
        broken['loot_tables'].clear()
        with self.assertRaises(DomainError):
            validate_catalog(broken)
        broken = deepcopy(self.catalog)
        del broken['raids']['expedition']['combat']['nodes']['guard'][0]['contribution_counter_id']
        with self.assertRaises(DomainError):
            validate_catalog(broken)

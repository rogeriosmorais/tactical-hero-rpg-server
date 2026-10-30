"""Material development, recursive crafting and bulk rollback on authored data."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


def cost(credits=0, **items):
    return {'wallet': {'credits': credits}, 'inventory': items}


def authored_catalog():
    catalog = deepcopy(DEMO_CATALOG)
    catalog['starting_wallet']['credits'] = 1000
    catalog['starting_characters'] = {'scout': 1}
    for character in catalog['characters'].values():
        character['maximum_level'] = 4
    catalog['missions']['training.1']['rewards'] = {'experience': 300}
    catalog['offers']['kit'] = {'cost': {'credits': 10}, 'purchase_limit': 10,
        'rewards': {'inventory': {'module': 10, 'part': 20, 'shard': 30, 'ability-token': 10}}}
    definition = {'experience_thresholds': [0, 10, 30, 60], 'recruitment': cost(5, shard=5),
        'initial_rank': 1, 'rank_upgrades': [cost(7, shard=3), cost(9, shard=5)],
        'initial_abilities': [1, 0, None, None],
        'abilities': [[{'required_level': 1, 'required_tier': 1, 'payment': cost()},
                       {'required_level': 2, 'required_tier': 1, 'payment': cost(2, **{'ability-token': 1})}],
                      [{'required_level': 2, 'required_tier': 2, 'payment': cost(3, **{'ability-token': 2})}], [], []],
        'tiers': [{'a': {'item_id': 'head', 'required_level': 2}, 'b': {'item_id': 'body', 'required_level': 1}},
                  {'a': {'item_id': 'next', 'required_level': 4}}, {}]}
    catalog['development'] = {'characters': {key: deepcopy(definition) for key in catalog['characters']},
        'training_items': {'module': {'experience': 10, 'cost': {'credits': 2}}},
        'recipes': {'body': {'ingredients': {'part': 2}, 'cost': {'credits': 1}},
                    'head': {'ingredients': {'body': 1, 'part': 1}, 'cost': {'credits': 1}},
                    'next': {'ingredients': {'part': 2}, 'cost': {'credits': 1}}}}
    return catalog


class DevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.catalog = authored_catalog()
        self.service = GameService(self.path, self.catalog)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def command(self, name, key, **parameters):
        return self.service.execute('a', {'command': name, 'idempotency_key': key, 'parameters': parameters})

    def earn(self):
        battle = self.command('mission.begin', 'begin', mission_id='training.1', team=['scout'])['battle']
        self.command('mission.complete', 'win', mission_id='training.1', battle_id=battle['battle_id'], outcome='victory', stars=3)
        self.command('shop.purchase', 'kit', offer_id='kit', quantity=1)

    def train(self, quantity=3):
        return self.command('character.train_items', 'train', character_id='scout', expected_level=1, items={'module': quantity})

    def test_training_uses_materials_and_account_cap_and_replays_after_restart(self):
        self.command('shop.purchase', 'early-kit', offer_id='kit', quantity=1)
        before = self.service.get_player('a')
        with self.assertRaisesRegex(DomainError, 'account level'):
            self.train()
        with self.assertRaisesRegex(DomainError, 'training materials'):
            self.command('character.train', 'bypass', character_id='scout', levels=1)
        self.assertEqual(self.service.get_player('a'), before)
        self.earn()
        before = self.service.get_player('a')
        result = self.train()
        character = result['character']
        self.assertEqual((character['level'], character['experience'], character['power']), (3, 30, 36))
        self.assertEqual(result['player']['inventory']['module'], before['inventory']['module'] - 3)
        self.assertEqual(result['player']['wallet']['credits'], before['wallet']['credits'] - 6)
        restarted = GameService(self.path, self.catalog)
        self.assertEqual(restarted.execute('a', {'command': 'character.train_items', 'idempotency_key': 'train',
            'parameters': {'character_id': 'scout', 'expected_level': 1, 'items': {'module': 3}}}), result)
        after = restarted.get_player('a')
        for items in ({'module': True}, {'module': 0}, {'module': -1}, {'module': 9999}, {'undefined': 1}):
            with self.subTest(items=items), self.assertRaises(DomainError):
                self.command('character.train_items', 'bad', character_id='scout', expected_level=3, items=items)
            self.assertEqual(self.service.get_player('a'), after)

    def test_recruitment_incremental_rank_cost_and_ability_gates(self):
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('character.recruit', 'recruit', character_id='guardian')
        self.assertEqual(self.service.get_player('a'), before)
        self.earn()
        recruited = self.command('character.recruit', 'recruit', character_id='guardian')
        self.assertEqual(recruited['character']['abilities'], [1, 0, None, None])
        self.assertEqual(recruited['player']['inventory']['shard'], 25)
        self.assertEqual(self.command('character.recruit', 'recruit', character_id='guardian'), recruited)
        self.command('character.rank_up', 'rank-2', character_id='guardian')
        promoted = self.command('character.rank_up', 'rank-3', character_id='guardian')
        self.assertEqual(promoted['character']['rank'], 3)
        self.assertEqual(promoted['player']['inventory']['shard'], 17)
        with self.assertRaises(DomainError):
            self.command('character.rank_up', 'rank-4', character_id='guardian')
        self.train()
        upgraded = self.command('character.ability_upgrade', 'ability', character_id='scout', ability_index=0, unlock=False)
        self.assertEqual(upgraded['character']['abilities'][0], 2)
        before = self.service.get_player('a')
        with self.assertRaisesRegex(DomainError, 'requirements'):
            self.command('character.ability_upgrade', 'unlock', character_id='scout', ability_index=1, unlock=True)
        self.assertEqual(self.service.get_player('a'), before)

    def test_allowed_ability_targets_are_guards_and_exact_retries_ignore_later_state(self):
        self.earn()
        self.train()
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('character.ability_upgrade', 'ability', character_id='scout', ability_index=0,
                         unlock=False, allowed_levels=[1])
        self.assertEqual(self.service.get_player('a'), before)
        first = self.command('character.ability_upgrade', 'ability', character_id='scout', ability_index=0,
                             unlock=False, allowed_levels=[2])
        self.assertEqual(self.command('character.ability_upgrade', 'ability', character_id='scout', ability_index=0,
                                     unlock=False, allowed_levels=[2]), first)

    def test_recursive_crafting_equipment_promotion_and_next_tier_level_gate(self):
        self.earn()
        self.train()
        first = self.command('equipment.equip', 'equip', character_id='scout', craft_missing=True,
            slots=[{'slot_id': 'a', 'item_id': 'head'}, {'slot_id': 'b', 'item_id': 'body'}])
        self.assertEqual(first['player']['inventory']['part'], 15)
        self.assertEqual(first['character']['equipment'], {'a': 'head', 'b': 'body'})
        self.assertEqual(self.command('equipment.equip', 'equip', character_id='scout', craft_missing=True,
            slots=[{'slot_id': 'a', 'item_id': 'head'}, {'slot_id': 'b', 'item_id': 'body'}]), first)
        promoted = self.command('equipment.tier_up', 'tier', character_id='scout', target_tier=2)
        self.assertEqual(promoted['character']['equipment'], {})
        self.assertEqual(promoted['character']['tier'], 2)
        before = self.service.get_player('a')
        with self.assertRaisesRegex(DomainError, 'level'):
            self.command('equipment.equip', 'next', character_id='scout', craft_missing=True,
                         slots=[{'slot_id': 'a', 'item_id': 'next'}])
        self.assertEqual(self.service.get_player('a'), before)
        unlocked = self.command('character.ability_upgrade', 'unlock', character_id='scout', ability_index=1, unlock=True)
        self.assertEqual(unlocked['character']['abilities'][1], 1)

    def test_bulk_tier_plan_is_exact_and_failed_later_tier_rolls_back_earlier_spend(self):
        self.earn()
        self.train()
        plan = [{'tier': 1, 'slot_id': 'a', 'item_id': 'head'}, {'tier': 1, 'slot_id': 'b', 'item_id': 'body'},
                {'tier': 2, 'slot_id': 'a', 'item_id': 'next'}]
        before = self.service.get_player('a')
        for supplied in (plan[:-1], plan + plan[:1], plan):
            with self.subTest(supplied=supplied), self.assertRaises(DomainError):
                self.command('equipment.bulk_tier_up', 'bulk', character_id='scout', target_tier=3, plan=supplied)
            self.assertEqual(self.service.get_player('a'), before)
        self.command('character.train_items', 'train-more', character_id='scout', expected_level=3, items={'module': 3})
        result = self.command('equipment.bulk_tier_up', 'bulk', character_id='scout', target_tier=3, plan=plan)
        self.assertEqual(result['character']['tier'], 3)
        self.assertEqual(result['character']['equipment'], {})
        self.assertEqual(result['player']['inventory']['part'], 13)
        self.assertEqual(self.command('equipment.bulk_tier_up', 'bulk', character_id='scout', target_tier=3, plan=plan), result)

    def test_recipe_cycle_and_invalid_character_definitions_are_rejected(self):
        catalog = deepcopy(self.catalog)
        catalog['development']['recipes']['part'] = {'ingredients': {'head': 1}, 'cost': {}}
        with self.assertRaisesRegex(DomainError, 'acyclic'):
            validate_catalog(catalog)
        for key, value in (('experience_thresholds', [0, 10, 10, 30]), ('initial_rank', True),
                           ('initial_abilities', [1, 0, 0, None])):
            catalog = deepcopy(self.catalog)
            catalog['development']['characters']['scout'][key] = value
            with self.subTest(key=key), self.assertRaises(DomainError):
                validate_catalog(catalog)

    def test_shared_recipe_dependencies_use_existing_stock_and_bounded_expansion(self):
        catalog = deepcopy(self.catalog)
        recipes = catalog['development']['recipes']
        recipes.update({'joined': {'ingredients': {'head': 1, 'body': 1}, 'cost': {'credits': 2}}})
        service = GameService(Path(self.temp.name) / 'shared.sqlite', catalog)
        service.create_player({'player_id': 'a', 'display_name': 'A'})
        def command(name, key, **parameters):
            return service.execute('a', {'command': name, 'idempotency_key': key, 'parameters': parameters})
        command('shop.purchase', 'kit', offer_id='kit', quantity=1)
        command('equipment.craft', 'body', item_id='body', quantity=1)
        first = command('equipment.craft', 'joined', item_id='joined', quantity=1)
        self.assertEqual(first['player']['inventory']['part'], 15)
        self.assertEqual(first['player']['inventory']['body'], 0)
        self.assertEqual(first['player']['inventory']['head'], 0)
        self.assertEqual(first['player']['inventory']['joined'], 1)
        self.assertEqual(first['player']['wallet']['credits'], 985)

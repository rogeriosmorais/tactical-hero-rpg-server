"""Authored enhancement rules retain paid history and refund equipped stock once."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DomainError, validate_catalog
from game_server.service import GameService
from test_development import authored_catalog


def extension_catalog():
    catalog = authored_catalog()
    items = {}
    for slot in ('left', 'right'):
        for quality in (1, 2, 3):
            item = {'role': 'alpha', 'slot_id': slot, 'quality': quality, 'tier': 2 if quality == 3 else 1}
            if quality == 2:
                item['fusion'] = {'source': slot + '-1', 'quantity': 2, 'cost': {'credits': 3}}
            items[slot + '-' + str(quality)] = item
    items['other-role'] = {'role': 'beta', 'slot_id': 'left', 'quality': 1, 'tier': 1}
    classes = {}
    for name, prices in (('guard', [4, 5, 6]), ('healer', [1, 2, 3])):
        skills = []
        for level, price in enumerate(prices, 1):
            row = {'skill_id': name + str(level), 'required_quality': level, 'cost': {'credits': price}}
            if level > 1:
                row['prerequisite'] = name + str(level - 1)
            skills.append(row)
        classes[name] = {'skills': skills}
    catalog['enhancements'] = {'required_level': 2, 'required_item': 'augmentation-license', 'swap_seconds': 20, 'slots': ['left', 'right'],
        'items': items, 'characters': {'scout': 'alpha'}, 'classes': classes,
        'tiers': [{'required_level': 1, 'minimum_quality': 0, 'cost': {}},
                  {'required_level': 4, 'minimum_quality': 2, 'cost': {'credits': 10}}]}
    catalog['promotions'] = {'required_level': 2, 'maximum_base_rank': 3, 'characters': {'scout': {
        'items': {'low': {'rank': 1, 'recycle_rewards': {'wallet': {'credits': 2}}},
                  'middle': {'rank': 2, 'recycle_rewards': {'wallet': {'credits': 3}}},
                  'high': {'rank': 3, 'recycle_rewards': {'inventory': {'recycle-token': 5}}}},
        'advanced_items': {'advanced': {'rank': 2}}}}}
    catalog['offers']['kit']['rewards']['inventory'].update({
        'left-1': 20, 'right-1': 20, 'left-3': 1, 'right-3': 1, 'other-role': 1,
        'low': 2, 'middle': 1, 'high': 3, 'advanced': 1})
    catalog['missions']['training.1']['rewards']['inventory'] = {'augmentation-license': 1}
    return catalog


class ExtensionFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.catalog = extension_catalog()
        self.now = 1000
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def command(self, name, key, **parameters):
        return self.service.execute('a', {'command': name, 'idempotency_key': key,
                                         'parameters': {'character_id': 'scout', **parameters}})

    def earn(self):
        battle = self.service.execute('a', {'command': 'mission.begin', 'idempotency_key': 'begin',
            'parameters': {'mission_id': 'training.1', 'team': ['scout']}})['battle']
        self.service.execute('a', {'command': 'mission.complete', 'idempotency_key': 'win',
            'parameters': {'battle_id': battle['battle_id'], 'mission_id': 'training.1', 'outcome': 'victory', 'stars': 3}})
        self.kit('kit')

    def kit(self, key):
        return self.service.execute('a', {'command': 'shop.purchase', 'idempotency_key': key,
                                         'parameters': {'offer_id': 'kit', 'quantity': 1}})


class EnhancementTests(ExtensionFixture):
    def test_level_alone_does_not_grant_access_and_views_do_not_mutate(self):
        catalog = deepcopy(self.catalog)
        catalog['missions']['training.1']['rewards'].pop('inventory')
        self.service = GameService(Path(self.temp.name) / 'no-license.sqlite', catalog)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.earn()
        before = self.service.get_player('a')
        view = self.service.get_augmentations('a')
        self.assertFalse(view['enhancements_available'])
        self.assertTrue(view['promotions_available'])
        with self.assertRaises(DomainError):
            self.command('enhancement.select_class', 'no-license', class_id='guard')
        self.assertEqual(self.service.get_player('a'), before)

    def test_ownership_role_refund_same_item_and_fusion_recipe(self):
        before = self.service.get_player('a')
        with self.assertRaisesRegex(DomainError, 'account level'):
            self.command('enhancement.equip', 'equip', item_id='left-1')
        self.assertEqual(self.service.get_player('a'), before)
        self.earn()
        first = self.command('enhancement.equip', 'equip', item_id='left-1')
        before = self.service.get_player('a')
        for item in ('other-role', 'undefined', 'left-3'):
            with self.subTest(item=item), self.assertRaises(DomainError):
                self.command('enhancement.equip', 'bad', item_id=item)
            self.assertEqual(self.service.get_player('a'), before)
        same = self.command('enhancement.equip', 'same', item_id='left-1')
        self.assertEqual(same['inventory_changes'], [])
        with self.assertRaises(DomainError):
            self.command('enhancement.fuse', 'fuse', item_id='left-2', sources=['right-1', 'right-1'])
        self.command('enhancement.fuse', 'fuse', item_id='left-2', sources=['left-1', 'left-1'])
        swapped = self.command('enhancement.equip', 'swap', item_id='left-2')
        self.assertEqual(swapped['player']['inventory']['left-1'], 18)
        self.assertEqual(swapped['player']['inventory']['left-2'], 0)
        removed = self.command('enhancement.unequip', 'remove', slot_id='left')
        self.assertEqual(removed['player']['inventory']['left-2'], 1)
        self.assertEqual(self.command('enhancement.unequip', 'remove', slot_id='left'), removed)
        self.assertEqual(first['character']['enhancement']['items'], {'left': 'left-1'})

    def test_bulk_equipped_component_and_class_history_survive_switch_and_restart(self):
        self.earn()
        self.command('enhancement.equip', 'equip', item_id='left-1')
        first = self.command('enhancement.bulk', 'bulk', class_id='guard', target_quality=2)
        state = first['character']['enhancement']
        self.assertEqual(state['items'], {'left': 'left-2', 'right': 'right-2'})
        self.assertEqual(state['learned']['guard'], ['guard1', 'guard2'])
        self.assertEqual(first['player']['inventory']['left-1'], 18)
        self.assertEqual(first['player']['inventory']['right-1'], 18)
        self.command('enhancement.select_class', 'class-two', class_id='healer')
        self.command('enhancement.learn', 'heal', skill_id='healer1')
        switched = self.command('enhancement.select_class', 'class-one', class_id='guard')
        self.assertEqual(switched['character']['enhancement']['learned']['guard'], ['guard1', 'guard2'])
        self.assertEqual(switched['character']['enhancement']['swap_available_at'], 1020)
        self.assertEqual(self.command('enhancement.learn', 'known', skill_id='guard2')['wallet_changes'], [])
        restarted = GameService(self.path, self.catalog)
        self.assertEqual(restarted.get_player('a')['characters']['scout']['enhancement'], switched['character']['enhancement'])
        self.assertEqual(self.command('enhancement.bulk', 'bulk', class_id='guard', target_quality=2), first)

    def test_late_bulk_failure_rolls_back_refund_fusion_currency_and_receipt(self):
        catalog = deepcopy(self.catalog)
        catalog['offers']['kit']['rewards']['inventory']['right-1'] = 1
        self.service = GameService(Path(self.temp.name) / 'late.sqlite', catalog)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.earn()
        self.command('enhancement.equip', 'equip', item_id='left-1')
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('enhancement.bulk', 'late', class_id='guard', target_quality=2)
        self.assertEqual(self.service.get_player('a'), before)
        self.kit('more')
        self.assertEqual(self.command('enhancement.bulk', 'late', class_id='guard', target_quality=2)['character']['enhancement']['quality'], 2)

    def test_matrix_skills_and_tier_gates_never_mint_base_crystals(self):
        self.earn()
        self.command('enhancement.select_class', 'select', class_id='guard')
        with self.assertRaises(DomainError):
            self.command('enhancement.learn', 'learn', skill_id='guard1')
        with self.assertRaises(DomainError):
            self.command('enhancement.refresh', 'matrix')
        self.command('enhancement.bulk', 'bulk', class_id='guard', target_quality=2)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('enhancement.tier_up', 'tier', target_tier=2)
        self.assertEqual(self.service.get_player('a'), before)
        self.command('character.train_items', 'training', expected_level=1, items={'module': 6})
        first = self.command('enhancement.tier_up', 'tier', target_tier=2)
        self.assertEqual(first['character']['enhancement']['tier'], 2)
        self.assertEqual(self.command('enhancement.tier_up', 'same-tier', target_tier=2)['wallet_changes'], [])
        upgraded = self.command('enhancement.bulk', 'next', class_id='guard', target_quality=3)
        self.assertEqual(upgraded['character']['enhancement']['quality'], 3)
        self.assertEqual(upgraded['player']['inventory']['left-2'], 1)

    def test_catalog_rejects_cyclic_fusion_and_ambiguous_targets(self):
        catalog = deepcopy(self.catalog)
        catalog['enhancements']['items']['left-1']['fusion'] = {'source': 'left-2', 'quantity': 2, 'cost': {}}
        with self.assertRaises(DomainError):
            validate_catalog(catalog)
        catalog = deepcopy(self.catalog)
        catalog['enhancements']['items']['duplicate'] = deepcopy(catalog['enhancements']['items']['left-1'])
        with self.assertRaises(DomainError):
            validate_catalog(catalog)


class PromotionTests(ExtensionFixture):
    def test_application_caps_to_rank_preserves_items_and_advanced_requires_base(self):
        self.kit('early')
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('promotion.apply', 'apply')
        self.assertEqual(self.service.get_player('a'), before)
        self.earn()
        inventory = self.service.get_player('a')['inventory']
        self.assertEqual(self.command('promotion.apply', 'first')['character']['promotion_rank'], 1)
        self.command('character.rank_up', 'rank2')
        self.command('character.rank_up', 'rank3')
        base = self.command('promotion.apply', 'base')
        self.assertEqual(base['character']['promotion_rank'], 3)
        advanced = self.command('promotion.apply', 'advanced')
        self.assertEqual(advanced['character']['promotion_rank'], 5)
        for item in ('low', 'middle', 'high', 'advanced'):
            self.assertEqual(advanced['player']['inventory'][item], inventory[item])
        self.assertEqual(self.command('promotion.apply', 'base'), base)
        self.assertEqual(self.service.get_player('a')['characters']['scout']['promotion_rank'], 5)

    def test_recycling_retains_one_highest_ignores_advanced_and_concurrent_retry_pays_once(self):
        self.earn()
        before = self.service.get_player('a')
        def recycle(_):
            return self.command('promotion.recycle', 'recycle')
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(recycle, range(4)))
        self.assertTrue(all(response == responses[0] for response in responses))
        result = responses[0]
        self.assertEqual(result['rewards'], {'wallet': {'credits': 7}, 'inventory': {'recycle-token': 10}, 'experience': 0})
        inventory = result['player']['inventory']
        self.assertEqual([inventory[key] for key in ('low', 'middle', 'high', 'advanced')], [0, 0, 1, 1])
        self.assertEqual(result['player']['wallet']['credits'], before['wallet']['credits'] + 7)
        again = self.command('promotion.recycle', 'nothing-left')
        self.assertEqual(again['wallet_changes'], [])
        self.assertEqual(again['inventory_changes'], [])
        self.assertEqual(GameService(self.path, self.catalog).get_player('a'), again['player'])

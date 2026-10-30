"""Paid openings conserve fragments and persist actual RNG/XP/prize receipts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.loot import roll
from game_server.service import GameService


def product_catalog(empty=False):
    catalog = deepcopy(DEMO_CATALOG)
    catalog['missions']['training.1']['rewards'] = {'experience': 100}
    catalog['offers']['fragments'] = {'cost': {}, 'purchase_limit': 1, 'rewards': {'inventory': {'fragments': 10000}}}
    item = {'weight': 1, 'minimum': 0 if empty else 1, 'maximum': 0 if empty else 1,
            'multiplier': 0 if empty else 1, 'rewards': None if empty else {'inventory': {'fragments': 1, 'badge': 2}}}
    catalog['loot_tables'] = {'prizes': {'groups': [[{'weight': 1, 'multiplier': 1, 'items': [item]}]]}}
    catalog['loot_products'] = {'pack': {'revision': 'release-1', 'table_id': 'prizes', 'payment_item_id': 'fragments',
        'unit_cost': 2, 'experience': 1, 'required_level': 1, 'batch_required_level': 2, 'maximum_quantity': 100}}
    return catalog


class LootProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.catalog = product_catalog()
        self.service = GameService(self.path, self.catalog)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.command('shop.purchase', 'fund', offer_id='fragments', quantity=1)

    def command(self, command, key, **parameters):
        return self.service.execute('a', {'command': command, 'idempotency_key': key, 'parameters': parameters})

    def open(self, key='open', quantity=1, revision='release-1'):
        return self.command('loot.open', key, product_id='pack', revision=revision, quantity=quantity)

    def earn(self):
        battle = self.command('mission.begin', 'begin', mission_id='training.1', team=['scout'])['battle']
        # The independently authored mission earns the batch level gate.
        self.command('mission.complete', 'win', mission_id='training.1', battle_id=battle['battle_id'], outcome='victory', stars=3)

    def test_single_concurrent_retry_saved_seed_and_payment_token_reward_survive_restart(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.open(), range(3)))
        self.assertTrue(all(result == results[0] for result in results))
        result = results[0]
        self.assertEqual(result['payment'], {'item_id': 'fragments', 'quantity': 2, 'previous_balance': 10000, 'balance': 9999})
        self.assertEqual(result['player']['inventory']['badge'], 2)
        self.assertEqual(result['loot_rolls'], [roll(self.catalog['loot_tables'], 'prizes', result['roll_seed'] + ':0')])
        self.service = GameService(self.path, self.catalog)
        self.assertEqual(self.open(), result)
        with self.assertRaises(DomainError):
            self.open(quantity=2)

    def test_batch_gate_25_and_100_spins_preserve_cost_xp_and_final_self_token_balance(self):
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.open(quantity=25)
        self.assertEqual(self.service.get_player('a'), before)
        self.earn()
        result = self.open(quantity=25)
        self.assertEqual(result['payment']['quantity'], 50)
        self.assertEqual(result['payment']['balance'], 9975)
        self.assertEqual(result['opening_experience'], 25)
        self.assertEqual(len(result['loot_rolls']), 25)
        self.assertEqual(self.open('hundred', quantity=100)['payment']['balance'], 9875)

    def test_forged_cost_seed_revision_and_invalid_counts_leave_state_unchanged(self):
        before = self.service.get_player('a')
        for parameters in ({'product_id': 'pack', 'revision': 'old', 'quantity': 1},
                           {'product_id': 'pack', 'revision': 'release-1', 'quantity': True},
                           {'product_id': 'pack', 'revision': 'release-1', 'quantity': 101},
                           {'product_id': 'pack', 'revision': 'release-1', 'quantity': 1, 'seed': 'client'},
                           {'product_id': 'pack', 'revision': 'release-1', 'quantity': 1, 'cost': 0}):
            with self.assertRaises(DomainError):
                self.command('loot.open', 'retry', **parameters)
            self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(self.command('loot.open', 'retry', product_id='pack', revision='release-1', quantity=1)['quantity'], 1)

    def test_empty_result_rejects_even_opening_xp_and_does_not_debit_or_persist_receipt(self):
        catalog = product_catalog(empty=True)
        service = GameService(Path(self.temp.name) / 'empty.sqlite', catalog)
        service.create_player({'player_id': 'a', 'display_name': 'A'})
        service.execute('a', {'command': 'shop.purchase', 'idempotency_key': 'fund', 'parameters': {'offer_id': 'fragments', 'quantity': 1}})
        before = service.get_player('a')
        with self.assertRaises(DomainError):
            service.execute('a', {'command': 'loot.open', 'idempotency_key': 'empty',
                'parameters': {'product_id': 'pack', 'revision': 'release-1', 'quantity': 1}})
        self.assertEqual(service.get_player('a'), before)
        with closing(service.connect()) as connection:
            self.assertIsNone(connection.execute("SELECT result FROM commands WHERE idempotency_key='empty'").fetchone())

    def test_missing_table_counter_effect_and_invalid_cost_rejected_before_database_use(self):
        for mutate in (lambda c: c['loot_products']['pack'].update(unit_cost=0),
                       lambda c: c['loot_products']['pack'].update(table_id='missing'),
                       lambda c: c['loot_tables']['prizes']['groups'][0][0]['items'][0].update(counters={'unowned': 1})):
            catalog = deepcopy(self.catalog)
            mutate(catalog)
            with self.assertRaises(DomainError):
                validate_catalog(catalog)

    def test_optional_fingerprint_preserves_request_identity_and_saved_time(self):
        self.service.clock = lambda: 12345
        parameters = {'product_id': 'pack', 'revision': 'release-1', 'quantity': 1, 'request_fingerprint': 'a' * 64}
        result = self.command('loot.open', 'bound', **parameters)
        self.assertEqual(result['server_time'], 12345)
        self.open('later')
        self.service = GameService(self.path, self.catalog, clock=lambda: 99999)
        self.assertEqual(self.command('loot.open', 'bound', **parameters), result)
        before = self.service.get_player('a')
        for fingerprint in ('b' * 64, '', None, 'X' * 64, 'a' * 65):
            with self.assertRaises(DomainError):
                self.command('loot.open', 'bound' if fingerprint == 'b' * 64 else 'invalid',
                             **{**parameters, 'request_fingerprint': fingerprint})
            self.assertEqual(self.service.get_player('a'), before)

    def test_failure_during_reward_grant_rolls_back_debit_and_receipt(self):
        catalog = product_catalog()
        catalog['loot_tables']['prizes']['groups'][0][0]['items'][0]['rewards'] = {
            'wallet': {'credits': 9000000000000000}, 'inventory': {'badge': 1}}
        service = GameService(Path(self.temp.name) / 'overflow.sqlite', catalog)
        service.create_player({'player_id': 'a', 'display_name': 'A'})
        service.execute('a', {'command': 'shop.purchase', 'idempotency_key': 'fund',
                             'parameters': {'offer_id': 'fragments', 'quantity': 1}})
        before = service.get_player('a')
        with self.assertRaises(DomainError):
            service.execute('a', {'command': 'loot.open', 'idempotency_key': 'overflow',
                'parameters': {'product_id': 'pack', 'revision': 'release-1', 'quantity': 1}})
        self.assertEqual(service.get_player('a'), before)
        with closing(service.connect()) as connection:
            self.assertIsNone(connection.execute("SELECT result FROM commands WHERE idempotency_key='overflow'").fetchone())

    def test_payment_receipt_includes_later_level_awards_of_the_payment_item(self):
        catalog = product_catalog()
        catalog['experience_per_level'] = 1
        catalog['level_rewards'] = {'policy_id': 'authored-level', 'awards': {'2': {'inventory': {'fragments': 5}}}}
        service = GameService(Path(self.temp.name) / 'level-award.sqlite', catalog)
        service.create_player({'player_id': 'a', 'display_name': 'A'})
        service.execute('a', {'command': 'shop.purchase', 'idempotency_key': 'fund',
                             'parameters': {'offer_id': 'fragments', 'quantity': 1}})
        request = {'command': 'loot.open', 'idempotency_key': 'open',
                   'parameters': {'product_id': 'pack', 'revision': 'release-1', 'quantity': 1}}
        result = service.execute('a', request)
        self.assertEqual(result['player']['level'], 2)
        self.assertEqual(result['player']['inventory']['fragments'], 10004)
        self.assertEqual(result['payment']['balance'], 10004)
        service = GameService(service.database, catalog)
        self.assertEqual(service.execute('a', request), result)

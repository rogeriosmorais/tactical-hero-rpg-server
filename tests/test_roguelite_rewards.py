"""Authored economy and frozen settlements, with no original game inputs."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DomainError, MAX_AMOUNT, validate_catalog
from game_server.service import GameService
from test_roguelite import authored_catalog


def scaled_catalog(level=1):
    catalog = authored_catalog()
    catalog.update(maximum_player_level=110, experience_thresholds=[100 * n for n in range(110)])
    catalog['starting_characters'] = {hero: min(level, 109) for hero in ('scout', 'guardian', 'healer')}
    for hero, definition in catalog['characters'].items():
        definition['maximum_level'] = 110
        development = catalog['development']['characters'][hero]
        development['experience_thresholds'] = [n ** 3 for n in range(110)]
        development['abilities'][0][1] = {'required_level': 50, 'required_tier': 1,
            'payment': {'wallet': {'credits': 5000}, 'inventory': {'advanced-token': 10}}}
        development['tiers'][0] = {'head': {'item_id': 'plate', 'required_level': 50}}
    catalog['development']['training_items']['training'] = {'experience': 1, 'cost': {'credits': 5}}
    catalog['development']['recipes']['plate'] = {'ingredients': {'ore': 5}, 'cost': {'credits': 200}}
    rules = catalog['roguelite']
    rules.update(enemy_base_level=level, enemy_level_step=0)
    rules['reward_bands'] = []
    for enemy_level in range(1, 111):
        # One next hero level per victory throughout this independently authored
        # curve. At the cap use the last level's cost; other heroes still grow.
        cost_level = min(enemy_level, 109)
        xp = cost_level ** 3 - (cost_level - 1) ** 3
        count = xp
        advanced = enemy_level >= 50
        rules['reward_bands'].append({'minimum_enemy_level': enemy_level,
            'shard_multiplier': 1 + enemy_level // 25,
            'victory_rewards': {'experience': enemy_level * 100,
                'wallet': {'credits': count * 5 + (5200 if advanced else 0)},
                'inventory': {'training': count,
                    **({'advanced-token': 10, 'ore': 5} if advanced else {'ability-token': 1})}}})
    return catalog


class ScaledRewardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sequence = 0

    def setup_game(self, level=1):
        self.catalog = scaled_catalog(level)
        self.database = Path(self.temp.name) / f'level-{level}.sqlite'
        self.service = GameService(self.database, self.catalog)
        self.service.create_player({'player_id': 'a', 'display_name': 'Authored economy probe'})

    def command(self, name, key=None, **parameters):
        self.sequence += 1
        return self.service.execute('a', {'command': name, 'idempotency_key': key or str(self.sequence),
                                         'parameters': parameters})

    def start(self):
        return self.command('roguelite.start')['roguelite']['active_run']

    def begin(self, run, team=None):
        return self.command('roguelite.begin', run_id=run['run_id'],
            encounter_id=run['encounter']['encounter_id'], team=team or ['scout'])['battle']

    def finish(self, battle, outcome='victory', key=None):
        return self.command('roguelite.complete', key=key, battle_id=battle['battle_id'],
                            outcome=outcome, stars=3 if outcome == 'victory' else 0)

    def test_level_fifty_and_high_level_rewards_fund_actual_development(self):
        for level in (1, 25, 50, 75, 109, 110):
            with self.subTest(level=level):
                self.setup_game(level)
                run = self.start()
                expected = self.catalog['roguelite']['reward_bands'][level - 1]
                self.assertEqual(run['encounter']['victory_rewards'], expected['victory_rewards'])
                battle = self.begin(run)
                result = self.finish(battle)
                self.assertEqual(result['rewards'], expected['victory_rewards'])
                count = result['rewards']['inventory']['training']
                player = self.command('character.train_items', character_id='scout',
                    expected_level=min(level, 109), items={'training': count})['player']
                self.assertEqual(player['characters']['scout']['level'], min(level + 1, 110))
                self.assertEqual(player['inventory']['training'], 0)
                if level >= 50:
                    self.command('character.ability_upgrade', character_id='scout', ability_index=0, unlock=False)
                    self.command('equipment.craft', item_id='plate', quantity=1)
                    self.command('equipment.equip', character_id='scout',
                        slots=[{'slot_id': 'head', 'item_id': 'plate'}], craft_missing=False)
                    player = self.service.get_player('a')
                    self.assertEqual(player['characters']['scout']['abilities'], [2])
                    self.assertEqual(player['characters']['scout']['equipment'], {'head': 'plate'})
                    self.assertEqual(player['wallet']['credits'], 0)
                choice = result['roguelite']['active_run']['pending_choice']
                self.assertTrue(all(option['quantity'] == 5 * expected['shard_multiplier']
                                    for option in choice['options']))

    def test_high_account_cannot_inflate_first_floor_payout(self):
        self.setup_game()
        initial = deepcopy(self.catalog['roguelite']['reward_bands'][0]['victory_rewards'])
        for _ in range(50):
            run = self.start()
            result = self.finish(self.begin(run))
            self.assertEqual(result['rewards'], initial)
            self.command('roguelite.end', run_id=run['run_id'])
        self.assertEqual(self.service.get_player('a')['level'], 51)
        self.assertEqual(self.start()['encounter']['reward_enemy_level'], 1)

    def test_frozen_rewards_survive_restart_loss_retry_and_exact_replay(self):
        self.setup_game(50)
        run = self.start()
        expected = deepcopy(run['encounter']['victory_rewards'])
        lost = self.finish(self.begin(run), 'defeat')
        self.assertEqual(lost['rewards'], {'wallet': {}, 'inventory': {}, 'experience': 0})
        self.assertEqual(lost['roguelite']['active_run']['encounter'], run['encounter'])
        battle = self.begin(run, ['guardian'])
        self.service = GameService(self.database, self.catalog)
        self.assertEqual(self.service.get_battle('a', battle['battle_id']), battle)
        # Neither later policy/roster mutations nor refresh determine this issued
        # fight's payout. Production catalog replacement is separately blocked.
        self.service.catalog['roguelite']['reward_bands'][49]['victory_rewards'] = {}
        self.service.catalog['roguelite']['reward_bands'][49]['shard_multiplier'] = 100
        paid = self.finish(battle, key='earned')
        self.assertEqual(paid['rewards'], expected)
        self.assertTrue(all(option['quantity'] == 15 for option in
                            paid['roguelite']['active_run']['pending_choice']['options']))
        self.assertEqual(self.finish(battle, key='earned'), paid)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.finish(battle, key='another')
        self.assertEqual(self.service.get_player('a'), before)

    def test_conservative_band_uses_all_enemy_levels_and_catalog_rejects_bad_bands(self):
        from game_server.roguelite import reward_snapshot
        self.setup_game()
        enemies = [{'state': {'level': level}} for level in (1, 50, 50, 50, 50)]
        self.assertEqual(reward_snapshot(self.catalog['roguelite'], enemies)['reward_enemy_level'], 1)
        for bad in (None, [], [{'minimum_enemy_level': 2, 'victory_rewards': {}, 'shard_multiplier': 1}],
                    [{'minimum_enemy_level': 1, 'victory_rewards': {}, 'shard_multiplier': True}],
                    [{'minimum_enemy_level': 1, 'victory_rewards': {}, 'shard_multiplier': MAX_AMOUNT}],
                    [{'minimum_enemy_level': 1, 'victory_rewards': {'experience': -1}, 'shard_multiplier': 1}],
                    [self.catalog['roguelite']['reward_bands'][0]] * 2):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                catalog = deepcopy(self.catalog)
                catalog['roguelite']['reward_bands'] = bad
                validate_catalog(catalog)

    def test_old_saved_battle_without_snapshot_keeps_flat_catalog_payout(self):
        self.catalog = authored_catalog()
        self.database = Path(self.temp.name) / 'old-shape.sqlite'
        self.service = GameService(self.database, self.catalog)
        self.service.create_player({'player_id': 'a', 'display_name': 'Legacy fixture'})
        battle = self.begin(self.start())
        with closing(self.service.connect()) as connection, connection:
            for key in ('victory_rewards', 'shard_multiplier', 'reward_enemy_level'):
                battle.pop(key)
            connection.execute('UPDATE battles SET data=? WHERE id=?', (json.dumps(battle), battle['battle_id']))
        self.service = GameService(self.database, self.catalog)
        self.assertEqual(self.finish(battle)['rewards'], self.catalog['roguelite']['victory_rewards'])

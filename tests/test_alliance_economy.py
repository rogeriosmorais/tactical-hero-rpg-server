"""Donation ordinal limits, boost gates, aggregate costs and frozen combat ownership."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService
from test_alliances import alliance_rules
from test_towers import tower_catalog


def economy_catalog():
    catalog = deepcopy(DEMO_CATALOG)
    catalog['alliances'] = alliance_rules()
    catalog['alliances'].update(required_level=1, experience_per_level=100, maximum_level=5)
    catalog['starting_wallet'] = {'credits': 200, 'energy': 20, 'research': 0, 'premium': 0}
    catalog['missions']['training.1'].update(rewards={'experience': 200}, first_clear_rewards={})
    donations = [{'cost': {'credits': cost}, 'rewards': {'wallet': {'research': 100}},
                  'maxed_rewards': {'wallet': {'credits': 1}}, 'experience': xp}
                 for cost, xp in ((10, 60), (20, 70), (30, 80))]
    step = lambda cost, level, percent: {'cost': {'research': cost}, 'required_level': level, 'percent': percent}
    catalog['alliance_economy'] = {'donations': {'basic': donations,
        'elite': [{**deepcopy(row), 'cost': {'premium': 50}} for row in donations]}, 'boosts': {
        'health': {'alpha': [step(20, 1, 10)], 'beta': [step(30, 1, 20)]},
        'attack': {'alpha': [step(25, 1, 5), step(30, 3, 10)], 'beta': [step(25, 2, 5), step(30, 3, 10)]}}}
    catalog['objectives'] = {'entries': {}, 'counters': {
        'contribution': {'source': {'kind': 'alliance_contribution'}},
        'daily-contribution': {'source': {'kind': 'activity', 'rules': [
            {'command': 'alliance.donate', 'measure': 'alliance_experience'}]}, 'period': {'seconds': 86400, 'anchor': 0}},
        'spent': {'source': {'kind': 'activity', 'rules': [
            {'command': 'alliance.donate', 'measure': 'wallet_spent', 'currency_id': 'credits'}]}}}}
    return catalog


class AllianceEconomyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.catalog = economy_catalog()
        self.now = 1000000
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def command(self, command, key, **parameters):
        return self.service.execute('a', {'command': command, 'idempotency_key': key, 'parameters': parameters})

    def donate(self, key='donate', tier='basic'):
        return self.command('alliance.donate', key, tier_id=tier)

    def earn_level(self):
        battle = self.command('mission.begin', 'level-begin', mission_id='training.1', team=['scout'])['battle']
        return self.command('mission.complete', 'level-win', mission_id='training.1', battle_id=battle['battle_id'], outcome='victory', stars=3)

    def test_donation_wallet_xp_stats_concurrent_replay_and_restart(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.donate(), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        result = results[0]
        self.assertEqual(result['player']['wallet']['credits'], 190)
        self.assertEqual(result['player']['wallet']['research'], 100)
        self.assertEqual(result['alliance_state']['current']['experience'], 60)
        self.assertEqual(result['alliance_state']['economy']['donations']['count'], 1)
        self.assertEqual(result['objective_state']['counters']['contribution']['value'], 60)
        self.assertEqual(result['objective_state']['counters']['spent']['value'], 10)
        restarted = GameService(self.path, self.catalog, clock=lambda: self.now + 86400)
        self.assertEqual(restarted.execute('a', {'command': 'alliance.donate', 'idempotency_key': 'donate',
            'parameters': {'tier_id': 'basic'}}), result)

    def test_daily_ordinal_cap_and_failed_cost_rollback_and_reset(self):
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.donate('poor', 'elite')
        self.assertEqual(self.service.get_player('a'), before)
        for index in range(3):
            self.donate('day1-' + str(index))
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.donate('fourth')
        self.assertEqual(self.service.get_player('a'), before)
        self.now += 86400
        self.assertEqual(self.service.get_alliances('a')['economy']['donations']['count'], 0)
        result = self.donate('next-day')
        self.assertEqual(result['donation_count'], 1)
        self.assertEqual(result['payment'], {'credits': 10})
        self.assertEqual(result['objective_state']['counters']['daily-contribution']['value'], 60)
        self.assertEqual(result['objective_state']['counters']['contribution']['value'], 270)
        self.now -= 86400
        with self.assertRaises(DomainError):
            self.donate('past-day')

    def test_bulk_target_gate_and_aggregate_payment_are_atomic_and_levels_persist(self):
        self.donate()
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('alliance.upgrade_category', 'locked', category_id='attack')
        self.assertEqual(self.service.get_player('a'), before)
        result = self.command('alliance.upgrade_category', 'health', category_id='health')
        self.assertEqual(result['payment'], {'research': 50})
        self.assertEqual(result['player']['alliance_boosts']['health'], {'alpha': 1, 'beta': 1})
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('alliance.upgrade_category', 'maxed', category_id='health')
        self.assertEqual(self.service.get_player('a'), before)
        aid = result['alliance_state']['current']['alliance_id']
        self.command('alliance.leave', 'leave', alliance_id=aid)
        self.assertEqual(self.service.get_player('a')['alliance_boosts'], result['player']['alliance_boosts'])
        with self.assertRaises(DomainError):
            self.command('alliance.upgrade_boost', 'no-team', category_id='attack', boost_id='alpha')

    def test_membership_changes_keep_daily_usage_and_join_cooldown(self):
        self.donate()
        aid = self.service.get_alliances('a')['current']['alliance_id']
        self.command('alliance.leave', 'leave', alliance_id=aid)
        with self.assertRaises(DomainError):
            self.donate('none')
        self.command('alliance.join', 'join', alliance_id=aid, allow_switch=False)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.donate('cooldown')
        self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(self.service.get_alliances('a')['economy']['donations']['count'], 1)
        self.now += 86400
        self.assertEqual(self.donate('after-cooldown')['donation_count'], 1)

    def test_fully_maxed_rewards_and_battle_snapshot_do_not_use_later_upgrades(self):
        self.donate()
        battle = self.command('mission.begin', 'before-upgrade', mission_id='training.1', team=['scout'])['battle']
        self.command('alliance.upgrade_category', 'health', category_id='health')
        self.assertEqual(self.service.get_battle('a', battle['battle_id'])['alliance_boosts'], {})
        self.earn_level()
        self.donate('second')
        self.command('alliance.upgrade_category', 'attack1', category_id='attack')
        self.command('alliance.upgrade_category', 'attack2', category_id='attack')
        self.assertTrue(self.service.get_alliances('a')['economy']['boosts_maxed'])
        before = self.service.get_player('a')['wallet']['research']
        result = self.donate('maxed-donation')
        self.assertEqual(result['rewards']['wallet'], {'credits': 1})
        self.assertEqual(result['player']['wallet']['research'], before)
        issued = self.command('mission.begin', 'after-upgrade', mission_id='training.1', team=['scout'])['battle']
        self.assertEqual(issued['alliance_boosts']['attack']['alpha'], 2)

    def test_invalid_boost_catalog_and_client_selected_percent_rejected(self):
        for value in (True, float('nan'), -1):
            catalog = deepcopy(self.catalog)
            catalog['alliance_economy']['boosts']['health']['alpha'][0]['percent'] = value
            with self.assertRaises(DomainError):
                validate_catalog(catalog)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('alliance.upgrade_boost', 'forged', category_id='health', boost_id='alpha', percent=999)
        self.assertEqual(self.service.get_player('a'), before)

    def test_bulk_combined_payment_failure_and_same_key_retry_after_funding(self):
        self.donate()
        self.earn_level()
        self.command('alliance.upgrade_category', 'health', category_id='health')
        self.command('alliance.upgrade_boost', 'alpha', category_id='attack', boost_id='alpha')
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('alliance.upgrade_category', 'batch', category_id='attack')
        self.assertEqual(self.service.get_player('a'), before)
        self.donate('fund')
        result = self.command('alliance.upgrade_category', 'batch', category_id='attack')
        self.assertEqual(result['payment'], {'research': 55})
        self.assertEqual(result['player']['alliance_boosts']['attack'], {'alpha': 2, 'beta': 1})

    def test_tower_battle_also_freezes_profile_boosts_without_changing_run_rules(self):
        catalog = deepcopy(self.catalog)
        catalog['towers'] = tower_catalog()['towers']
        self.now = 1001
        self.service = GameService(Path(self.temp.name) / 'tower.sqlite', catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.donate()
        self.command('alliance.upgrade_boost', 'alpha', category_id='health', boost_id='alpha')
        started = self.command('tower.start', 'start', tower_id='trial', difficulty_id='intro')
        issued = self.command('tower.begin', 'begin', tower_id='trial', run_id=started['tower']['active_run']['run_id'],
                              floor_index=0, encounter_id='trial.alpha', team=['scout'])['battle']
        self.command('alliance.upgrade_boost', 'beta', category_id='health', boost_id='beta')
        self.assertEqual(self.service.get_battle('a', issued['battle_id'])['alliance_boosts'], {'health': {'alpha': 1}})

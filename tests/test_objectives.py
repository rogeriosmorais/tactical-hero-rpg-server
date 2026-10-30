"""Earned counters/reset claims and automatic premium passes use only transactions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


def objective_catalog():
    catalog = deepcopy(DEMO_CATALOG)
    catalog['missions']['training.1']['rewards'] = {'experience': 100}
    catalog['offers']['training-kit'].update(purchase_limit=10, rewards={'wallet': {'credits': 5}, 'inventory': {'training-kit': 1}})
    daily = {'seconds': 86400, 'anchor': 0}
    activity = lambda rules, period=None: {'source': {'kind': 'activity', 'rules': rules}, **({'period': period} if period else {})}
    catalog['objectives'] = {'counters': {
        'wins': activity([{'command': 'mission.complete', 'measure': 'count', 'outcome': 'victory'},
                          {'command': 'mission.sweep', 'measure': 'quantity'}], daily),
        'spend': activity([{'command': 'shop.purchase', 'measure': 'wallet_spent', 'currency_id': 'credits'}], daily),
        'local': activity([{'command': 'shop.purchase', 'measure': 'wallet_spent', 'currency_id': 'credits'}],
                          {'seconds': 86400, 'anchor': 0, 'local_time': True, 'alternate_anchor': 1800}),
        'points': activity([{'command': 'objective.claim', 'measure': 'inventory_earned', 'item_id': 'pass-point'}],
                          {'seconds': 100, 'anchor': 1000}),
        'login': activity([{'command': 'player.login', 'measure': 'login'}], daily),
        'streak': {'source': {'kind': 'login_streak'}}, 'size': {'source': {'kind': 'owned_characters'}},
        'level': {'source': {'kind': 'level'}}, 'slots': {'source': {'kind': 'inventory', 'item_id': 'team-slot'}},
        'clear': {'source': {'kind': 'mission_clear', 'mission_id': 'training.1'}},
        'unknown': {'source': {'kind': 'untracked'}},
        'all': {'source': {'kind': 'all_clear', 'objective_ids': ['win', 'purchase']}, 'period': daily},
        'tags': {'source': {'kind': 'signals', 'label_ids': ['alpha', 'beta']},
                 'period': {'seconds': 60, 'anchor': 1000, 'repeat': False}}}, 'entries': {}}
    for key, counter, threshold, reward in (
            ('win', 'wins', 2, {'wallet': {'credits': 5}, 'inventory': {'team-slot': 1, 'pass-point': 10},
                               'signals': [{'labels': ['alpha', 'beta'], 'quantity': 3}]}),
            ('purchase', 'spend', 20, {'wallet': {'credits': 1}}), ('complete', 'all', 1, {'experience': 5}),
            ('unknown', 'unknown', 0, {'wallet': {'credits': 999}}), ('tags', 'tags', 3, {'wallet': {'credits': 2}})):
        catalog['objectives']['entries'][key] = {'counter_id': counter, 'threshold': threshold,
            'required_level': 1, 'maximum_level': 100, 'rewards': reward}
    catalog['objectives']['entries']['tags']['claim_grace_seconds'] = 10
    catalog['passes'] = {'season': {'counter_id': 'points', 'required_level': 1, 'maximum_level': 100,
        'premium_required_level': 2, 'rewards': [
            {'reward_id': 'free-zero', 'threshold': 0, 'track': 'free', 'rewards': {'wallet': {'credits': 1}}},
            {'reward_id': 'premium-zero', 'threshold': 0, 'track': 'premium', 'rewards': {'wallet': {'credits': 2}}},
            {'reward_id': 'free-ten', 'threshold': 10, 'track': 'free', 'rewards': {'inventory': {'badge': 1}}},
            {'reward_id': 'premium-ten', 'threshold': 10, 'track': 'premium', 'rewards': {'inventory': {'premium-badge': 1}}}]}}
    return catalog


class ObjectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1010
        self.catalog = objective_catalog()
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def command(self, name, key, player='a', **parameters):
        return self.service.execute(player, {'command': name, 'idempotency_key': key, 'parameters': parameters})

    def earn(self):
        battle = self.command('mission.begin', 'begin', mission_id='training.1', team=['scout'])['battle']
        self.command('mission.complete', 'win', battle_id=battle['battle_id'], mission_id='training.1', outcome='victory', stars=3)
        self.command('mission.sweep', 'sweep', mission_id='training.1', quantity=1)
        self.command('shop.purchase', 'purchase', offer_id='training-kit', quantity=1)

    def test_gross_spending_trusted_wins_all_clear_and_atomic_claim_rewards(self):
        self.earn()
        state = self.service.get_objectives('a')
        self.assertEqual(state['counters']['spend']['value'], 20)
        self.assertEqual(state['counters']['wins']['value'], 2)
        self.assertEqual(state['counters']['clear']['value'], 1)
        self.assertEqual(state['counters']['all']['value'], 1)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('objective.claim', 'bad', objective_ids=['win', 'unknown'])
        with self.assertRaises(DomainError):
            self.command('objective.claim', 'forged', objective_ids=['win'], progress=999)
        self.assertEqual(self.service.get_player('a'), before)
        first = self.command('objective.claim', 'claim', objective_ids=['win', 'purchase'])
        self.assertEqual(first['rewards']['inventory'], {'team-slot': 1, 'pass-point': 10})
        self.assertEqual(first['objective_state']['counters']['points']['value'], 10)
        self.assertEqual(first['objective_state']['counters']['tags']['value'], 3)
        self.assertEqual(len(first['player']['reward_signals']), 1)
        self.assertNotIn('_transaction_activity', first['player'])
        self.assertEqual(self.command('objective.claim', 'claim', objective_ids=['win', 'purchase']), first)
        with self.assertRaises(DomainError):
            self.command('objective.claim', 'again', objective_ids=['win'])
        self.assertEqual(self.service.get_player('a')['inventory']['pass-point'], 10)

    def test_reads_reset_projection_without_mutating_and_old_receipt_survives_new_day(self):
        self.earn()
        first = self.command('objective.claim', 'claim', objective_ids=['win'])
        before = self.service.get_player('a')
        self.now += 86400
        state = self.service.get_objectives('a')
        self.assertEqual(state['counters']['wins']['value'], 0)
        self.assertFalse(state['objectives']['win']['claimed'])
        self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(self.command('objective.claim', 'claim', objective_ids=['win']), first)
        self.assertEqual(GameService(self.path, self.catalog).get_player('a'), before)
        with self.assertRaises(DomainError):
            self.command('objective.claim', 'new-day', objective_ids=['win'])

    def test_fixed_player_offsets_and_alternate_anchors_are_independent(self):
        self.now = 4 * 86400 + 100
        for key, alternate in (('local', False), ('alternate', True)):
            self.service.create_player({'player_id': key, 'display_name': key,
                                        'reset_offset_seconds': 3600, 'alternate_reset': alternate})
            self.command('shop.purchase', 'purchase', player=key, offer_id='training-kit', quantity=1)
        before = self.service.get_player('alternate')
        self.service.create_player({'player_id': 'alternate', 'display_name': 'changed', 'reset_offset_seconds': 0, 'alternate_reset': False})
        self.assertEqual(self.service.get_player('alternate'), before)
        self.now = 5 * 86400 - 3599
        self.assertEqual(self.service.get_objectives('local')['counters']['local']['value'], 0)
        self.assertEqual(self.service.get_objectives('local')['counters']['spend']['value'], 20)
        self.assertEqual(self.service.get_objectives('alternate')['counters']['local']['value'], 0)

    def test_login_day_deduplication_and_streak_are_server_owned(self):
        self.command('player.login', 'login')
        self.command('player.login', 'different-nonce')
        self.assertEqual(self.service.get_objectives('a')['counters']['login']['value'], 1)
        self.now += 86400
        self.command('player.login', 'tomorrow')
        self.assertEqual(self.service.get_objectives('a')['counters']['streak']['value'], 2)
        self.now += 2 * 86400
        self.command('player.login', 'gap')
        self.assertEqual(self.service.get_objectives('a')['counters']['streak']['value'], 1)

    def test_nonrepeating_signal_window_preserves_one_action_and_claim_grace(self):
        self.earn()
        self.command('objective.claim', 'claim', objective_ids=['win'])
        self.now = 1065
        state = self.service.get_objectives('a')
        self.assertEqual(state['counters']['tags']['value'], 3)
        self.assertTrue(state['objectives']['tags']['claimable'])
        self.command('objective.claim', 'signal-claim', objective_ids=['tags'])
        self.now = 1071
        self.assertFalse(self.service.get_objectives('a')['objectives']['tags']['available'])

    def test_pass_levels_periods_claim_all_and_concurrent_exact_retry(self):
        state = self.service.get_passes('a')['passes']['season']['current']
        before = self.service.get_player('a')
        self.service.get_passes('a')
        self.assertEqual(self.service.get_player('a'), before)
        with self.assertRaises(DomainError):
            self.command('pass.unlock', 'unlock', pass_id='season', period_key=state['period']['key'])
        self.command('pass.claim', 'free', pass_id='season', period_key='0', reward_ids=['free-zero'])
        self.earn()
        self.command('objective.claim', 'claim', objective_ids=['win'])
        self.assertEqual(self.service.get_passes('a')['passes']['season']['current']['points'], 10)
        self.command('pass.unlock', 'unlock', pass_id='season', period_key='0')
        def claim(_):
            return self.command('pass.claim', 'all', pass_id='season', period_key='0', all=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(claim, range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(set(results[0]['reward_ids']), {'premium-zero', 'free-ten', 'premium-ten'})
        self.now = 1110
        before = self.service.get_player('a')
        view = self.service.get_passes('a')['passes']['season']
        self.assertEqual(view['current']['points'], 0)
        self.assertTrue(view['history'][0]['premium'])
        self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(claim(0), results[0])
        with self.assertRaises(DomainError):
            self.command('pass.claim', 'stale', pass_id='season', period_key='0', all=True)

    def test_dependency_cycles_and_unsupported_stat_writes_are_rejected(self):
        catalog = deepcopy(self.catalog)
        catalog['objectives']['counters']['all']['source']['objective_ids'] = ['complete']
        with self.assertRaises(DomainError):
            validate_catalog(catalog)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('objective.set_counter', 'forged', counter_id='wins', value=999)
        self.assertEqual(self.service.get_player('a'), before)

    def test_claim_signals_points_and_pass_selection_share_one_transaction_time(self):
        self.earn()
        ticks = iter([1099, 1100, 1101, 1102])
        self.service.clock = lambda: next(ticks)
        claimed = self.command('objective.claim', 'boundary', objective_ids=['win'])
        self.assertEqual(claimed['server_time'], 1099)
        self.assertEqual(claimed['player']['reward_signals'][0]['at'], 1099)
        self.assertEqual(claimed['objective_state']['counters']['points']['value'], 10)
        self.assertEqual(claimed['pass_state']['passes']['season']['current']['period']['key'], '0')
        self.assertEqual(claimed['pass_state']['passes']['season']['current']['points'], 10)

    def test_old_free_season_does_not_gain_premium_when_new_season_levels_up(self):
        self.command('pass.claim', 'first', pass_id='season', period_key='0', reward_ids=['free-zero'])
        self.now = 1110
        self.earn()
        view = self.service.get_passes('a')['passes']['season']
        self.assertTrue(view['current']['premium'])
        self.assertFalse(view['history'][0]['premium'])

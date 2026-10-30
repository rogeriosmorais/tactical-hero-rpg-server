"""Independent seasonal economy and paid trait upgrades, no private files."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


def sample_catalog():
    catalog = deepcopy(DEMO_CATALOG)
    catalog['starting_wallet']['season_xp'] = 100
    catalog['starting_wallet']['credits'] = 100
    catalog['seasonal_boost'] = {
        'starts_at': 1000, 'ends_at': 40000, 'maximum_accrual_minutes': 20,
        'claim_throttle_seconds': 60, 'xp_currency_id': 'season_xp',
        'tier_thresholds': [50, 150],
        'score_multipliers': {'total': 1.0, 'age': 0.25, 'chapter': 0.5},
        'character_tags': {'scout': ['age', 'chapter']},
        'rates': [{'minimum_score': 0, 'wallet_per_minute': {'credits': 1}},
                  {'minimum_score': 1, 'wallet_per_minute': {'credits': 2}}],
        'boosts': {'alpha': {'health': {'tier_requirements': [1, 2], 'costs': [
            {'credits': 30}, {'credits': 50}]}, 'focus': {
            'tier_requirements': [2], 'costs': [{'credits': 100}]}}}}
    return catalog


class SeasonalBoostTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'season.sqlite'
        self.now = 2000
        self.catalog = sample_catalog()
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'Player A'})

    def command(self, key, command, **parameters):
        return self.service.execute('a', {'command': command,
                                          'idempotency_key': key, 'parameters': parameters})

    def test_pure_read_active_state_initialize_and_current_roster_score(self):
        before = self.service.get_player('a')
        state = self.service.get_seasonal_boost('a')
        self.assertIsNone(state['state'])
        self.assertTrue(state['available'])
        self.assertEqual(state['pending_wallet'], {})
        self.assertEqual(state['scores']['tcp'],
                         sum(character['power'] for character in before['characters'].values()))
        self.assertEqual(self.service.get_player('a'), before)
        first = self.command('start', 'seasonal.refresh')
        self.assertEqual(first['state']['initialized_at'], self.now)
        self.assertEqual(first['state']['boosts'], [])
        self.assertEqual(first['claimed_wallet'], {})
        self.assertEqual(self.command('start', 'seasonal.refresh'), first)
        self.assertEqual(self.service.get_seasonal_boost('a')['state'], first['state'])
        self.assertEqual(self.command('view-again', 'seasonal.refresh')['claimed_wallet'], {})

    def test_floor_minutes_rate_cap_and_source_period_end(self):
        first = self.command('start', 'seasonal.refresh')
        initial = first['player']['wallet']['credits']
        self.now += 119
        self.assertEqual(self.service.get_seasonal_boost('a')['pending_minutes'], 1)
        self.assertEqual(self.service.get_seasonal_boost('a')['pending_wallet'], {'credits': 2})
        self.now += 11
        claim = self.command('earned', 'seasonal.claim')
        self.assertEqual(claim['claimed_wallet'], {'credits': 4})
        self.assertEqual(claim['state']['last_claimed_at'], self.now)
        self.assertEqual(claim['player']['wallet']['credits'], initial + 4)
        self.assertEqual(claim['pending_wallet'], {})
        self.now += 25 * 60
        self.assertEqual(self.service.get_seasonal_boost('a')['pending_minutes'], 20)
        self.assertEqual(self.command('cap', 'seasonal.claim')['claimed_wallet'], {'credits': 40})
        self.now = 40000
        self.assertFalse(self.service.get_seasonal_boost('a')['available'])
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('expired', 'seasonal.claim')
        self.assertEqual(self.service.get_player('a'), before)

    def test_concurrent_claim_replay_restart_and_throttle_roll_back(self):
        self.command('start', 'seasonal.refresh')
        self.now += 125
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.command('same-claim', 'seasonal.claim'), range(4)))
        self.assertEqual(responses, [responses[0]] * 4)
        self.assertEqual(responses[0]['claimed_wallet'], {'credits': 4})
        before = self.service.get_player('a')
        with self.assertRaises(DomainError) as error:
            self.command('different', 'seasonal.claim')
        self.assertEqual(error.exception.code, 'seasonal_claim_throttled')
        self.assertEqual(self.service.get_player('a'), before)
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now + 15)
        self.assertEqual(self.command('same-claim', 'seasonal.claim'), responses[0])
        self.now += 60
        again = self.command('new-day', 'seasonal.claim')
        self.assertEqual(again['claimed_wallet'], {'credits': 2})
        self.assertEqual(again['state']['boosts'], [])

    def test_atomic_paid_boost_tier_expected_level_cap_and_history(self):
        self.command('start', 'seasonal.refresh')
        original = self.service.get_player('a')
        with self.assertRaises(DomainError) as error:
            self.command('wrong', 'seasonal.buy', trait_id='alpha', stat_id='health', expected_level=1)
        self.assertEqual(error.exception.code, 'seasonal_stale_level')
        self.assertEqual(self.service.get_player('a'), original)
        first = self.command('one', 'seasonal.buy', trait_id='alpha', stat_id='health', expected_level=0)
        self.assertEqual(first['purchased'], {'trait_id': 'alpha', 'stat_id': 'health', 'level': 1, 'at': self.now})
        self.assertEqual(first['state']['levels'], {'alpha:health': 1})
        self.assertEqual(first['player']['wallet']['credits'], original['wallet']['credits'] - 30)
        self.assertEqual(self.command('one', 'seasonal.buy', trait_id='alpha', stat_id='health', expected_level=0), first)
        second = self.command('two', 'seasonal.buy', trait_id='alpha', stat_id='health', expected_level=1)
        self.assertEqual(second['state']['levels'], {'alpha:health': 2})
        self.assertEqual([row['level'] for row in second['state']['boosts']], [1, 2])
        before = self.service.get_player('a')
        with self.assertRaises(DomainError) as error:
            self.command('over', 'seasonal.buy', trait_id='alpha', stat_id='health', expected_level=2)
        self.assertEqual(error.exception.code, 'seasonal_boost_cap')
        self.assertEqual(self.service.get_player('a'), before)
        with self.assertRaises(DomainError):
            self.command('insufficient', 'seasonal.buy', trait_id='alpha', stat_id='focus', expected_level=0)
        self.assertEqual(self.service.get_player('a'), before)

    def test_tier_required_before_spending(self):
        self.catalog = sample_catalog()
        self.catalog['starting_wallet']['season_xp'] = 0
        with tempfile.TemporaryDirectory() as temp:
            service = GameService(Path(temp) / 'new.sqlite', self.catalog, clock=lambda: self.now)
            service.create_player({'player_id': 'low', 'display_name': 'Low'})
            before = service.get_player('low')
            with self.assertRaises(DomainError) as error:
                service.execute('low', {'command': 'seasonal.buy', 'idempotency_key': 'x',
                                        'parameters': {'trait_id': 'alpha', 'stat_id': 'focus', 'expected_level': 0}})
            self.assertEqual(error.exception.code, 'seasonal_tier_required')
            self.assertEqual(service.get_player('low'), before)

    def test_catalog_rejects_unknown_currency_thresholds_and_nan(self):
        for mutate in (
            lambda p: p['rates'][0].update(wallet_per_minute={'unknown': 5}),
            lambda p: p.update(tier_thresholds=[200, 100]),
            lambda p: p['score_multipliers'].update(total=float('nan')),
            lambda p: p['boosts']['alpha']['health'].update(costs=[{'credits': 1}]),
            lambda p: p['boosts']['alpha']['health'].update(tier_requirements=[0, 1]),
            lambda p: p['character_tags'].update(unknown=['age']),
            lambda p: p.update(maximum_accrual_minutes=True),
        ):
            catalog = sample_catalog()
            mutate(catalog['seasonal_boost'])
            with self.assertRaises(DomainError):
                validate_catalog(catalog)


if __name__ == '__main__':
    unittest.main()

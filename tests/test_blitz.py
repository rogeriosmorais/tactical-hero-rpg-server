"""Local Blitz authority, public ledger, private-combat activation boundary."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


def blitz_catalog():
    catalog = deepcopy(DEMO_CATALOG)
    catalog['starting_wallet'].update(charges=40, cores=50)
    catalog['blitz'] = {
        'required_level': 1, 'maximum_tier': 8,
        'matchup_expiry_seconds': 3600,
        'recharge': {'charges_currency_id': 'charges', 'premium_currency_id': 'cores',
                     'cost_per_character': 5},
        'events': {'round-one': {
            'starts_at': 1000, 'ends_at': 200000, 'cooldown_seconds': 300,
            'win_points': 100, 'lose_points': 5,
            'win_rewards': {'wallet': {'credits': 20}},
            'lose_rewards': {'wallet': {'credits': 2}}}}}
    return catalog


class BlitzTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'blitz.sqlite'
        self.now = 3000
        self.catalog = blitz_catalog()
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'owner', 'display_name': 'Owner'})

    def command(self, key, command, **parameters):
        return self.service.execute('owner', {
            'command': command, 'idempotency_key': key, 'parameters': parameters})

    def match(self, key='list', team=None, force=False):
        return self.command(key, 'blitz.refresh',
                            event_id='round-one', team=team or ['scout'], force=force)['matchup']

    def begin(self, key='begin', team=None, opponent_index=0):
        team = team or ['scout']
        match = self.match(key='choices-' + key, team=team)
        return self.command(key, 'blitz.begin',
                            event_id='round-one', team=team,
                            opponent_id=match['opponents'][opponent_index]['opponent_id'])['battle']

    def test_source_gates_pure_view_and_saved_matchup_after_restart(self):
        initial = self.service.get_player('owner')
        self.assertIsNone(self.service.get_blitz('owner')['state'])
        self.assertTrue(self.service.get_blitz('owner')['available'])
        self.assertEqual(self.service.get_player('owner'), initial)
        match = self.match()
        self.assertEqual(len(match['opponents']), 3)
        self.assertEqual(match['team'], ['scout'])
        self.assertEqual(self.match('again'), match)
        self.assertNotEqual(self.match('refresh', force=True), match)
        prior = self.service.get_player('owner')
        self.assertEqual(self.service.get_blitz('owner')['state'], prior['blitz'])
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.service.get_blitz('owner')['state'], prior['blitz'])
        self.assertNotIn('Wire', str(prior['blitz']))

    def test_actual_won_battle_points_rewards_cooldown_and_exact_replay(self):
        match = self.match()
        before = self.service.get_player('owner')
        battle = self.command('begin', 'blitz.begin',
                              event_id='round-one', team=['scout'],
                              opponent_id=match['opponents'][1]['opponent_id'])['battle']
        self.assertEqual(battle['kind'], 'blitz')
        self.assertEqual(battle['team'], ['scout'])
        self.assertEqual(self.service.get_blitz('owner')['state']['cooldowns']['scout'], self.now + 300)
        with self.assertRaises(DomainError):
            self.command('begin-other', 'blitz.begin',
                         event_id='round-one', team=['scout'],
                         opponent_id=match['opponents'][0]['opponent_id'])
        won = self.command('win', 'blitz.complete',
                           battle_id=battle['battle_id'], outcome='victory', stars=3,
                           duration_seconds=8)
        self.assertEqual(won['points'], 100)
        self.assertEqual(won['tier'], 2)
        self.assertEqual(won['blitz']['events']['round-one']['wins'], 1)
        self.assertEqual(won['player']['wallet']['credits'],
                         before['wallet']['credits'] + 20)
        self.assertEqual(self.service.get_battle('owner', battle['battle_id'])['status'], 'complete')
        self.assertEqual(self.command('win', 'blitz.complete',
            battle_id=battle['battle_id'], outcome='victory', stars=3,
            duration_seconds=8), won)
        with self.assertRaises(DomainError):
            self.command('forged', 'blitz.complete', battle_id=battle['battle_id'],
                         outcome='victory', stars=3, duration_seconds=8)
        self.assertEqual(self.service.get_player('owner'), won['player'])
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.command('win', 'blitz.complete',
            battle_id=battle['battle_id'], outcome='victory', stars=3,
            duration_seconds=8), won)

    def test_charges_and_premium_recharge_cost_atomic_and_fallback(self):
        battle = self.begin()
        self.command('loss', 'blitz.complete', battle_id=battle['battle_id'],
                     outcome='defeat', stars=0, duration_seconds=15)
        before = self.service.get_player('owner')
        with self.assertRaises(DomainError):
            self.command('wrong-recharge', 'blitz.recharge',
                         characters=['scout'], expected_cost=6)
        self.assertEqual(self.service.get_player('owner'), before)
        refill = self.command('recharge', 'blitz.recharge',
                              characters=['scout'], expected_cost=5)
        self.assertEqual(refill['currency_id'], 'charges')
        self.assertEqual(refill['player']['wallet']['charges'],
                         before['wallet']['charges'] - 5)
        self.assertNotIn('scout', refill['blitz']['cooldowns'])
        self.assertEqual(self.command('recharge', 'blitz.recharge',
                                     characters=['scout'], expected_cost=5), refill)
        # Start a fresh owned battle after the paid cooldown reset.
        self.begin('next-begin')
        with self.service.transaction() as db:
            player = self.service._player(db, 'owner')
            player['wallet']['charges'] = 0
            from game_server.service import canonical
            db.execute('UPDATE players SET data=? WHERE id=?',
                       (canonical(player), 'owner'))
        fallback = self.command('fallback', 'blitz.recharge', characters=['scout'])
        self.assertEqual(fallback['currency_id'], 'cores')
        self.assertEqual(fallback['cost'], 5)
        with self.assertRaises(DomainError):
            self.command('no-cooldown', 'blitz.recharge', characters=['scout'])

    def test_defeat_streak_tier_and_cannot_directly_forge_or_duplicate(self):
        self.match()
        initial = self.service.get_player('owner')
        with self.assertRaises(DomainError):
            self.command('no-battle', 'blitz.complete', battle_id='123', outcome='victory',
                         stars=3, duration_seconds=15)
        self.assertEqual(self.service.get_player('owner'), initial)
        battle = self.begin()
        for wrong in ({'outcome': 'victory', 'stars': 0},
                      {'outcome': 'defeat', 'stars': 3},
                      {'outcome': 'draw', 'stars': 1}):
            with self.assertRaises(DomainError):
                self.command('invalid-' + wrong['outcome'] + str(wrong['stars']), 'blitz.complete',
                             battle_id=battle['battle_id'], duration_seconds=15, **wrong)
        lost = self.command('lose', 'blitz.complete',
                            battle_id=battle['battle_id'], outcome='defeat',
                            stars=0, duration_seconds=15)
        self.assertEqual(lost['tier'], 1)
        self.assertEqual(lost['streak'], 0)
        self.assertEqual(lost['points'], 5)
        self.assertEqual(lost['player']['wallet']['credits'],
                         initial['wallet']['credits'] + 2)
        self.assertEqual(lost['blitz']['events']['round-one']['battles'], 1)

    def test_replay_parallel_begin_no_extra_cooldown_and_other_account_isolated(self):
        match = self.match()
        key = match['opponents'][0]['opponent_id']
        self.service.create_player({'player_id': 'other', 'display_name': 'Other'})
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.command('parallel', 'blitz.begin',
                event_id='round-one', team=['scout'], opponent_id=key), range(5)))
        self.assertEqual(results, [results[0]] * 5)
        self.assertIsNone(self.service.get_blitz('other')['state'])
        self.assertEqual(self.service.get_battle('owner', results[0]['battle']['battle_id'])['status'], 'active')
        with self.assertRaises(DomainError):
            self.service.get_battle('other', results[0]['battle']['battle_id'])

    def test_expiration_team_signature_and_bad_catalog_rejection(self):
        match = self.match()
        self.now += 3600
        before = self.service.get_player('owner')
        with self.assertRaises(DomainError):
            self.command('expired', 'blitz.begin', event_id='round-one',
                         team=['scout'], opponent_id=match['opponents'][0]['opponent_id'])
        self.assertEqual(self.service.get_player('owner'), before)
        self.assertNotEqual(self.match('new-expiration'), match)
        for mutate in (
            lambda c: c['blitz']['events']['round-one'].update(ends_at=1),
            lambda c: c['blitz']['recharge'].update(charges_currency_id='unknown'),
            lambda c: c['blitz'].update(matchup_expiry_seconds=True),
            lambda c: c['blitz']['events']['round-one'].update(win_points=-2),
            lambda c: c['blitz']['events']['round-one'].update(win_rewards={'wallet': {'unknown': 5}}),
        ):
            catalog = blitz_catalog()
            mutate(catalog)
            with self.assertRaises(DomainError):
                validate_catalog(catalog)

    def test_weekly_recurring_event_is_one_catalog_and_distinct_instance_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = blitz_catalog()
            event = catalog['blitz']['events']['round-one']
            event.pop('starts_at')
            event.pop('ends_at')
            event.update(period_seconds=604800, anchor=0)
            now = [3000]
            service = GameService(Path(folder) / 'weekly.sqlite', catalog, clock=lambda: now[0])
            service.create_player({'player_id': 'weekly', 'display_name': 'Weekly'})
            def command(key, name, **parameters):
                return service.execute('weekly', {'command': name,
                    'idempotency_key': key, 'parameters': parameters})
            first = command('list-a', 'blitz.refresh', event_id='round-one', team=['scout'])['matchup']
            self.assertEqual(first['occurrence_id'], 'round-one:0')
            issued = command('begin-a', 'blitz.begin', event_id='round-one',
                team=['scout'], opponent_id=first['opponents'][0]['opponent_id'])['battle']
            command('complete-a', 'blitz.complete', battle_id=issued['battle_id'],
                    outcome='victory', stars=1, duration_seconds=11)
            old_state = service.get_blitz('weekly')['state']
            self.assertEqual(old_state['events']['round-one:0']['points'], 100)
            now[0] = 604800 + 3000
            fresh = command('list-b', 'blitz.refresh', event_id='round-one', team=['scout'])['matchup']
            self.assertEqual(fresh['occurrence_id'], 'round-one:604800')
            self.assertNotEqual(first['opponents'], fresh['opponents'])
            self.assertEqual(service.get_blitz('weekly')['events']['round-one']['starts_at'], 604800)
            self.assertEqual(service.get_blitz('weekly')['state']['events']['round-one:0']['points'], 100)
            next_battle = command('begin-b', 'blitz.begin', event_id='round-one',
                team=['scout'], opponent_id=fresh['opponents'][0]['opponent_id'])['battle']
            second = command('complete-b', 'blitz.complete', battle_id=next_battle['battle_id'],
                             outcome='victory', stars=3, duration_seconds=9)
            self.assertEqual(second['points'], 100)
            self.assertEqual(second['blitz']['events']['round-one:604800']['wins'], 1)
            self.assertEqual(second['blitz']['events']['round-one:0']['wins'], 1)
            service = GameService(Path(folder) / 'weekly.sqlite', catalog, clock=lambda: now[0])
            self.assertEqual(service.get_blitz('weekly')['state'], second['blitz'])


if __name__ == '__main__':
    unittest.main()

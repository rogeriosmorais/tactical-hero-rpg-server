"""Authoritative local ranked Arena day, defense and sourced daily payouts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService, canonical


def arena_catalog():
    catalog = deepcopy(DEMO_CATALOG)
    catalog['maximum_player_level'] = 20
    catalog['starting_wallet']['credits'] = 40
    catalog['starting_characters'] = {'scout': 1, 'guardian': 1}
    catalog['ranked_arena'] = {'required_level': 2, 'maximum_rank': 10000,
        'attacks_per_day': 5, 'payout_ranges': [
            {'min_rank': 1, 'max_rank': 100, 'tier': 0, 'rewards': {'wallet': {'credits': 50}}},
            {'min_rank': 101, 'max_rank': 10000, 'tier': 9,
             'rewards': {'wallet': {'credits': 7}, 'inventory': {'arena-token': 2}}}]}
    return catalog


class RankedArenaTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'arena.sqlite'
        self.now = 1_000_000
        self.catalog = arena_catalog()
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'arena-player', 'display_name': 'Arena Player'})

    def command(self, name, key, **parameters):
        return self.service.execute('arena-player', {'command': name, 'idempotency_key': key, 'parameters': parameters})

    def earn(self):
        self.command('mission.begin', 'mission', mission_id='training.1', team=['scout'])
        self.command('mission.complete', 'complete', mission_id='training.1', outcome='victory',
                     battle_id=self.service.get_player('arena-player')['pending_battle']['battle_id'])

    def level(self):
        with self.service.transaction() as db:
            player = self.service._player(db, 'arena-player')
            player['level'] = 2
            player['experience'] = 100
            db.execute('UPDATE players SET data=? WHERE id=?', (canonical(player), 'arena-player'))

    def test_earned_initialization_stable_neutral_candidates_and_pure_read(self):
        self.assertFalse(self.service.get_ranked_arena('arena-player')['available'])
        before = self.service.get_player('arena-player')
        with self.assertRaises(DomainError):
            self.command('arena.refresh', 'unearned')
        self.assertEqual(self.service.get_player('arena-player'), before)
        self.level()
        self.assertTrue(self.service.get_ranked_arena('arena-player')['needs_refresh'])
        first = self.command('arena.refresh', 'initial')
        arena = first['arena']
        self.assertEqual((arena['rank'], arena['attacks'], arena['day']), (10000, 5, self.now // 86400))
        self.assertEqual(arena['payout_history'], [])
        self.assertEqual(len(arena['opponents']), 3)
        self.assertEqual([row['rank'] for row in arena['opponents']], [9600, 9000, 8000])
        self.assertTrue(all(set(row['team']) <= {'scout', 'guardian'} for row in arena['opponents']))
        self.assertNotIn('Wire', str(arena))
        saved = self.service.get_player('arena-player')
        self.assertEqual(self.service.get_ranked_arena('arena-player')['arena'], arena)
        self.assertEqual(self.service.get_player('arena-player'), saved)
        self.assertEqual(self.command('arena.refresh', 'initial'), first)
        self.assertEqual(self.service.get_player('arena-player'), saved)

    def test_daily_payout_once_after_gap_concurrent_and_restart(self):
        self.level()
        self.command('arena.refresh', 'initial')
        self.now += 5 * 86400
        self.assertTrue(self.service.get_ranked_arena('arena-player')['needs_refresh'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.command('arena.refresh', 'day-five'), range(4)))
        self.assertEqual(responses, [responses[0]] * 4)
        row = responses[0]
        self.assertEqual(row['payouts'], [{'day': self.now // 86400, 'rank': 10000, 'tier': 9,
                                          'rewards': {'wallet': {'credits': 7}, 'inventory': {'arena-token': 2}}}])
        self.assertEqual(row['player']['wallet']['credits'], 47)
        self.assertEqual(row['player']['inventory']['arena-token'], 2)
        self.assertEqual(len(row['arena']['payout_history']), 1)
        self.assertEqual(self.command('arena.refresh', 'same-day')['payouts'], [])
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now + 100)
        self.assertEqual(self.command('arena.refresh', 'day-five'), row)
        self.now += 86400
        self.assertEqual(self.command('arena.refresh', 'sixth')['player']['wallet']['credits'], 54)

    def test_defense_mutation_replaces_candidates_and_replays_identically(self):
        self.level()
        first = self.command('arena.refresh', 'first')['arena']
        selected = self.command('arena.set_defense', 'defense', team=['guardian'])
        self.assertEqual(selected['arena']['defense'], ['guardian'])
        self.assertEqual(selected['arena']['nonce'], first['nonce'] + 1)
        self.assertNotEqual(selected['arena']['opponents'], first['opponents'])
        self.assertEqual(self.command('arena.set_defense', 'defense', team=['guardian']), selected)
        with self.assertRaises(DomainError) as error:
            self.command('arena.set_defense', 'defense', team=['scout'])
        self.assertEqual(error.exception.code, 'idempotency_conflict')
        before = self.service.get_player('arena-player')
        for team in ([], ['scout', 'scout'], ['unknown'], [True]):
            with self.assertRaises(DomainError):
                self.command('arena.set_defense', 'invalid', team=team)
            self.assertEqual(self.service.get_player('arena-player'), before)

    def test_second_profile_isolated_and_no_combat_reward_forgery(self):
        self.level()
        self.service.create_player({'player_id': 'other', 'display_name': 'Other'})
        self.command('arena.refresh', 'start')
        self.assertIsNone(self.service.get_ranked_arena('other')['arena'])
        before = self.service.get_player('arena-player')
        with self.assertRaises(DomainError):
            self.command('arena.complete', 'forged', win=True, opponent_id='800000001')
        self.assertEqual(before, self.service.get_player('arena-player'))

    def test_catalog_rejects_bad_range_gaps_overlaps_and_unknown_currency(self):
        for mutate in (
            lambda p: p['payout_ranges'][1].update(min_rank=102),
            lambda p: p['payout_ranges'][0].update(max_rank=101),
            lambda p: p['payout_ranges'][1].update(max_rank=9000),
            lambda p: p.update(attacks_per_day=True),
            lambda p: p['payout_ranges'][0].update(rewards={'wallet': {'missing': 1}}),
        ):
            catalog = arena_catalog()
            mutate(catalog['ranked_arena'])
            with self.assertRaises(DomainError):
                validate_catalog(catalog)

    def test_earned_native_adjudicated_win_moves_rank_once_without_direct_rewards(self):
        self.level()
        opponent = self.command('arena.refresh', 'opponents')['arena']['opponents'][0]
        before = self.service.get_player('arena-player')
        issued = self.command('arena.begin', 'begin',
                              team=['scout'], opponent_id=opponent['opponent_id'])
        battle = issued['battle']
        self.assertEqual(battle['kind'], 'ranked_arena')
        self.assertEqual(battle['opponent'], opponent)
        self.assertEqual(issued['arena']['attacks'], 4)
        with self.assertRaises(DomainError):
            self.command('arena.begin', 'parallel-other',
                         team=['scout'], opponent_id=opponent['opponent_id'])
        won = self.command('arena.complete', 'won', battle_id=battle['battle_id'],
                           outcome='victory', stars=3, duration_seconds=22)
        self.assertEqual((won['old_rank'], won['new_rank']), (10000, opponent['rank']))
        self.assertEqual((won['arena']['victories'], won['arena']['defeats']), (1, 0))
        self.assertEqual(won['arena']['history'][0]['rank_before'], 10000)
        self.assertEqual(won['arena']['history'][0]['status'], 'won')
        self.assertEqual(won['arena']['opponents'], [])
        self.assertEqual(won['player']['wallet'], before['wallet'])
        self.assertEqual(self.service.get_battle('arena-player', battle['battle_id'])['status'], 'complete')
        self.assertEqual(self.command('arena.complete', 'won', battle_id=battle['battle_id'],
                                     outcome='victory', stars=3, duration_seconds=22), won)
        with self.assertRaises(DomainError):
            self.command('arena.complete', 'unearned-duplicate',
                         battle_id=battle['battle_id'], outcome='victory',
                         stars=3, duration_seconds=22)
        self.assertEqual(self.service.get_player('arena-player'), won['player'])

    def test_defeats_consume_attacks_and_defense_edits_cannot_refill_daily_cap(self):
        self.level()
        for index in range(5):
            opponent = self.command('arena.refresh', 'opponents-' + str(index))['arena']['opponents'][0]
            battle = self.command('arena.begin', 'begin-' + str(index),
                team=['scout'], opponent_id=opponent['opponent_id'])['battle']
            lost = self.command('arena.complete', 'lose-' + str(index),
                battle_id=battle['battle_id'], outcome='defeat',
                stars=0, duration_seconds=30)
            self.assertEqual(lost['arena']['attacks'], 4 - index)
            self.assertEqual(lost['arena']['rank'], 10000)
            self.assertEqual(lost['arena']['defeats'], index + 1)
        self.command('arena.set_defense', 'new-defense', team=['guardian'])
        opponents = self.command('arena.refresh', 'exhausted')['arena']['opponents']
        before = self.service.get_player('arena-player')
        with self.assertRaises(DomainError) as error:
            self.command('arena.begin', 'sixth', team=['scout'],
                         opponent_id=opponents[0]['opponent_id'])
        self.assertEqual(error.exception.code, 'arena_attacks_exhausted')
        self.assertEqual(self.service.get_player('arena-player'), before)

    def test_midnight_battle_settles_prior_rank_payout_before_new_rank(self):
        self.level()
        opponent = self.command('arena.refresh', 'initial-match')['arena']['opponents'][0]
        battle = self.command('arena.begin', 'prior-day-begin',
            team=['scout'], opponent_id=opponent['opponent_id'])['battle']
        self.now += 86400
        settled = self.command('arena.complete', 'prior-day-win',
            battle_id=battle['battle_id'], outcome='victory',
            stars=2, duration_seconds=15)
        self.assertEqual(settled['payouts'][0]['rank'], 10000)
        self.assertEqual(settled['payouts'][0]['day'], self.now // 86400)
        self.assertEqual(settled['new_rank'], opponent['rank'])
        self.assertEqual(settled['arena']['attacks'], 5)
        self.assertEqual(settled['player']['wallet']['credits'], 47)
        self.assertEqual(len(settled['arena']['payout_history']), 1)
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.command('arena.complete', 'prior-day-win',
            battle_id=battle['battle_id'], outcome='victory',
            stars=2, duration_seconds=15), settled)
        fresh = self.command('arena.refresh', 'day-refreshed')
        self.assertEqual(fresh['payouts'], [])
        self.assertEqual(fresh['arena']['attacks'], 5)
        self.assertEqual(fresh['player']['wallet']['credits'], 47)


if __name__ == '__main__':
    unittest.main()

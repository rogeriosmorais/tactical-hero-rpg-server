"""Real local membership, earned placement, settings, search and profile isolation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


def alliance_rules():
    return {'required_level': 2, 'name_limits': [4, 32], 'tag_limits': [1, 8], 'message_limits': [0, 255],
        'visibility': {'open': True, 'closed': False}, 'default_visibility': 'open', 'maximum_members': 24,
        'leave_on_create': True, 'rejoin_cooldown_seconds': 86400, 'ranks': ['owner', 'officer', 'member'],
        'inactive_days': [2, 7, 14], 'experience_per_level': 10000, 'maximum_level': 999,
        'bootstrap': {'name': 'Local Team', 'tag': 'LOCAL', 'message': 'Local team', 'visibility_id': 'open', 'settings': {}}}


class AllianceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.now = 1000000
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['alliances'] = alliance_rules()
        self.catalog['missions']['training.1']['rewards'] = {'experience': 100}
        self.catalog['missions']['training.1']['first_clear_rewards'] = {}
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def command(self, command_name, key, player='a', **parameters):
        return self.service.execute(player, {'command': command_name, 'idempotency_key': key, 'parameters': parameters})

    def earn(self, player='a'):
        battle = self.command('mission.begin', 'begin', player, mission_id='training.1', team=['scout'])['battle']
        return self.command('mission.complete', 'win', player, mission_id='training.1', battle_id=battle['battle_id'], outcome='victory', stars=3)

    def create(self, name='Second Team', key='create'):
        return self.command('alliance.create', key, name=name, tag='TEST')

    def test_earned_placement_has_one_actual_member_and_reads_do_not_grant_or_mutate(self):
        before = self.service.get_player('a')
        self.assertFalse(self.service.get_alliances('a')['available'])
        self.assertEqual(self.service.get_player('a'), before)
        with self.assertRaises(DomainError):
            self.create()
        earned = self.earn()
        alliance = earned['alliance_state']['current']
        self.assertEqual(alliance['member_count'], 1)
        self.assertEqual(alliance['members'], [{'player_id': 'a', 'display_name': 'A', 'level': 2, 'rank': 'owner', 'joined_at': self.now}])
        self.assertEqual(earned['player']['wallet']['credits'], before['wallet']['credits'])
        self.assertEqual(earned['player']['inventory'], before['inventory'])
        self.assertNotIn('_transaction_activity', earned['player'])
        before = self.service.get_player('a')
        self.service.get_alliances('a')
        self.service.search_alliances('a', {})
        self.assertEqual(self.service.get_player('a'), before)

    def test_create_concurrent_retry_settings_and_restart_preserve_economy(self):
        self.earn()
        before = self.service.get_player('a')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.create(), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        result = results[0]
        self.assertEqual(len(result['alliance_state']['alliances']), 2)
        self.assertEqual(result['player']['wallet'], before['wallet'])
        self.assertEqual(result['player']['inventory'], before['inventory'])
        aid = result['alliance_state']['current']['alliance_id']
        updated = self.command('alliance.update', 'edit', alliance_id=aid, name='Edited Team', tag='EDIT',
            message='Raid testing', settings={'emblem': 2, 'raid_rank': 'officer', 'churn_days': {'member': 7}})
        current = updated['alliance_state']['current']
        self.assertEqual(current['settings']['raid_rank'], 'officer')
        self.assertTrue(current['leadership_seen'])
        partial = self.command('alliance.update', 'partial', alliance_id=aid, name='Edited Team', tag='EDIT')
        self.assertEqual(partial['alliance_state']['current']['message'], 'Raid testing')
        restarted = GameService(self.path, self.catalog)
        self.assertEqual(restarted.execute('a', {'command': 'alliance.create', 'idempotency_key': 'create',
            'parameters': {'name': 'Second Team', 'tag': 'TEST'}}), result)

    def test_leave_rejoin_switch_flag_cooldown_and_no_automatic_rejoin(self):
        first = self.earn()['alliance_state']['current']['alliance_id']
        second = self.create()['alliance_state']['current']['alliance_id']
        with self.assertRaises(DomainError):
            self.command('alliance.join', 'deny-switch', alliance_id=first, allow_switch=False)
        self.command('alliance.leave', 'leave', alliance_id=second)
        self.command('character.train', 'unrelated', character_id='scout', levels=1)
        self.assertIsNone(self.service.get_alliances('a')['current'])
        self.now += 10
        joined = self.command('alliance.join', 'join', alliance_id=first, allow_switch=False)
        self.assertEqual(joined['alliance_state']['donation_cooldown_until'], self.now + 86400)
        self.assertEqual(joined['alliance_state']['current']['member_count'], 1)
        empty = next(row for row in joined['alliance_state']['alliances'] if row['alliance_id'] == second)
        self.assertEqual(empty['members'], [])

    def test_search_filters_known_teams_and_other_profiles_are_isolated(self):
        self.earn()
        second = self.create()['alliance_state']['current']['alliance_id']
        self.assertEqual(self.service.search_alliances('a', {'name': 'SECOND TEAM', 'exact': True})['alliances'][0]['alliance_id'], second)
        self.assertEqual(self.service.search_alliances('a', {'name': 'second', 'exact': True})['alliances'], [])
        self.assertEqual(self.service.search_alliances('a', {'tag': 'Team'})['alliances'], [])
        self.service.create_player({'player_id': 'b', 'display_name': 'B'})
        self.earn('b')
        self.command('alliance.create', 'foreign', 'b', name='Foreign Team', tag='OTHER')
        self.assertEqual(self.service.search_alliances('a', {'name': 'Foreign'})['alliances'], [])
        self.assertEqual(self.service.get_alliances('b')['current']['members'][0]['player_id'], 'b')

    def test_invalid_names_settings_ids_and_closed_teams_are_atomic(self):
        self.earn()
        before = self.service.get_player('a')
        for index, name in enumerate(('', 'bad/name', 'bad\nname', 'x' * 33, 'LOCAL TEAM')):
            with self.assertRaises(DomainError):
                self.create(name, key='invalid-name-' + str(index))
        self.assertEqual(self.service.get_player('a'), before)
        for filters in ({'limit': True}, {'limit': 0}, {'exact': 'true'}, {'name': []}, {'visibility_id': 'missing'}):
            with self.assertRaises(DomainError):
                self.service.search_alliances('a', filters)
        closed = self.command('alliance.create', 'closed', name='Closed Team', tag='C', visibility_id='closed')['alliance_state']['current']['alliance_id']
        self.command('alliance.leave', 'leave', alliance_id=closed)
        with self.assertRaises(DomainError):
            self.command('alliance.join', 'closed-join', alliance_id=closed, allow_switch=False)
        with self.assertRaises(DomainError):
            self.command('alliance.join', 'unknown', alliance_id='999', allow_switch=False)

    def test_calendar_level_reward_creates_real_membership_before_later_board_gate(self):
        catalog = deepcopy(self.catalog)
        catalog['calendars'] = {
            'level': {'mode': 'finite', 'order': 0, 'required_level': 1, 'rewards': [{'experience': 100}]},
            'member': {'mode': 'repeat', 'order': 1, 'required_level': 1, 'requires_alliance': True,
                       'rewards': [{'wallet': {'credits': 10}}]}}
        self.service = GameService(Path(self.temp.name) / 'calendar.sqlite', catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        result = self.command('calendar.advance', 'first')
        self.assertEqual(result['quantity'], 2)
        self.assertEqual(result['player']['wallet']['credits'], 110)
        self.command('alliance.leave', 'leave', alliance_id=result['alliance_state']['current']['alliance_id'])
        self.now += 86400
        self.assertFalse(self.service.get_calendars('a')['boards']['member']['available'])
        self.assertEqual(self.command('calendar.advance', 'left')['quantity'], 0)

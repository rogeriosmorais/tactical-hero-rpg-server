"""Migrated original campaign/challenge squad-eligibility authority."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


class MissionEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['missions']['training.1']['eligibility'] = {
            'scopes': [
                {'minimum_commander_level': 1, 'must_include': ['scout'],
                 'each_character': {'character_ids': ['scout', 'guardian'],
                                    'minimum_level': 1}},
                {'any_character': [{'character_ids': ['guardian']}],
                 'minimum_matching': 1},
            ],
        }
        self.catalog['missions']['training.2']['eligibility'] = {
            'minimum_mission_stars': {'training.1': 3},
            'must_include': ['guardian'],
        }
        self.service = GameService(Path(self.temp.name) / 'application.db',
                                   validate_catalog(self.catalog))
        self.service.create_player({'player_id': 'owner', 'display_name': 'Owner'})

    def tearDown(self):
        self.temp.cleanup()

    def command(self, command, key, **params):
        return self.service.execute('owner', {'command': command, 'idempotency_key': key,
                                              'parameters': params})

    def test_all_original_scopes_are_authorized_by_server_before_cost(self):
        before = self.service.get_player('owner')
        for index, team in enumerate((['scout'], ['guardian'])):
            with self.subTest(team=team), self.assertRaises(DomainError):
                self.command('mission.begin', f'bad-team-{index}',
                             mission_id='training.1', team=team)
        self.assertEqual(self.service.get_player('owner'), before)
        started = self.command('mission.begin', 'issued', mission_id='training.1',
                               team=['scout', 'guardian'])
        self.assertEqual(started['player']['wallet']['energy'], before['wallet']['energy'] - 2)
        self.assertEqual(self.command('mission.begin', 'issued', mission_id='training.1',
                                      team=['scout', 'guardian']), started)

    def test_minimum_stars_and_required_heroes_survive_transactions_and_restart(self):
        for stars in (1, 3):
            battle = self.command('mission.begin', f'begin-{stars}',
                                  mission_id='training.1', team=['scout', 'guardian'])
            self.command('mission.complete', f'win-{stars}',
                         battle_id=battle['battle']['battle_id'],
                         mission_id='training.1', outcome='victory', stars=stars)
            if stars == 1:
                before = self.service.get_player('owner')
                with self.assertRaises(DomainError):
                    self.command('mission.begin', 'unearned-stars',
                                 mission_id='training.2', team=['guardian'])
                self.assertEqual(self.service.get_player('owner'), before)
        self.service = GameService(Path(self.temp.name) / 'application.db', self.catalog)
        before = self.service.get_player('owner')
        with self.assertRaises(DomainError):
            self.command('mission.begin', 'missing-hero', mission_id='training.2',
                         team=['scout'])
        self.assertEqual(self.service.get_player('owner'), before)
        issued = self.command('mission.begin', 'correct', mission_id='training.2',
                              team=['guardian'])
        self.assertEqual(issued['player']['wallet']['energy'],
                         before['wallet']['energy'] - 3)

    def test_validation_rejects_missing_or_ambiguous_eligibility_rules(self):
        invalid = (
            {'must_include': ['missing']},
            {'scopes': [{}, {}, {}, {}]},
            {'any_character': []},
            {'minimum_matching': 1},
            {'minimum_mission_stars': {'training.2': 4}},
            {'each_character': {'character_ids': ['scout', 'scout']}},
            {'each_character': {'character_ids': ['scout'], 'excluded_character_ids': ['scout']}},
        )
        for definition in invalid:
            catalog = deepcopy(self.catalog)
            catalog['missions']['training.1']['eligibility'] = definition
            with self.subTest(definition=definition), self.assertRaises(DomainError):
                validate_catalog(catalog)

    def test_unupgraded_hero_has_implicit_first_rank_and_tier(self):
        from game_server.mission_eligibility import matches
        hero = self.service.get_player('owner')['characters']['scout']
        self.assertTrue(matches('scout', hero, {'minimum_rank': 1, 'minimum_tier': 1}))
        self.assertFalse(matches('scout', hero, {'minimum_rank': 2}))
        self.assertFalse(matches('scout', hero, {'minimum_promotion_rank': 1}))

    def test_original_challenge_local_weekday_is_server_authority_before_spending(self):
        # The old campaign module used process-local Sunday=0, not UTC's
        # Monday=0. Test the same mapping without depending on machine TZ.
        self.service.clock = lambda: self.now
        self.now = 1_700_000_000
        today = datetime.fromtimestamp(self.now).isoweekday() % 7
        allowed = (today + 1) % 7
        definition = self.service.catalog['missions']['training.1']['eligibility']
        definition['scopes'][0]['local_weekdays'] = [allowed]
        before = self.service.get_player('owner')
        with self.assertRaises(DomainError) as error:
            self.command('mission.begin', 'closed-weekday', mission_id='training.1',
                         team=['scout', 'guardian'])
        self.assertEqual(error.exception.code, 'challenge_not_active_today')
        self.assertEqual(self.service.get_player('owner'), before)
        # Persist/restart is not needed for read-only policy; the same normal
        # mission.begin command becomes eligible on the correct local day.
        for _ in range(8):
            self.now += 86400
            if datetime.fromtimestamp(self.now).isoweekday() % 7 == allowed:
                break
        started = self.command('mission.begin', 'open-weekday',
                               mission_id='training.1', team=['scout', 'guardian'])
        self.assertEqual(started['player']['wallet']['energy'], before['wallet']['energy'] - 2)
        self.assertEqual(self.command('mission.begin', 'open-weekday', mission_id='training.1',
                                      team=['scout', 'guardian']), started)

    def test_source_weekday_policy_is_bounded_and_cannot_accept_boolean_or_duplicates(self):
        for invalid in ([0, 0], [], [7], [-1], [True], list(range(8)), 'Monday'):
            candidate = deepcopy(self.catalog)
            candidate['missions']['training.1']['eligibility']['scopes'][0]['local_weekdays'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(DomainError):
                validate_catalog(candidate)

    def test_one_time_event_challenge_blocks_reentry_after_earned_clear(self):
        # This reproduces the original eligible_challenge saved-star guard,
        # now enforced against the public mission ledger before energy spend.
        self.service.catalog['missions']['training.1']['eligibility'] = {'one_time': True}
        first = self.command('mission.begin', 'event-start',
                             mission_id='training.1', team=['scout'])
        battle = first['battle']
        result = self.command('mission.complete', 'event-first-clear',
                              battle_id=battle['battle_id'],
                              mission_id='training.1', outcome='victory', stars=1)
        after = self.service.get_player('owner')
        self.assertEqual(after['missions']['training.1']['best_stars'], 1)
        with self.assertRaises(DomainError) as error:
            self.command('mission.begin', 'event-reentry',
                         mission_id='training.1', team=['scout'])
        self.assertEqual(error.exception.code, 'event_challenge_already_completed')
        self.assertEqual(self.service.get_player('owner'), after)
        self.assertEqual(self.command('mission.complete', 'event-first-clear',
                                      battle_id=battle['battle_id'],
                                      mission_id='training.1', outcome='victory', stars=1),
                         result)
        for invalid in ('true', 1, [], None):
            candidate = deepcopy(self.catalog)
            candidate['missions']['training.1']['eligibility'] = {'one_time': invalid}
            with self.subTest(invalid=invalid), self.assertRaises(DomainError):
                validate_catalog(candidate)

    def test_implicit_campaign_predecessor_restricts_only_first_entry(self):
        # Isolate the implicit migrated rule from the demo catalog's separate
        # permanently enforced explicit prerequisite.
        self.service.catalog['missions']['training.2'].pop('prerequisite', None)
        self.service.catalog['missions']['training.2']['eligibility'] = {
            'first_entry_minimum_mission_stars': {'training.1': 1}}
        before = self.service.get_player('owner')
        with self.assertRaises(DomainError) as error:
            self.command('mission.begin', 'campaign-gate',
                         mission_id='training.2', team=['guardian'])
        self.assertEqual(error.exception.code, 'previous_mission_required')
        self.assertEqual(self.service.get_player('owner'), before)
        first = self.command('mission.begin', 'clear-first',
                             mission_id='training.1', team=['scout', 'guardian'])
        self.command('mission.complete', 'first-clear', battle_id=first['battle']['battle_id'],
                     mission_id='training.1', outcome='victory', stars=1)
        second = self.command('mission.begin', 'campaign-open',
                              mission_id='training.2', team=['guardian'])
        self.command('mission.complete', 'second-clear', battle_id=second['battle']['battle_id'],
                     mission_id='training.2', outcome='victory', stars=1)
        from game_server.mission_eligibility import require
        earned = self.service.get_player('owner')
        earned['missions'].pop('training.1')
        require(earned, self.service.catalog['missions']['training.2'], ['guardian'], 0, 'training.2')
        earned['missions'].pop('training.2')
        with self.assertRaises(DomainError) as error:
            require(earned, self.service.catalog['missions']['training.2'], ['guardian'], 0, 'training.2')
        self.assertEqual(error.exception.code, 'previous_mission_required')
        for invalid in ({'missing': 1}, {'training.1': 0}, {'training.1': True}, []):
            candidate = deepcopy(self.catalog)
            candidate['missions']['training.2']['eligibility'] = {
                'first_entry_minimum_mission_stars': invalid}
            with self.subTest(invalid=invalid), self.assertRaises(DomainError):
                validate_catalog(candidate)

    def test_event_section_time_window_and_all_clears_are_server_authority(self):
        self.now = 1_700_000_000
        self.service.clock = lambda: self.now
        self.service.catalog['missions']['training.1']['eligibility'] = {
            'event_access': {
                'section_mission_ids': ['training.1', 'training.2'],
                'windows': [{'starts_at': self.now + 10, 'ends_at': self.now + 30,
                             'required_level': 1}]}}
        self.service.catalog['missions']['training.2']['eligibility'] = {
            'event_access': {
                'section_mission_ids': ['training.1', 'training.2'],
                'windows': [{'starts_at': self.now + 10, 'ends_at': self.now + 30,
                             'required_level': 1}]}}
        before = self.service.get_player('owner')
        with self.assertRaises(DomainError) as exc:
            self.command('mission.begin', 'before-window', mission_id='training.1', team=['scout'])
        self.assertEqual(exc.exception.code, 'event_challenge_inactive')
        self.assertEqual(self.service.get_player('owner'), before)
        self.now += 10
        issued = self.command('mission.begin', 'inside', mission_id='training.1', team=['scout'])
        # Old combat settlement permits a legitimately issued fight to
        # complete after the event window closes.
        self.now += 25
        cleared = self.command('mission.complete', 'clear', battle_id=issued['battle']['battle_id'],
                               mission_id='training.1', outcome='victory', stars=3)
        self.assertEqual(cleared['player']['missions']['training.1']['best_stars'], 3)
        snapshot = self.service.get_player('owner')
        with self.assertRaises(DomainError) as exc:
            self.command('mission.sweep', 'expired', mission_id='training.1', quantity=1)
        self.assertEqual(exc.exception.code, 'event_challenge_inactive')
        self.assertEqual(self.service.get_player('owner'), snapshot)
        self.service.catalog['missions']['training.1']['eligibility']['event_access'] = {
            'section_mission_ids': ['training.1', 'training.2'], 'permanent_minimum_level': 1}
        self.service.catalog['missions']['training.2']['eligibility']['event_access'] = {
            'section_mission_ids': ['training.1', 'training.2'], 'permanent_minimum_level': 1}
        sweep = self.command('mission.sweep', 'permitted', mission_id='training.1', quantity=1)
        self.assertEqual(sweep['player']['missions']['training.1']['wins'], 2)
        self.service.catalog['missions']['training.1']['eligibility']['one_time'] = True
        one_time = self.service.get_player('owner')
        with self.assertRaises(DomainError) as exc:
            self.command('mission.sweep', 'single-clear-no-sweep',
                         mission_id='training.1', quantity=1)
        self.assertEqual(exc.exception.code, 'event_challenge_already_completed')
        self.assertEqual(self.service.get_player('owner'), one_time)
        second = self.command('mission.begin', 'second', mission_id='training.2', team=['guardian'])
        self.command('mission.complete', 'second-win', battle_id=second['battle']['battle_id'],
                     mission_id='training.2', outcome='victory', stars=1)
        finished = self.service.get_player('owner')
        for command, params in (
            ('mission.begin', {'mission_id': 'training.1', 'team': ['scout']}),
            ('mission.sweep', {'mission_id': 'training.1', 'quantity': 1})):
            with self.assertRaises(DomainError) as exc:
                self.command(command, 'section-complete-' + command, **params)
            self.assertEqual(exc.exception.code, 'event_challenge_section_complete')
            self.assertEqual(self.service.get_player('owner'), finished)

    def test_event_access_catalog_rejects_unbounded_or_forged_windows(self):
        correct = {'section_mission_ids': ['training.1'],
                   'windows': [{'starts_at': 10, 'ends_at': 20, 'required_level': 1}]}
        for invalid in (
            {'section_mission_ids': ['training.1']},
            {'section_mission_ids': ['training.1'], 'windows': [], 'permanent_minimum_level': 1},
            {'section_mission_ids': ['training.1', 'training.1'], 'windows': []},
            {'section_mission_ids': ['unknown'], 'windows': []},
            {'section_mission_ids': ['training.1'], 'windows': [{'starts_at': 10, 'ends_at': 10,
                                                                  'required_level': 1}]},
            {'section_mission_ids': ['training.1'], 'windows': [{'starts_at': 10, 'ends_at': 20,
                                                                  'required_level': True}]},
            {'section_mission_ids': ['training.1'], 'permanent_minimum_level': 1000},
            {'section_mission_ids': [], 'windows': []}):
            candidate = deepcopy(self.catalog)
            candidate['missions']['training.1']['eligibility'] = {'event_access': invalid}
            with self.subTest(invalid=invalid), self.assertRaises(DomainError):
                validate_catalog(candidate)
        candidate = deepcopy(self.catalog)
        candidate['missions']['training.1']['eligibility'] = {'event_access': correct}
        validate_catalog(candidate)

    def test_sweep_rechecks_challenge_schedule_and_commander_gates_without_squad(self):
        self.now = 1_700_000_000
        self.service.clock = lambda: self.now
        today = datetime.fromtimestamp(self.now).isoweekday() % 7
        rules = self.service.catalog['missions']['training.1']['eligibility']
        # The original replay path accepts an empty squad because the
        # three-star clear already proves combat eligibility, but it still
        # evaluates the authored day/level requirements on every sweep.
        rules.clear()
        rules['scopes'] = [{
            'local_weekdays': [today], 'minimum_commander_level': 1,
            'must_include': ['scout'],
            'each_character': {'character_ids': ['scout']},
        }]
        battle = self.command('mission.begin', 'earned-challenge',
                              mission_id='training.1', team=['scout'])['battle']
        self.command('mission.complete', 'earned-three-stars', battle_id=battle['battle_id'],
                     mission_id='training.1', outcome='victory', stars=3)
        self.now += 86400
        before = self.service.get_player('owner')
        with self.assertRaises(DomainError) as error:
            self.command('mission.sweep', 'retry-after-opening',
                         mission_id='training.1', quantity=1)
        self.assertEqual(error.exception.code, 'challenge_not_active_today')
        self.assertEqual(self.service.get_player('owner'), before)
        self.now += 6 * 86400
        rules['scopes'][0]['minimum_commander_level'] = 2
        with self.assertRaises(DomainError) as error:
            self.command('mission.sweep', 'retry-after-opening',
                         mission_id='training.1', quantity=1)
        self.assertEqual(error.exception.code, 'mission_commander_level')
        self.assertEqual(self.service.get_player('owner'), before)
        rules['scopes'][0]['minimum_commander_level'] = 1
        swept = self.command('mission.sweep', 'retry-after-opening',
                             mission_id='training.1', quantity=1)
        self.assertEqual(swept['player']['missions']['training.1']['wins'], 2)
        self.assertEqual(self.command('mission.sweep', 'retry-after-opening',
                                      mission_id='training.1', quantity=1), swept)
        self.assertEqual(self.service.get_player('owner')['version'], swept['player']['version'])


if __name__ == '__main__':
    unittest.main()

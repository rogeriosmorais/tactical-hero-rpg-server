"""Regression of legacy challenge section limits after moving authority to server."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


class ChallengeLimitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'challenge.sqlite'
        self.now = 1728000
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['challenge_limits'] = {'materials': 2}
        self.catalog['missions']['training.1']['challenge_section'] = 'materials'
        extra = deepcopy(self.catalog['missions']['training.1'])
        self.catalog['missions']['challenge.other'] = extra
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def tearDown(self):
        self.temp.cleanup()

    def command(self, command, key, **parameters):
        return self.service.execute('a', {'command': command, 'idempotency_key': key,
                                          'parameters': parameters})

    def begin(self, key, mission='training.1'):
        return self.command('mission.begin', key, mission_id=mission, team=['scout'])

    def finish(self, key, battle, mission='training.1', outcome='victory'):
        return self.command('mission.complete', key, battle_id=battle['battle_id'],
                            mission_id=mission, outcome=outcome,
                            stars=3 if outcome == 'victory' else 0)

    def test_completed_challenge_and_sweep_use_one_shared_section_cap(self):
        original = self.service.get_player('a')
        self.assertEqual(self.service.get_challenge_limits('a')['sections']['materials'],
                         {'daily_limit': 2, 'used': 0, 'remaining': 2})
        self.assertEqual(self.service.get_player('a'), original)
        initial = self.begin('start')
        first = self.finish('first', initial['battle'])
        self.assertEqual(self.service.get_challenge_limits('a')['sections']['materials']['remaining'], 1)
        before_sweep = self.service.get_player('a')
        with self.assertRaises(DomainError) as error:
            self.command('mission.sweep', 'overspend', mission_id='training.1', quantity=2)
        self.assertEqual(error.exception.code, 'challenge_daily_limit_reached')
        self.assertEqual(self.service.get_player('a'), before_sweep)
        used = self.command('mission.sweep', 'last', mission_id='training.1', quantity=1)
        self.assertEqual(used['player']['challenge_attempts'],
                         {'day_start': self.now, 'used': {'materials': 2}})
        self.assertEqual(self.command('mission.sweep', 'last',
                                     mission_id='training.1', quantity=1), used)
        blocked = self.service.get_player('a')
        with self.assertRaises(DomainError) as error:
            self.begin('another', mission='challenge.other')
        self.assertEqual(error.exception.code, 'challenge_daily_limit_reached')
        self.assertEqual(self.service.get_player('a'), blocked)
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.service.get_challenge_limits('a')['sections']['materials']['remaining'], 0)
        self.assertEqual(self.finish('first', initial['battle']), first)
        self.assertEqual(self.service.get_player('a'), blocked)

    def test_defeat_does_not_consume_and_midnight_resets_read_only(self):
        battle = self.begin('begun')['battle']
        before = self.service.get_challenge_limits('a')
        failure = self.finish('loss', battle, outcome='defeat')
        self.assertEqual(self.service.get_challenge_limits('a'), before)
        self.assertNotIn('challenge_attempts', failure['player'])
        self.now += 86400
        view = self.service.get_challenge_limits('a')
        self.assertEqual((view['day_start'], view['next_reset']), (self.now, self.now + 86400))
        self.assertEqual(view['sections']['materials']['remaining'], 2)
        self.assertNotIn('challenge_attempts', self.service.get_player('a'))
        battle = self.begin('day-two')['battle']
        self.finish('day-two-win', battle)
        self.assertEqual(self.service.get_challenge_limits('a')['sections']['materials']['remaining'], 1)
        self.now += 86400
        self.assertEqual(self.service.get_challenge_limits('a')['sections']['materials']['remaining'], 2)
        self.assertEqual(self.service.get_player('a')['challenge_attempts']['day_start'], self.now - 86400)

    def test_attempt_is_charged_on_victory_only_and_rollback_is_atomic(self):
        battle = self.begin('begin')['battle']
        self.finish('win', battle)
        second = self.begin('begin-other', 'challenge.other')['battle']
        self.finish('other-win', second, 'challenge.other')
        snapshot = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('mission.sweep', 'denied', mission_id='training.1', quantity=1)
        self.assertEqual(self.service.get_player('a'), snapshot)
        self.now += 86400
        self.assertEqual(self.service.get_challenge_limits('a')['sections']['materials']['remaining'], 2)
        with self.assertRaises(DomainError):
            self.command('mission.sweep', 'too-many', mission_id='training.1', quantity=3)
        self.assertEqual(self.service.get_player('a'), snapshot)

    def test_catalog_requires_only_valid_referenced_section_limits(self):
        valid = deepcopy(self.catalog)
        self.assertEqual(validate_catalog(valid)['challenge_limits'], {'materials': 2})
        variants = [
            lambda v: v['challenge_limits'].update(materials=0),
            lambda v: v['challenge_limits'].update(materials=True),
            lambda v: v['challenge_limits'].update(materials=101),
            lambda v: v['challenge_limits'].update(other=3),
            lambda v: v['missions']['training.1'].update(challenge_section='not-configured'),
            lambda v: v.pop('challenge_limits'),
        ]
        for mutate in variants:
            candidate = deepcopy(valid)
            mutate(candidate)
            with self.assertRaises(DomainError):
                validate_catalog(candidate)
        legacy = deepcopy(DEMO_CATALOG)
        self.assertNotIn('challenge_limits', validate_catalog(legacy))


if __name__ == '__main__':
    unittest.main()

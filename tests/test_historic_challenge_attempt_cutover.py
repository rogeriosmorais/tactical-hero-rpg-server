"""Original successful Challenge usage must remain spent until UTC reset."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError
from game_server.profile_cutover import import_complete_player
from game_server.service import GameService


class HistoricChallengeAttemptCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.today = 18 * 86400 + 50
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['challenge_limits'] = {'materials': 2}
        self.catalog['missions']['training.1']['challenge_section'] = 'materials'
        original = deepcopy(self.catalog)
        original.pop('challenge_limits')
        original['missions']['training.1'].pop('challenge_section')
        source = GameService(Path(self.temp.name) / 'source.sqlite', original,
                             clock=lambda: self.today)
        self.snapshot = source.create_player({'player_id': 'retained',
                                               'display_name': 'Existing Commander'})
        self.snapshot['challenge_attempts'] = {
            'day_start': 18 * 86400, 'used': {'materials': 2}}
        self.service = GameService(Path(self.temp.name) / 'target.sqlite', self.catalog,
                                   clock=lambda: self.today)

    def provenance(self, snapshot):
        return {'source_digest': 'd' * 64,
                'source_sections': ['profile', 'transactions'],
                'section_map': {'profile': sorted(snapshot), 'transactions': ['legacy_receipts']},
                'used_command_keys': ['historic-attempt']}

    def test_current_utc_day_is_exhausted_but_next_day_recovers_normally(self):
        original = deepcopy(self.snapshot)
        self.assertEqual(import_complete_player(self.service, original,
                                                self.provenance(original))['status'], 'imported')
        self.assertEqual(import_complete_player(self.service, original,
                                               self.provenance(original))['status'], 'already_imported')
        quota = self.service.get_challenge_limits('retained')['sections']['materials']
        self.assertEqual(quota['used'], 2)
        self.assertEqual(quota['remaining'], 0)
        before = self.service.get_player('retained')
        with self.assertRaises(DomainError) as blocked:
            self.service.execute('retained', {'command': 'mission.begin',
                'idempotency_key': 'attempt-current',
                'parameters': {'mission_id': 'training.1', 'team': ['scout']}})
        self.assertEqual(blocked.exception.code, 'challenge_daily_limit_reached')
        self.assertEqual(self.service.get_player('retained'), before)
        self.service.clock = lambda: self.today + 86400
        self.assertEqual(self.service.get_challenge_limits('retained')['sections']['materials']['remaining'], 2)
        result = self.service.execute('retained', {'command': 'mission.begin',
            'idempotency_key': 'attempt-next-day',
            'parameters': {'mission_id': 'training.1', 'team': ['scout']}})
        self.assertIn('battle', result)

    def test_missing_unmapped_future_or_excess_saved_usage_blocks_import(self):
        variants = []
        missing = deepcopy(self.snapshot)
        missing.pop('challenge_attempts')
        variants.append(missing)
        for change in (
                lambda s: s.update(day_start=18 * 86400 + 1),
                lambda s: s.update(day_start=19 * 86400),
                lambda s: s.update(day_start=True),
                lambda s: s['used'].update(materials=3),
                lambda s: s['used'].update(materials=True),
                lambda s: s['used'].update(materials=0),
                lambda s: s['used'].update(other=1),
                lambda s: s.update(unknown=True),
        ):
            case = deepcopy(self.snapshot)
            change(case['challenge_attempts'])
            variants.append(case)
        for snapshot in variants:
            with self.subTest(snapshot=snapshot.get('challenge_attempts')):
                with self.assertRaises(DomainError):
                    import_complete_player(self.service, snapshot, self.provenance(snapshot))
        with self.assertRaises(DomainError) as error:
            self.service.get_player('retained')
        self.assertEqual(error.exception.code, 'player_not_found')

    def test_source_attempts_without_configured_challenge_policy_are_rejected(self):
        plain_catalog = deepcopy(self.catalog)
        plain_catalog.pop('challenge_limits')
        plain_catalog['missions']['training.1'].pop('challenge_section')
        public = GameService(Path(self.temp.name) / 'without-challenge.sqlite',
                             plain_catalog, clock=lambda: self.today)
        snapshot = deepcopy(self.snapshot)
        with self.assertRaises(DomainError) as error:
            import_complete_player(public, snapshot, self.provenance(snapshot))
        self.assertEqual(error.exception.code, 'cutover_challenge')
        snapshot['challenge_attempts'] = None
        with self.assertRaises(DomainError):
            import_complete_player(public, snapshot, self.provenance(snapshot))


if __name__ == '__main__':
    unittest.main()

"""Trusted guide history cannot reset completed native tutorials on import."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError
from game_server.profile_cutover import import_complete_player
from game_server.service import GameService


class HistoricGuideCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['guides'] = {'intro': {'events': {
            'start': {'completes': False},
            'save': {'completes': False, 'checkpoint': 'step-a'},
            'finish': {'completes': True}}}}
        source = GameService(Path(self.temp.name) / 'source.sqlite',
                             deepcopy(DEMO_CATALOG), clock=lambda: 1000)
        self.snapshot = source.create_player({'player_id': 'migrant',
                                              'display_name': 'Retained Guide'})
        self.snapshot['guides'] = {'intro': {'completed': True, 'checkpoint': 'step-a'}}
        self.service = GameService(Path(self.temp.name) / 'public.sqlite',
                                   self.catalog, clock=lambda: 1001)

    def manifest(self, snapshot):
        return {'source_digest': 'a' * 64,
                'source_sections': ['player', 'operations'],
                'section_map': {'player': sorted(snapshot),
                                'operations': ['legacy_receipts']},
                'used_command_keys': []}

    def test_completed_tutorial_and_savepoint_survive_import_and_replay(self):
        saved = deepcopy(self.snapshot)
        self.assertEqual(import_complete_player(self.service, saved,
                                                self.manifest(saved))['status'], 'imported')
        self.assertEqual(self.service.get_preferences('migrant')['guides']['intro'],
                         {'completed': True, 'checkpoint': 'step-a'})
        request = {'command': 'guide.event', 'idempotency_key': 'next-save',
                   'parameters': {'guide_id': 'intro', 'event_id': 'save', 'success': True}}
        replay = self.service.execute('migrant', request)
        self.assertEqual(replay['guides']['intro']['completed'], True)
        self.assertEqual(self.service.execute('migrant', request), replay)
        reloaded = GameService(self.service.database, self.catalog)
        self.assertEqual(reloaded.get_preferences('migrant')['guides']['intro'],
                         {'completed': True, 'checkpoint': 'step-a'})

    def test_missing_corrupt_unknown_guide_progress_blocks_import(self):
        failures = []
        candidate = deepcopy(self.snapshot)
        candidate.pop('guides')
        failures.append(candidate)
        for change in (
            lambda s: s['guides'].clear(),
            lambda s: s['guides']['intro'].update(completed=1),
            lambda s: s['guides']['intro'].update(checkpoint='wrong-point'),
            lambda s: s['guides']['intro'].update(extra='native-only'),
            lambda s: s['guides'].update(unknown={'completed': True}),
        ):
            candidate = deepcopy(self.snapshot)
            change(candidate)
            failures.append(candidate)
        for candidate in failures:
            with self.subTest(candidate=candidate.get('guides')):
                with self.assertRaises(DomainError):
                    import_complete_player(self.service, candidate, self.manifest(candidate))
        with self.assertRaises(DomainError) as missing:
            self.service.get_player('migrant')
        self.assertEqual(missing.exception.code, 'player_not_found')


if __name__ == '__main__':
    unittest.main()

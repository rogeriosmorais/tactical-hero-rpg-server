"""An imported frame selection must be backed by already owned inventory."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError
from game_server.profile_cutover import import_complete_player
from game_server.service import GameService


class HistoricAppearanceCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['appearance'] = {'default': 'plain', 'choices': {
            'plain': {}, 'earned': {'required_item': 'frame-license'}}}
        independent = GameService(Path(self.temp.name) / 'original.sqlite',
                                  deepcopy(DEMO_CATALOG), clock=lambda: 1000)
        self.snapshot = independent.create_player({'player_id': 'migrant',
                                                   'display_name': 'Earned Frame'})
        self.snapshot['appearance_id'] = 'earned'
        self.snapshot['inventory']['frame-license'] = 1
        self.service = GameService(Path(self.temp.name) / 'new.sqlite', self.catalog,
                                   clock=lambda: 1000)

    def provenance(self, player):
        return {'source_digest': 'a' * 64, 'source_sections': ['player', 'operations'],
                'section_map': {'player': sorted(player), 'operations': ['legacy_receipts']},
                'used_command_keys': []}

    def test_owned_frame_survives_restart_and_never_grants_second_item(self):
        player = deepcopy(self.snapshot)
        self.assertEqual(import_complete_player(self.service, player,
                                                self.provenance(player))['status'], 'imported')
        self.assertEqual(self.service.get_preferences('migrant')['appearance_id'], 'earned')
        saved = self.service.get_player('migrant')
        self.assertEqual(saved['inventory']['frame-license'], 1)
        restarted = GameService(Path(self.temp.name) / 'new.sqlite', self.catalog,
                                clock=lambda: 1000)
        self.assertEqual(restarted.get_preferences('migrant')['appearance_id'], 'earned')
        self.assertEqual(import_complete_player(restarted, player,
                                                self.provenance(player))['status'], 'already_imported')
        request = {'command': 'appearance.select', 'idempotency_key': 'switch-to-default',
                   'parameters': {'appearance_id': 'plain'}}
        switched = restarted.execute('migrant', request)
        self.assertEqual(switched['appearance_id'], 'plain')
        self.assertEqual(restarted.execute('migrant', request), switched)
        self.assertEqual(restarted.get_player('migrant')['inventory'], saved['inventory'])

    def test_missing_unknown_unowned_and_unconfigured_frame_blocks_import_atomically(self):
        cases = []
        for modify in (
            lambda s: s.pop('appearance_id'),
            lambda s: s.update(appearance_id='unknown'),
            lambda s: s.update(appearance_id=1),
            lambda s: s['inventory'].pop('frame-license'),
            lambda s: s['inventory'].update({'frame-license': 0}),
        ):
            candidate = deepcopy(self.snapshot)
            modify(candidate)
            cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=candidate.get('appearance_id')):
                with self.assertRaises(DomainError):
                    import_complete_player(self.service, candidate, self.provenance(candidate))
        with self.assertRaises(DomainError) as absent:
            self.service.get_player('migrant')
        self.assertEqual(absent.exception.code, 'player_not_found')
        no_frames = GameService(Path(self.temp.name) / 'without-frames.sqlite',
                                deepcopy(DEMO_CATALOG), clock=lambda: 1000)
        with self.assertRaises(DomainError):
            import_complete_player(no_frames, self.snapshot, self.provenance(self.snapshot))


if __name__ == '__main__':
    unittest.main()

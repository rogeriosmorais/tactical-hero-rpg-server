"""Trusted import preserves typed saved settings and rejects missing choices."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError
from game_server.profile_cutover import import_complete_player
from game_server.service import GameService


class HistoricSettingsCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['client_settings'] = {
            'audio.music': {'kind': 'boolean', 'default': True},
            'queue.sort': {'kind': 'integer', 'default': 0},
            'profile.tag': {'kind': 'string', 'default': ''}}
        starter = GameService(Path(self.temp.name) / 'starter.sqlite',
                              deepcopy(DEMO_CATALOG), clock=lambda: 1000)
        self.snapshot = starter.create_player({'player_id': 'migrant',
                                               'display_name': 'My Settings'})
        self.snapshot['client_settings'] = {
            'audio.music': False, 'queue.sort': 8, 'profile.tag': 'saved'}
        self.service = GameService(Path(self.temp.name) / 'public.sqlite',
                                   self.catalog, clock=lambda: 1000)

    def provenance(self, snapshot):
        return {'source_digest': 'c' * 64,
                'source_sections': ['saved', 'receipts'],
                'section_map': {'saved': sorted(snapshot), 'receipts': ['legacy_receipts']},
                'used_command_keys': []}

    def test_selected_values_survive_new_session_and_can_change_normally(self):
        original = deepcopy(self.snapshot)
        imported = import_complete_player(self.service, original, self.provenance(original))
        self.assertEqual(imported['status'], 'imported')
        self.assertEqual(self.service.get_client_settings('migrant')['values'], original['client_settings'])
        restarted = GameService(Path(self.temp.name) / 'public.sqlite', self.catalog, clock=lambda: 1000)
        self.assertEqual(restarted.get_client_settings('migrant')['values'], original['client_settings'])
        self.assertEqual(import_complete_player(restarted, original, self.provenance(original))['status'],
                         'already_imported')
        reply = restarted.execute('migrant', {'command': 'client_settings.patch',
            'idempotency_key': 'change', 'parameters': {'values': {'audio.music': True}}})
        self.assertTrue(reply['values']['audio.music'])
        self.assertEqual(restarted.get_player('migrant')['wallet'], original['wallet'])

    def test_incomplete_invalid_and_unconfigured_settings_cannot_be_silently_reset(self):
        candidates = []
        no_settings = deepcopy(self.snapshot)
        del no_settings['client_settings']
        candidates.append(no_settings)
        changes = (
            lambda s: s['client_settings'].pop('profile.tag'),
            lambda s: s['client_settings'].update(unexpected=True),
            lambda s: s['client_settings'].update({'audio.music': 1}),
            lambda s: s['client_settings'].update({'queue.sort': True}),
            lambda s: s['client_settings'].update({'profile.tag': 'x' * 257}),
        )
        for change in changes:
            candidate = deepcopy(self.snapshot)
            change(candidate)
            candidates.append(candidate)
        for candidate in candidates:
            with self.subTest(candidate=candidate.get('client_settings')):
                with self.assertRaises(DomainError):
                    import_complete_player(self.service, candidate, self.provenance(candidate))
        with self.assertRaises(DomainError) as missing:
            self.service.get_player('migrant')
        self.assertEqual(missing.exception.code, 'player_not_found')
        plain = deepcopy(DEMO_CATALOG)
        target = GameService(Path(self.temp.name) / 'without-policy.sqlite', plain, clock=lambda: 1000)
        with self.assertRaises(DomainError):
            import_complete_player(target, self.snapshot, self.provenance(self.snapshot))


if __name__ == '__main__':
    unittest.main()

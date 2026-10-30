"""Legacy DS settings mutations now use the public atomic command journal."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


class ClientSettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Path(self.folder.name) / 'game.sqlite'
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['client_settings'] = {
            'audio.music': {'kind': 'boolean', 'default': True},
            'audio.effects': {'kind': 'boolean', 'default': True},
            'display.rotation': {'kind': 'boolean', 'default': False},
            'queue.offset': {'kind': 'integer', 'default': 0},
            'last.view': {'kind': 'string', 'default': ''}}
        self.service = GameService(self.database, self.catalog)
        self.service.create_player({'player_id': 'p', 'display_name': 'P'})

    def tearDown(self):
        self.folder.cleanup()

    def execute(self, key, values):
        return self.service.execute('p', {'command': 'client_settings.patch',
                                          'idempotency_key': key, 'parameters': {'values': values}})

    def test_atomic_settings_and_receipt_replay_survive_server_restart(self):
        initial = self.service.get_player('p')
        self.assertEqual(self.service.get_client_settings('p')['values']['audio.music'], True)
        self.assertEqual(self.service.get_player('p'), initial)
        changed = self.execute('save-a', {'audio.music': False, 'queue.offset': 25})
        self.assertEqual(changed['values']['audio.music'], False)
        self.assertEqual(changed['values']['queue.offset'], 25)
        self.assertEqual(changed['values']['audio.effects'], True)
        self.assertEqual(self.execute('save-a', {'audio.music': False, 'queue.offset': 25}), changed)
        self.assertEqual(changed['player']['wallet'], initial['wallet'])
        self.assertEqual(changed['player']['inventory'], initial['inventory'])
        with self.assertRaises(DomainError) as conflict:
            self.execute('save-a', {'audio.music': True})
        self.assertEqual(conflict.exception.code, 'idempotency_conflict')
        self.service = GameService(self.database, self.catalog)
        self.assertEqual(self.service.get_client_settings('p'), {
            'values': {'audio.music': False, 'audio.effects': True, 'display.rotation': False,
                       'queue.offset': 25, 'last.view': ''}})
        self.assertEqual(self.execute('save-a', {'audio.music': False, 'queue.offset': 25}), changed)
        other = self.service.create_player({'player_id': 'other', 'display_name': 'Other'})
        self.assertNotIn('client_settings', other)

    def test_unmapped_or_invalid_patch_rolls_back_all_values(self):
        initial = self.service.get_player('p')
        candidates = ({'audio.music': True, 'queue.offset': False},
                      {'audio.music': True, 'frame_id': 'premium'},
                      {'audio.music': 1},
                      {'last.view': 'x' * 257},
                      {'queue.offset': 2**40},
                      {})
        for index, patch in enumerate(candidates):
            with self.assertRaises(DomainError):
                self.execute('invalid-' + str(index), patch)
            self.assertEqual(self.service.get_player('p'), initial)
        valid = self.execute('invalid-0', {'audio.music': False})
        self.assertFalse(valid['values']['audio.music'])

    def test_catalog_rejects_unsupported_value_types_and_empty_policy(self):
        self.assertEqual(len(validate_catalog(self.catalog)['client_settings']), 5)
        for mutate in (
            lambda x: x.update(client_settings={}),
            lambda x: x['client_settings']['audio.music'].update(kind='native'),
            lambda x: x['client_settings']['audio.music'].update(default=1),
            lambda x: x['client_settings']['queue.offset'].update(default=True),
            lambda x: x['client_settings']['last.view'].update(default='x'*257),
        ):
            catalog = deepcopy(self.catalog)
            mutate(catalog)
            with self.assertRaises(DomainError):
                validate_catalog(catalog)


if __name__ == '__main__':
    unittest.main()

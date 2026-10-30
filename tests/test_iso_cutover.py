"""Public cutover accepts only source-translated earned enhancement snapshots."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DomainError
from game_server.profile_cutover import import_complete_player
from game_server.service import GameService
from test_enhancements import extension_catalog


class IsoCutoverTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.catalog = extension_catalog()
        self.service = GameService(Path(self.folder.name) / 'target.sqlite', self.catalog)
        original = GameService(Path(self.folder.name) / 'source.sqlite', self.catalog)
        self.snapshot = original.create_player({'player_id': 'migrant', 'display_name': 'Migrant'})
        self.snapshot['inventory']['augmentation-license'] = 1
        self.snapshot['characters']['scout']['enhancement'] = {
            'items': {'left': 'left-2', 'right': 'right-2'},
            'tier': 1, 'class_id': 'guard',
            'learned': {'guard': ['guard1', 'guard2'], 'healer': ['healer1']},
            'swap_available_at': 1000, 'quality': 2}
        self.provenance = {
            'source_digest': 'a' * 64,
            'source_sections': ['user', 'resources', 'inventory', 'roster', 'missions', 'offers', 'operations'],
            'section_map': {
                'user': ['player_id', 'display_name', 'level', 'experience', 'version'],
                'resources': ['wallet'], 'inventory': ['inventory'],
                'roster': ['characters'], 'missions': ['missions'],
                'offers': ['offer_purchases'], 'operations': ['legacy_receipts']},
            'used_command_keys': ['iso-legacy-equip', 'iso-legacy-skill']}
        mapped = {field for fields in self.provenance['section_map'].values()
                  for field in fields}
        unmapped = sorted(set(self.snapshot) - mapped)
        if unmapped:
            self.provenance['source_sections'].append('generated-fields')
            self.provenance['section_map']['generated-fields'] = unmapped

    def test_trusted_iso_progress_imports_without_rewards_or_unpaid_replay(self):
        result = import_complete_player(self.service, self.snapshot, self.provenance)
        self.assertEqual(result['status'], 'imported')
        actual = self.service.get_player('migrant')
        self.assertEqual(actual['characters']['scout']['enhancement'],
                         self.snapshot['characters']['scout']['enhancement'])
        self.assertEqual(actual['inventory'], self.snapshot['inventory'])
        self.assertEqual(actual['wallet'], self.snapshot['wallet'])
        self.assertEqual(import_complete_player(self.service, self.snapshot, self.provenance)['status'],
                         'already_imported')
        with self.assertRaises(DomainError) as conflict:
            self.service.execute('migrant', {
                'command': 'enhancement.unequip', 'idempotency_key': 'iso-legacy-equip',
                'parameters': {'character_id': 'scout', 'slot_id': 'left'}})
        self.assertEqual(conflict.exception.code, 'idempotency_conflict')
        self.assertEqual(self.service.get_player('migrant'), actual)

    def test_invalid_iso_snapshots_fail_atomically_without_losing_purchase_history(self):
        cases = []
        for change in (
            lambda s: s['items'].update(left='unknown'),
            lambda s: s['items'].update(left='right-2'),
            lambda s: s['items'].update(left='other-role'),
            lambda s: s.update(quality=0),
            lambda s: s.update(tier=3),
            lambda s: s.update(class_id='unknown'),
            lambda s: s['learned'].update(guard=['guard2']),
            lambda s: s['learned'].update(healer=['guard1']),
            lambda s: s['learned'].clear(),
            lambda s: s.pop('items')
        ):
            snapshot = deepcopy(self.snapshot)
            change(snapshot['characters']['scout']['enhancement'])
            cases.append(snapshot)
        for snapshot in cases:
            with self.subTest(value=snapshot['characters']['scout']['enhancement']):
                with self.assertRaises((DomainError, ValueError, TypeError)):
                    import_complete_player(self.service, snapshot, self.provenance)
                with self.assertRaises(DomainError) as missing:
                    self.service.get_player('migrant')
                self.assertEqual(missing.exception.code, 'player_not_found')


if __name__ == '__main__':
    unittest.main()

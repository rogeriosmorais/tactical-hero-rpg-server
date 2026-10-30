"""Migration of the old gateway ProfileStore.backup to public authority."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from game_server.catalog import DomainError
from game_server.service import GameService


class PublicBackupTests(unittest.TestCase):
    def test_backup_is_a_committed_consistent_copy_of_independent_player_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = GameService(root / 'live.sqlite')
            source.create_player({'player_id': 'player-one', 'display_name': 'One'})
            earned = source.execute('player-one', {'command': 'mission.begin',
                'idempotency_key': 'first-battle', 'parameters': {'mission_id': 'training.1', 'team': ['scout']}})
            target = root / 'backups' / 'checkpoint.sqlite'
            info = source.backup(target)
            self.assertEqual(info['database'], str(target))
            self.assertEqual(info['catalog_digest'], source.catalog_digest)
            with closing(sqlite3.connect(target)) as verification:
                self.assertEqual(verification.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            restored = GameService(target)
            self.assertEqual(restored.get_player('player-one'), source.get_player('player-one'))
            self.assertEqual(restored.get_battle('player-one', earned['battle']['battle_id']),
                             source.get_battle('player-one', earned['battle']['battle_id']))
            self.assertEqual(restored.execute('player-one', {
                'command': 'mission.begin', 'idempotency_key': 'first-battle',
                'parameters': {'mission_id': 'training.1', 'team': ['scout']}}), earned)
            source.create_player({'player_id': 'later', 'display_name': 'Later'})
            with self.assertRaises(DomainError):
                restored.get_player('later')
            self.assertEqual(restored.get_player('player-one')['version'], 1)
            with self.assertRaises(DomainError):
                source.backup(target)
            with self.assertRaises(DomainError):
                source.backup(source.database)
            self.assertEqual(restored.get_player('player-one'), source.get_player('player-one'))

    def test_failed_destination_does_not_damage_existing_public_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = GameService(root / 'live.sqlite')
            before = source.create_player({'player_id': 'p', 'display_name': 'P'})
            target = root / 'backups'
            target.mkdir()
            with self.assertRaises(DomainError):
                source.backup(target)
            self.assertEqual(source.get_player('p'), before)
            self.assertTrue(target.is_dir())


if __name__ == '__main__':
    unittest.main()

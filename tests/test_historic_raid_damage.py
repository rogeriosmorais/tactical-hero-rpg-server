"""Old per-node damage migration: public authority, no simulated rewards."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DomainError
from game_server.historic_raid_damage import import_historic_raid_damage
from game_server.service import GameService, canonical
from test_raids import raid_catalog


class HistoricRaidDamageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'public.sqlite'
        self.now = 1_000_000
        self.catalog = raid_catalog()
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.service.create_player({'player_id': 'b', 'display_name': 'B'})
        self.raid = self.service.execute('a', {
            'command': 'raid.launch', 'idempotency_key': 'launch',
            'parameters': {'raid_id': 'expedition'}})['raid']['instance_id']

    def rows(self, damage=150):
        return [{'team_index': 0, 'node_id': 'guard', 'damage': damage},
                {'team_index': 0, 'node_id': 'leader', 'damage': 25}]

    def import_(self, records=None):
        return import_historic_raid_damage(self.service, 'a', self.raid,
                                           self.rows() if records is None else records)

    def test_aggregate_import_is_atomic_idempotent_persistent_and_no_reward(self):
        before = self.service.get_player('a')
        first = self.import_()
        self.assertEqual(first['total_damage_imported'], 175)
        after = self.service.get_player('a')
        self.assertEqual(after['raids'][self.raid]['damage']['0'], 175)
        self.assertEqual(after['raids'][self.raid]['node_damage']['0'],
                         {'guard': 150, 'leader': 25})
        self.assertEqual(after['wallet'], before['wallet'])
        self.assertEqual(after['inventory'], before['inventory'])
        self.assertEqual(after['experience'], before['experience'])
        self.assertEqual(self.service.get_player('b').get('raids', {}), {})
        self.assertEqual(self.import_()['total_damage_imported'], 0)
        self.assertEqual(self.service.get_player('a'), after)
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.import_()['total_damage_imported'], 0)
        self.assertEqual(self.service.get_player('a'), after)

    def test_conflicting_reimports_and_invalid_rows_never_mutate(self):
        self.import_()
        saved = self.service.get_player('a')
        for invalid in (
            self.rows(151), [{'team_index': 3, 'node_id': 'guard', 'damage': 4}],
            [{'team_index': 0, 'node_id': 'alien', 'damage': 4}],
            [{'team_index': 0, 'node_id': 'guard', 'damage': -1}],
            [{'team_index': 0, 'node_id': 'guard', 'damage': 1}] * 2,
        ):
            with self.assertRaises(DomainError):
                self.import_(invalid)
            self.assertEqual(self.service.get_player('a'), saved)

    def test_existing_public_combat_or_mission_clear_blocks_import(self):
        with self.service.transaction() as connection:
            player = self.service._player(connection, 'a')
            player['raids'][self.raid]['node_states'] = {'0': {'guard': {'settled_attempt': True}}}
            connection.execute('UPDATE players SET data=? WHERE id=?', (canonical(player), 'a'))
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.import_([{'team_index': 0, 'node_id': 'guard', 'damage': 50}])
        self.assertEqual(self.service.get_player('a'), before)
        self.assertNotIn('historic_damage_imports', self.service.get_player('a')['raids'][self.raid])


if __name__ == '__main__':
    unittest.main()

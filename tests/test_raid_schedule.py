"""UTC daily raid slots, atomic scheduled purchases and persistent cursors."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DomainError, validate_catalog
from game_server.raid_schedule import due_slot
from game_server.service import GameService
from test_raids import raid_catalog


def scheduled_catalog(two=False):
    result = raid_catalog()
    entries = {'first': {'raid_id': 'expedition', 'difficulty': 0}}
    if two:
        other = deepcopy(result['raids']['expedition'])
        other['launch_cost'] = {'keys': 90}
        result['raids']['expedition_alt'] = other
        entries['second'] = {'raid_id': 'expedition_alt', 'difficulty': 0}
    result['raid_schedule'] = {'enabled': True, 'hour_utc': 12, 'minutes': 0, 'entries': entries}
    return result


class RaidScheduleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = 1_000_000
        self.path = Path(self.folder.name) / 'server.sqlite'
        self.configure(scheduled_catalog())

    def configure(self, catalog):
        self.catalog = catalog
        self.service = GameService(self.path, catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def command(self, key, command='raid.auto_launch', **parameters):
        return self.service.execute('a', {'command': command, 'idempotency_key': key, 'parameters': parameters})

    def due(self):
        state = self.service.get_raid_schedule('a')
        self.assertFalse(state['due'])
        self.now = state['slot'] + 86400
        self.assertTrue(self.service.get_raid_schedule('a')['due'])
        return self.service.get_raid_schedule('a')['slot']

    def test_daily_slot_matches_source_utc_formula_and_reads_are_pure(self):
        start = self.service.get_player('a')
        self.assertEqual(due_slot({'hour_utc': 12, 'minutes': 15}, 86400 + 12 * 3600 + 14 * 60),
                         12 * 3600 + 15 * 60)
        self.assertEqual(due_slot({'hour_utc': 12, 'minutes': 15}, 86400 + 12 * 3600 + 15 * 60),
                         86400 + 12 * 3600 + 15 * 60)
        self.assertFalse(self.service.get_raid_schedule('a')['due'])
        self.assertEqual(self.service.get_player('a'), start)
        slot = self.due()
        with ThreadPoolExecutor(max_workers=3) as pool:
            issued = list(pool.map(lambda _: self.command('auto-' + str(slot)), range(3)))
        self.assertEqual(issued, [issued[0]] * 3)
        self.assertEqual(len(issued[0]['launched_raids']), 1)
        self.assertEqual(issued[0]['player']['wallet']['keys'], 80)
        self.assertFalse(self.service.get_raid_schedule('a')['due'])
        self.assertEqual(self.command('redundant')['launched_raids'], [])
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now + 100)
        self.assertEqual(self.command('auto-' + str(slot)), issued[0])
        self.assertFalse(self.service.get_raid_schedule('a')['due'])
        self.now += 86400
        again = self.command('second-day')
        self.assertEqual(len(again['launched_raids']), 1)
        self.assertEqual(again['player']['wallet']['keys'], 60)

    def test_second_raid_insufficient_funds_rolls_back_first_and_cursor(self):
        self.path = self.path.with_name('two.sqlite')
        self.configure(scheduled_catalog(two=True))
        slot = self.due()
        before = self.service.get_player('a')
        with self.assertRaises(DomainError) as context:
            self.command('retry')
        self.assertEqual(context.exception.code, 'insufficient_funds')
        self.assertEqual(self.service.get_player('a'), before)
        self.assertTrue(self.service.get_raid_schedule('a')['due'])
        with self.service.transaction() as connection:
            from game_server.service import canonical
            player = self.service._player(connection, 'a')
            player['wallet']['keys'] = 110
            connection.execute('UPDATE players SET data=? WHERE id=?', (canonical(player), 'a'))
        response = self.command('retry')
        self.assertEqual(len(response['launched_raids']), 2)
        self.assertEqual(response['player']['wallet']['keys'], 0)
        self.assertEqual(response['player']['alliances']['1']['raid_schedule']['last_attempt_at'], slot)

    def test_native_read_skips_unfunded_schedule_without_consuming_retry_key(self):
        self.path = self.path.with_name('native-skip.sqlite')
        self.configure(scheduled_catalog(two=True))
        slot = self.due()
        before = self.service.get_player('a')
        for _ in range(2):
            skipped = self.command('native-slot', skip_if_unfunded=True)
            self.assertEqual(skipped['launched_raids'], [])
            self.assertEqual(skipped['slot'], slot)
            self.assertEqual(skipped['skipped'], 'insufficient_funds')
            self.assertEqual(self.service.get_player('a'), before)
            self.assertTrue(self.service.get_raid_schedule('a')['due'])
        with self.service.transaction() as connection:
            from game_server.service import canonical
            player = self.service._player(connection, 'a')
            player['wallet']['keys'] = 110
            connection.execute('UPDATE players SET data=? WHERE id=?', (canonical(player), 'a'))
        issued = self.command('native-slot', skip_if_unfunded=True)
        self.assertEqual(len(issued['launched_raids']), 2)
        self.assertEqual(issued['player']['wallet']['keys'], 0)
        self.assertEqual(issued['player']['alliances']['1']['raid_schedule']['last_attempt_at'], slot)
        self.assertEqual(self.command('native-slot', skip_if_unfunded=True), issued)
        with self.assertRaises(DomainError):
            self.command('invalid-skip', skip_if_unfunded='yes')

    def test_manual_active_raid_skips_only_that_scheduled_entry(self):
        self.path = self.path.with_name('skip.sqlite')
        catalog = scheduled_catalog(two=True)
        catalog['raids']['expedition_alt']['launch_cost'] = {'keys': 10}
        self.configure(catalog)
        self.due()
        first = self.command('manual', 'raid.launch', raid_id='expedition')['raid']
        result = self.command('auto')
        self.assertEqual(len(result['launched_raids']), 1)
        self.assertEqual(result['launched_raids'][0]['raid_id'], 'expedition_alt')
        # An existing active raid is not duplicated by scheduled launch.
        self.assertEqual(first['status'], 'active')

    def test_raid_preference_updates_future_scheduled_difficulty(self):
        self.path = self.path.with_name('preference.sqlite')
        catalog = scheduled_catalog()
        catalog['raids']['expedition']['nonsequential_difficulties'] = True
        self.configure(catalog)
        issued = self.command('difficulty', 'raid.set_difficulty', raid_id='expedition', difficulty=1)
        self.assertEqual(issued['player']['alliances']['1']['raid_schedule']['entries']['first']['difficulty'], 1)
        self.due()
        self.assertEqual(self.command('auto')['launched_raids'][0]['difficulty'], 1)

    def test_catalog_rejects_invalid_time_mappings_and_duplicate_raids(self):
        for mutate in (
            lambda c: c['raid_schedule'].update(hour_utc=24),
            lambda c: c['raid_schedule'].update(minutes=True),
            lambda c: c['raid_schedule']['entries']['first'].update(raid_id='missing'),
            lambda c: c['raid_schedule'].update(entries={}),
            lambda c: c['raid_schedule'].update(enabled='true'),
        ):
            catalog = scheduled_catalog()
            mutate(catalog)
            with self.assertRaises(DomainError):
                validate_catalog(catalog)
        catalog = scheduled_catalog()
        catalog['raid_schedule']['entries']['copy'] = deepcopy(catalog['raid_schedule']['entries']['first'])
        with self.assertRaises(DomainError):
            validate_catalog(catalog)


if __name__ == '__main__':
    unittest.main()

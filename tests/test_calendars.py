"""Attendance sequencing, day receipts, UTC rollover and atomic board batches."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService
from game_server.calendar_policy import estimated_boards, split_inventory_awards
from game_server.calendars import import_historic_attendance


def timestamp(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp())


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.now = timestamp('2028-02-01')
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['calendars'] = {mode: {'mode': mode, 'order': index, 'required_level': 1,
            'rewards': [{'inventory': {mode: 1}}, {'inventory': {mode: 2}}]}
            for index, mode in enumerate(('finite', 'repeat', 'consecutive', 'monthly'))}
        self.catalog['calendars'].update({
            'gated': {'mode': 'repeat', 'order': 4, 'required_level': 2, 'rewards': [{'wallet': {'credits': 1}}]},
            'expired': {'mode': 'repeat', 'order': 5, 'required_level': 1, 'ends_at': self.now,
                        'rewards': [{'wallet': {'credits': 999}}]},
            'membership': {'mode': 'repeat', 'order': 6, 'required_level': 1, 'requires_alliance': True,
                           'rewards': [{'wallet': {'credits': 999}}]}})
        self.catalog['objectives'] = {'entries': {}, 'counters': {'attendance': {'source': {'kind': 'activity',
            'rules': [{'command': 'calendar.advance', 'measure': 'quantity'}]}}}}
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def advance(self, key):
        return self.service.execute('a', {'command': 'calendar.advance', 'idempotency_key': key, 'parameters': {}})

    def test_daily_claims_concurrency_expiration_gates_and_exact_restart_replay(self):
        before = self.service.get_player('a')
        state = self.service.get_calendars('a')
        self.assertFalse(state['boards']['gated']['available'])
        self.assertFalse(state['boards']['expired']['available'])
        self.assertFalse(state['boards']['membership']['available'])
        self.assertEqual(self.service.get_player('a'), before)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.advance('first'), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(results[0]['quantity'], 4)
        self.assertEqual(self.advance('different-key')['quantity'], 0)
        self.assertEqual(self.service.get_objectives('a')['counters']['attendance']['value'], 4)
        restarted = GameService(self.path, self.catalog, clock=lambda: self.now + 86400)
        self.assertEqual(restarted.execute('a', {'command': 'calendar.advance', 'idempotency_key': 'first', 'parameters': {}}), results[0])

    def test_finite_exhaustion_repeat_and_consecutive_gap_rules(self):
        self.advance('day1')
        self.now += 86400
        self.advance('day2')
        self.now += 2 * 86400
        state = self.service.get_calendars('a')['boards']
        self.assertFalse(state['finite']['claimable'])
        self.assertEqual(state['repeat']['next_index'], 0)
        self.assertEqual(state['consecutive']['step'], 0)
        self.assertTrue(state['consecutive']['reset_consecutive'])
        result = self.advance('day4')
        self.assertEqual(result['player']['inventory']['finite'], 3)
        self.assertEqual(result['player']['inventory']['repeat'], 4)
        self.assertEqual(result['player']['inventory']['consecutive'], 4)
        self.assertEqual(result['calendar_state']['boards']['consecutive']['step'], 1)

    def test_leap_month_attendance_limit_and_new_month_reset(self):
        for day in range(29):
            self.now = timestamp('2028-02-01') + day * 86400
            self.advance('feb-' + str(day))
        state = self.service.get_calendars('a')
        self.assertEqual(state['days_in_month'], 29)
        self.assertEqual(state['boards']['monthly']['step'], 29)
        self.assertEqual(state['end_of_month'], timestamp('2028-03-01'))
        self.now = timestamp('2028-03-01')
        state = self.service.get_calendars('a')
        self.assertEqual(state['boards']['monthly']['step'], 0)
        result = self.advance('march')
        self.assertEqual(result['calendar_state']['boards']['monthly']['step'], 1)
        self.assertEqual(len(result['player']['calendar_receipts']['monthly']), 30)

    def test_backward_clock_cannot_grant_new_boards_or_replay_tiles(self):
        first = self.advance('today')
        self.now -= 86400
        self.assertEqual(self.advance('backward')['calendar_awards'], [])
        state = self.service.get_calendars('a')
        self.assertEqual(state['day'], first['calendar_state']['day'])
        self.assertEqual(self.service.get_player('a')['inventory'], first['player']['inventory'])

    def test_later_board_failure_rolls_back_every_receipt_and_reward(self):
        before = self.service.get_player('a')
        original = self.service._reward
        def fail_later(player, bundle, count=1):
            original(player, bundle, count)
            if 'repeat' in bundle.get('inventory', {}):
                raise RuntimeError('later board failure')
        self.service._reward = fail_later
        with self.assertRaises(RuntimeError):
            self.advance('retry')
        self.assertEqual(self.service.get_player('a'), before)
        self.service._reward = original
        self.assertEqual(self.advance('retry')['quantity'], 4)

    def test_invalid_daily_farming_rules_and_client_selected_prizes_rejected(self):
        catalog = deepcopy(self.catalog)
        catalog['objectives']['counters']['attendance']['source']['rules'][0]['measure'] = 'count'
        with self.assertRaises(DomainError):
            validate_catalog(catalog)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.service.execute('a', {'command': 'calendar.advance', 'idempotency_key': 'forged', 'parameters': {'day': 9}})
        self.assertEqual(self.service.get_player('a'), before)

    def test_calendar_only_commands_share_one_timestamp_without_objective_configuration(self):
        catalog = deepcopy(self.catalog)
        del catalog['objectives']
        service = GameService(Path(self.temp.name) / 'calendar-only.sqlite', catalog)
        service.create_player({'player_id': 'a', 'display_name': 'A'})
        day_end = timestamp('2028-02-01') + 86399
        ticks = iter([day_end, day_end + 2, day_end + 3])
        service.clock = lambda: next(ticks)
        result = service.execute('a', {'command': 'calendar.advance', 'idempotency_key': 'boundary', 'parameters': {}})
        self.assertEqual(result['server_time'], day_end)
        self.assertEqual(result['calendar_state']['day'], day_end // 86400)
        self.assertTrue(all(row['day'] == day_end // 86400 for row in result['calendar_awards']))
        self.assertNotIn('_transaction_activity', result['player'])

    def test_local_estimated_board_policy_and_split_remainder_are_neutral_configuration(self):
        definitions = estimated_boards('scout-fragment', 'promotion-credit', 'donation-credit', 42)
        catalog = deepcopy(self.catalog)
        catalog['calendars'] = definitions
        validate_catalog(catalog)
        self.assertEqual(sum(row['inventory']['scout-fragment'] for row in definitions['weekly-fragments']['rewards']), 50)
        self.assertEqual(definitions['daily-promotion']['mode'], 'repeat')
        self.assertTrue(definitions['daily-donations']['requires_alliance'])
        distribution = split_inventory_awards('orb-fragment', 100)
        self.assertEqual(sum(row['inventory']['orb-fragment'] for row in distribution), 100)
        self.assertEqual([row['inventory']['orb-fragment'] for row in distribution], [15, 15, 14, 14, 14, 14, 14])

    def test_ordered_board_xp_can_unlock_later_board_and_awards_level_bonus_atomically(self):
        catalog = deepcopy(DEMO_CATALOG)
        catalog['calendars'] = {
            'first': {'mode': 'finite', 'required_level': 1, 'order': 0, 'rewards': [{'experience': 100}]},
            'later': {'mode': 'repeat', 'required_level': 2, 'order': 1, 'rewards': [{'wallet': {'credits': 1}}]}}
        catalog['level_rewards'] = {'policy_id': 'authored-calendar', 'awards': {'2': {'wallet': {'credits': 5}}}}
        service = GameService(Path(self.temp.name) / 'levels.sqlite', catalog)
        service.create_player({'player_id': 'a', 'display_name': 'A'})
        result = service.execute('a', {'command': 'calendar.advance', 'idempotency_key': 'first', 'parameters': {}})
        self.assertEqual([row['calendar_id'] for row in result['calendar_awards']], ['first', 'later'])
        self.assertEqual(result['levels_gained'], [2])
        self.assertEqual(result['player']['wallet']['credits'], 106)

    def test_trusted_legacy_attendance_cutover_does_not_regrant_earned_day(self):
        day = self.now // 86400
        historic = {'boards': {
            'finite': {'step': 1, 'period': 'lifetime', 'last_day': day,
                       'receipts': [{'day': day, 'period': 'lifetime', 'step': 1}]},
            'monthly': {'step': 1, 'period': '2028-02', 'last_day': day,
                        'receipts': [{'day': day, 'period': '2028-02', 'step': 1}]},
        }}
        before = self.service.get_player('a')
        # The original already granted these prizes into the old inventory.
        # This operation imports its receipt only: NO duplicated rewards.
        first = import_historic_attendance(self.service, 'a', historic)
        self.assertEqual(first['imported_receipts'], {'finite': 1, 'monthly': 1})
        migrated = self.service.get_player('a')
        self.assertEqual(migrated['inventory'], before['inventory'])
        self.assertEqual(migrated['wallet'], before['wallet'])
        self.assertEqual(import_historic_attendance(self.service, 'a', historic)['imported_receipts'], {})
        self.assertEqual(self.service.get_player('a'), migrated)
        current = self.advance('post-import')
        self.assertNotIn('finite', [r['calendar_id'] for r in current['calendar_awards']])
        self.assertNotIn('monthly', [r['calendar_id'] for r in current['calendar_awards']])
        self.assertEqual(current['player']['inventory'].get('finite', 0), 0)
        self.assertEqual(current['player']['inventory'].get('monthly', 0), 0)
        self.now += 86400
        subsequent = self.advance('next-day')
        self.assertIn('finite', [r['calendar_id'] for r in subsequent['calendar_awards']])
        self.assertIn('monthly', [r['calendar_id'] for r in subsequent['calendar_awards']])
        self.assertEqual(subsequent['player']['inventory']['finite'], 2)
        self.assertEqual(subsequent['player']['inventory']['monthly'], 2)
        restarted = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(restarted.get_player('a'), subsequent['player'])

    def test_historic_import_validates_all_boards_before_atomic_write(self):
        day = self.now // 86400
        valid = {'boards': {'finite': {'step': 1, 'period': 'lifetime',
            'last_day': day, 'receipts': [{'day': day, 'period': 'lifetime', 'step': 1}]}}}
        original = self.service.get_player('a')
        for candidate in (
            {'boards': {'unknown': valid['boards']['finite']}},
            {'boards': {'finite': {**valid['boards']['finite'], 'last_day': day + 1}}},
            {'boards': {'finite': {**valid['boards']['finite'], 'receipts': [
                {'day': day, 'period': 'lifetime', 'step': 1}] * 2}}},
            {'boards': {'monthly': {**valid['boards']['finite'], 'period': '2028-03'}}},
        ):
            with self.assertRaises(DomainError):
                import_historic_attendance(self.service, 'a', candidate)
            self.assertEqual(self.service.get_player('a'), original)
        import_historic_attendance(self.service, 'a', valid)
        after = self.service.get_player('a')
        with self.assertRaises(DomainError) as error:
            import_historic_attendance(self.service, 'a',
                {'boards': {'finite': {**valid['boards']['finite'], 'step': 2,
                    'receipts': [{'day': day, 'period': 'lifetime', 'step': 2}]}}})
        self.assertEqual(error.exception.code, 'legacy_calendar_conflict')
        self.assertEqual(self.service.get_player('a'), after)

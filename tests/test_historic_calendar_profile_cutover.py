"""A complete player cutover must not farm already paid calendar days."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError
from game_server.profile_cutover import import_complete_player
from game_server.service import GameService


def ts(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


class CalendarProfileCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = ts('2026-10-30')
        self.day = ts('2026-10-29') // 86400
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['calendars'] = {
            'claimed': {'mode': 'finite', 'required_level': 1, 'order': 0,
                        'rewards': [{'wallet': {'credits': 5}}, {'wallet': {'credits': 7}}]},
            'gated': {'mode': 'repeat', 'required_level': 4, 'order': 1,
                      'rewards': [{'wallet': {'credits': 100}}]}}
        basic = deepcopy(self.catalog)
        basic.pop('calendars')
        original = GameService(Path(self.temp.name) / 'original.sqlite', basic,
                               clock=lambda: self.now)
        self.snapshot = original.create_player({'player_id': 'migrant',
                                                'display_name': 'Retained player'})
        self.snapshot['wallet']['credits'] += 5
        self.snapshot['calendar_progress'] = {
            'claimed': {'step': 1, 'period': 'lifetime', 'last_day': self.day}}
        self.snapshot['calendar_receipts'] = {'claimed': {
            'lifetime:' + str(self.day): {
                'day': self.day, 'period': 'lifetime', 'step': 1, 'imported_legacy': True}}}
        self.service = GameService(Path(self.temp.name) / 'public.sqlite', self.catalog,
                                   clock=lambda: self.now)

    def provenance(self, snapshot):
        return {'source_digest': 'a' * 64,
                'source_sections': ['profile', 'operations'],
                'section_map': {'profile': sorted(snapshot), 'operations': ['legacy_receipts']},
                'used_command_keys': []}

    def test_claimed_day_is_consumed_and_next_day_pays_only_next_prize(self):
        source = deepcopy(self.snapshot)
        self.assertEqual(import_complete_player(self.service, source,
                                                self.provenance(source))['status'], 'imported')
        self.service.clock = lambda: ts('2026-10-29') + 80000
        self.assertEqual(self.service.execute('migrant', {'command': 'calendar.advance',
            'idempotency_key': 'first', 'parameters': {}})['calendar_awards'], [])
        self.assertEqual(self.service.get_player('migrant')['wallet']['credits'],
                         self.snapshot['wallet']['credits'])
        self.service.clock = lambda: ts('2026-10-30')
        claim = self.service.execute('migrant', {'command': 'calendar.advance',
            'idempotency_key': 'new-day', 'parameters': {}})
        self.assertEqual(len(claim['calendar_awards']), 1)
        self.assertEqual(claim['calendar_awards'][0]['step'], 2)
        self.assertEqual(claim['player']['wallet']['credits'],
                         self.snapshot['wallet']['credits'] + 7)
        self.assertEqual(self.service.execute('migrant', {'command': 'calendar.advance',
            'idempotency_key': 'new-day', 'parameters': {}}), claim)

    def test_malformed_or_incomplete_receipts_block_import_without_player_creation(self):
        cases = []
        for remove in ('calendar_progress', 'calendar_receipts'):
            s = deepcopy(self.snapshot)
            s.pop(remove)
            cases.append(s)
        for change in (
                lambda s: s['calendar_progress']['claimed'].update(step=2),
                lambda s: s['calendar_progress']['claimed'].update(last_day=self.day + 3),
                lambda s: s['calendar_progress']['claimed'].update(period='2026-10'),
                lambda s: s['calendar_receipts']['claimed']['lifetime:' + str(self.day)].update(step=2),
                lambda s: s['calendar_receipts']['claimed']['lifetime:' + str(self.day)].update(imported_legacy=False),
                lambda s: s['calendar_receipts']['claimed']['lifetime:' + str(self.day)].update(rewards={'wallet': {'credits': 5}}),
                lambda s: s['calendar_receipts']['claimed'].update({'lifetime:' + str(self.day + 1): {
                    'day': self.day + 1, 'step': 4, 'period': 'lifetime', 'imported_legacy': True}}),
                lambda s: s['calendar_progress'].update(unknown={'step': 1, 'period': 'lifetime', 'last_day': self.day}),
        ):
            s = deepcopy(self.snapshot)
            change(s)
            cases.append(s)
        for candidate in cases:
            with self.subTest(candidate=candidate.get('calendar_progress')):
                with self.assertRaises(DomainError):
                    import_complete_player(self.service, candidate, self.provenance(candidate))
        with self.assertRaises(DomainError) as error:
            self.service.get_player('migrant')
        self.assertEqual(error.exception.code, 'player_not_found')

    def test_consecutive_gap_resets_sequence_without_repaying_old_days(self):
        self.catalog['calendars']['claimed']['mode'] = 'consecutive'
        self.service = GameService(Path(self.temp.name) / 'consecutive.sqlite',
                                   self.catalog, clock=lambda: self.now)
        snapshot = deepcopy(self.snapshot)
        older = self.day - 3
        previous = self.day - 2
        snapshot['calendar_progress']['claimed'] = {
            'step': 2, 'period': 'lifetime', 'last_day': previous}
        snapshot['calendar_receipts']['claimed'] = {
            'lifetime:' + str(older): {'day': older, 'period': 'lifetime',
                                       'step': 1, 'imported_legacy': True},
            'lifetime:' + str(previous): {'day': previous, 'period': 'lifetime',
                                          'step': 2, 'imported_legacy': True}}
        import_complete_player(self.service, snapshot, self.provenance(snapshot))
        view = self.service.get_calendars('migrant')['boards']['claimed']
        self.assertEqual(view['step'], 0)
        self.assertTrue(view['reset_consecutive'])
        granted = self.service.execute('migrant', {'command': 'calendar.advance',
            'idempotency_key': 'resume', 'parameters': {}})
        self.assertEqual(granted['calendar_awards'][0]['step'], 1)
        self.assertEqual(granted['player']['wallet']['credits'], snapshot['wallet']['credits'] + 5)

    def test_monthly_rollover_retains_last_month_receipt_and_starts_new_period(self):
        self.catalog['calendars']['claimed']['mode'] = 'monthly'
        rollover = ts('2026-11-01')
        self.service = GameService(Path(self.temp.name) / 'monthly.sqlite',
                                   self.catalog, clock=lambda: rollover)
        snapshot = deepcopy(self.snapshot)
        snapshot['calendar_progress']['claimed']['period'] = '2026-10'
        original = snapshot['calendar_receipts']['claimed'].pop('lifetime:' + str(self.day))
        original['period'] = '2026-10'
        snapshot['calendar_receipts']['claimed']['2026-10:' + str(self.day)] = original
        import_complete_player(self.service, snapshot, self.provenance(snapshot))
        granted = self.service.execute('migrant', {'command': 'calendar.advance',
            'idempotency_key': 'next-month', 'parameters': {}})
        self.assertEqual(granted['calendar_awards'][0]['period'], '2026-11')
        self.assertEqual(granted['calendar_awards'][0]['step'], 1)
        self.assertEqual(len(granted['player']['calendar_receipts']['claimed']), 2)


if __name__ == '__main__':
    unittest.main()

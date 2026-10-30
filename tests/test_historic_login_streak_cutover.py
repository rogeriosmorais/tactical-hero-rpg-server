"""Trusted offline login-streak seed, with no fabricated login dates or grants."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DomainError
from game_server.profile_cutover import import_complete_player
from game_server.service import GameService
from test_objectives import objective_catalog


class HistoricalLoginStreakTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = objective_catalog()
        self.today = 2 * 86400 + 100
        self.service = GameService(Path(self.temp.name) / 'public.sqlite', self.catalog,
                                   clock=lambda: self.today)
        source = GameService(Path(self.temp.name) / 'old-neutral.sqlite', self.catalog,
                             clock=lambda: 86400 + 1)
        self.snapshot = source.create_player({'player_id': 'migrant', 'display_name': 'Migrant'})
        self.snapshot.update(created_at=86400 + 1,
                             level_reached_at={'1': 86400 + 1},
                             login_days=[1, 2],
                             login_streak_seed={'day': 1, 'length': 4},
                             activity_windows={'login': {'2': 1}})

    @staticmethod
    def provenance(snapshot):
        return {'source_digest': 'c' * 64,
                'source_sections': ['neutral-snapshot', 'legacy-operations'],
                'section_map': {'neutral-snapshot': sorted(snapshot),
                                'legacy-operations': ['legacy_receipts']},
                'used_command_keys': []}

    def command(self, name, nonce):
        return self.service.execute('migrant', {'command': name,
                                                'idempotency_key': nonce, 'parameters': {}})

    def test_seed_preserves_original_streak_and_prevents_double_daily_login(self):
        snapshot = deepcopy(self.snapshot)
        self.assertEqual(import_complete_player(self.service, snapshot,
                         self.provenance(snapshot))['status'], 'imported')
        self.assertEqual(self.service.get_objectives('migrant')['counters']['streak']['value'], 5)
        before = self.service.get_player('migrant')
        same_day = self.command('player.login', 'same-day')
        self.assertFalse(same_day['new_login'])
        self.assertEqual(same_day['player']['activity_windows']['login']['2'], 1)
        self.assertEqual(self.service.get_objectives('migrant')['counters']['streak']['value'], 5)
        self.today += 86400
        newer = self.command('player.login', 'next-day')
        self.assertTrue(newer['new_login'])
        self.assertEqual(self.service.get_objectives('migrant')['counters']['streak']['value'], 6)
        self.assertEqual(self.command('player.login', 'next-day'), newer)
        self.service = GameService(self.service.database, self.catalog, clock=lambda: self.today)
        self.assertEqual(self.service.get_objectives('migrant')['counters']['streak']['value'], 6)
        self.today += 2 * 86400
        self.command('player.login', 'after-gap')
        self.assertEqual(self.service.get_objectives('migrant')['counters']['streak']['value'], 1)
        self.assertEqual(before['login_streak_seed'], {'day': 1, 'length': 4})

    def test_seed_requires_exact_anchor_and_real_observed_days(self):
        bad = []
        for value in (-1, True, '4', 1000001):
            candidate = deepcopy(self.snapshot)
            candidate['login_streak_seed']['length'] = value
            bad.append(candidate)
        for value in ([-1, 1, 2], [2], [1, 1, 2], [1, 2, 3], [1, '2']):
            candidate = deepcopy(self.snapshot)
            candidate['login_days'] = value
            bad.append(candidate)
        for candidate in bad:
            with self.subTest(seed=candidate['login_streak_seed'], days=candidate['login_days']):
                with self.assertRaises(DomainError):
                    import_complete_player(self.service, candidate, self.provenance(candidate))
        with self.assertRaises(DomainError):
            self.service.get_player('migrant')

    def test_original_zero_streak_is_not_upgraded_to_an_unearned_one(self):
        snapshot = deepcopy(self.snapshot)
        snapshot['login_streak_seed']['length'] = 0
        import_complete_player(self.service, snapshot, self.provenance(snapshot))
        self.assertEqual(self.service.get_objectives('migrant')['counters']['streak']['value'], 1)
        self.today += 2 * 86400
        self.command('player.login', 'new-period')
        self.assertEqual(self.service.get_objectives('migrant')['counters']['streak']['value'], 1)


if __name__ == '__main__':
    unittest.main()

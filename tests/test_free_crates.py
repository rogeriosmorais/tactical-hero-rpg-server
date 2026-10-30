"""Migrated timed-crate gameplay remains public-server-owned and replay-safe."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError
from game_server.service import GameService


class FreeCrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'game.sqlite'
        self.now = 1000
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['free_crates'] = {
            'capacity': 2, 'refill_seconds': 30,
            'rotation': ['basic', 'rare', 'basic'],
            'rewards': {'basic': {'wallet': {'credits': 5}},
                        'rare': {'inventory': {'badge': 1}}},
        }
        self.game = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.game.create_player({'player_id': 'qa', 'display_name': 'QA'})
        self.game.create_player({'player_id': 'personal', 'display_name': 'Personal'})

    def claim(self, key, player='qa', parameters=None):
        return self.game.execute(player, {'command': 'free_crate.claim',
                                           'idempotency_key': key,
                                           'parameters': {} if parameters is None else parameters})

    def test_separate_accounts_replay_and_persistence(self):
        preview = self.game.get_free_crates('qa')
        self.assertEqual(preview['schedule']['available'], ['basic', 'rare'])
        self.assertEqual(preview['server_time'], self.now)
        self.assertNotIn('free_crates', self.game.get_player('qa'))
        first = self.claim('first')
        self.assertEqual(first['crate_id'], 'basic')
        self.assertEqual(first['player']['wallet']['credits'], 105)
        self.assertEqual(first['schedule']['available'], ['rare'])
        self.assertEqual(self.claim('first'), first)
        second = self.claim('second')
        self.assertEqual(second['crate_id'], 'rare')
        self.assertEqual(second['player']['inventory']['badge'], 1)
        with self.assertRaises(DomainError) as exhausted:
            self.claim('third')
        self.assertEqual(exhausted.exception.code, 'no_free_crates')
        self.assertNotIn('free_crates', self.game.get_player('personal'))

        self.now += 30
        self.game = GameService(self.path, self.catalog, clock=lambda: self.now)
        refreshed = self.claim('third')
        self.assertEqual(refreshed['crate_id'], 'basic')
        self.assertEqual(refreshed['player']['wallet']['credits'], 110)
        self.assertEqual(self.claim('first'), first)
        self.assertEqual(self.claim('first-personal', player='personal')['player']['wallet']['credits'], 105)
        self.assertEqual(self.game.get_player('qa')['inventory']['badge'], 1)

    def test_invalid_claim_does_not_mutate_or_accept_prizes(self):
        before = self.game.get_player('qa')
        with self.assertRaises(DomainError):
            self.claim('fake', parameters={'rewards': {'wallet': {'credits': 99999}}})
        self.assertEqual(self.game.get_player('qa'), before)
        self.assertEqual(self.claim('first')['crate_id'], 'basic')
        with self.assertRaises(DomainError) as changed:
            self.game.execute('qa', {'command': 'character.train',
                                     'idempotency_key': 'first',
                                     'parameters': {'character_id': 'scout', 'levels': 1}})
        self.assertEqual(changed.exception.code, 'idempotency_conflict')

    def test_invalid_config_cannot_start_or_change_database(self):
        for patch in ({'capacity': 0}, {'rotation': ['unmapped', 'rare']},
                      {'rewards': {'basic': {'wallet': {'credits': -3}},
                                   'rare': {'inventory': {'badge': 1}}}}):
            catalog = deepcopy(self.catalog)
            catalog['free_crates'].update(patch)
            with self.assertRaises(DomainError):
                GameService(self.path, catalog)
        self.assertEqual(self.game.get_player('qa')['wallet']['credits'], 100)


if __name__ == '__main__':
    unittest.main()

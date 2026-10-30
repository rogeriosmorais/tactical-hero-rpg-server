"""Historical level bonuses are consumed receipt facts, never replayed grants."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError
from game_server.profile_cutover import import_complete_player
from game_server.service import GameService


class HistoricLevelBonusCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog.update(maximum_player_level=4,
                            experience_thresholds=[0, 50, 130, 240],
                            level_rewards={'policy_id': 'authored-v1', 'awards': {
                                '2': {'wallet': {'credits': 7}},
                                '3': {'inventory': {'badge': 2}},
                                '4': {'wallet': {'credits': 11}}}})
        self.catalog['offers']['training-kit']['rewards'] = {'experience': 150}
        self.service = GameService(Path(self.temp.name) / 'public.sqlite', self.catalog,
                                   clock=lambda: 5000)
        original = GameService(Path(self.temp.name) / 'fixture.sqlite', self.catalog,
                               clock=lambda: 2000)
        self.snapshot = original.create_player({'player_id': 'migrant',
                                                 'display_name': 'Historical commander'})
        self.snapshot.update(level=3, experience=150, version=8,
                             level_reward_claims={
                                 '2': {'policy_id': 'authored-v1', 'historic': True},
                                 '3': {'policy_id': 'authored-v1', 'historic': True}})
        self.snapshot['inventory']['badge'] = 2

    def provenance(self, snapshot):
        return {'source_digest': 'e' * 64,
                'source_sections': ['neutral', 'operations'],
                'section_map': {'neutral': sorted(snapshot),
                                'operations': ['legacy_receipts']},
                'used_command_keys': ['historical-level-reward']}

    def test_historical_rewards_do_not_repay_and_future_level_still_grants_once(self):
        original = deepcopy(self.snapshot)
        imported = import_complete_player(self.service, original, self.provenance(original))
        self.assertEqual(imported['status'], 'imported')
        self.assertEqual(import_complete_player(self.service, original,
                                               self.provenance(original))['status'], 'already_imported')
        self.assertEqual(self.service.get_player('migrant')['inventory']['badge'], 2)
        self.assertEqual(self.service.get_progression('migrant')['progression']['awarded_levels'],
                         original['level_reward_claims'])
        purchase = {'command': 'shop.purchase', 'idempotency_key': 'fresh-buy',
                    'parameters': {'offer_id': 'training-kit', 'quantity': 1}}
        done = self.service.execute('migrant', purchase)
        self.assertEqual(done['levels_gained'], [4])
        self.assertEqual([row['level'] for row in done['level_rewards']], [4])
        self.assertEqual(done['player']['inventory']['badge'], 2)
        self.assertEqual(done['player']['level_reward_claims']['2'],
                         {'policy_id': 'authored-v1', 'historic': True})
        restart = GameService(self.service.database, self.catalog, clock=lambda: 5001)
        self.assertEqual(restart.execute('migrant', purchase), done)

    def test_invalid_historic_level_receipts_block_atomic_import(self):
        missing_history = deepcopy(self.snapshot)
        missing_history.pop('level_reward_claims')
        with self.assertRaises(DomainError) as missing:
            import_complete_player(self.service, missing_history,
                                   self.provenance(missing_history))
        self.assertEqual(missing.exception.code, 'cutover_incomplete')
        for bad_key, bad_receipt in (
                ('2', {'policy_id': 'wrong', 'historic': True}),
                ('2', {'policy_id': 'authored-v1', 'historic': False}),
                ('2', {'policy_id': 'authored-v1', 'historic': True, 'at': 3000}),
                ('2', {'policy_id': 'authored-v1', 'at': 'unknown'}),
                ('2', {'policy_id': 'authored-v1', 'at': 6000}),
                ('4', {'policy_id': 'authored-v1', 'historic': True}),
                ('02', {'policy_id': 'authored-v1', 'historic': True}),
                ('100000', {'policy_id': 'authored-v1', 'historic': True})):
            snapshot = deepcopy(self.snapshot)
            snapshot['level_reward_claims'][bad_key] = bad_receipt
            with self.subTest(key=bad_key, receipt=bad_receipt), self.assertRaises(DomainError):
                import_complete_player(self.service, snapshot, self.provenance(snapshot))
        with self.assertRaises(DomainError) as missing:
            self.service.get_player('migrant')
        self.assertEqual(missing.exception.code, 'player_not_found')


if __name__ == '__main__':
    unittest.main()

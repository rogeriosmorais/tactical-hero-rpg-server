"""Public receiver preserves already-claimed legacy pass prizes, no new awards."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from game_server.catalog import DomainError
from game_server.profile_cutover import import_complete_player, validate_snapshot
from game_server.service import GameService
from test_objectives import objective_catalog


class HistoricPassReceiverTests(unittest.TestCase):
    def test_imported_claims_and_transaction_points_remain_earned_and_nonrepeatable(self):
        with TemporaryDirectory() as tmp:
            catalog = objective_catalog()
            old = GameService(Path(tmp) / 'normalizing.sqlite', deepcopy(catalog), clock=lambda: 1010)
            old.create_player({'player_id': 'migrant', 'display_name': 'Migrated'})

            def command(name, nonce, **parameters):
                return old.execute('migrant', {'command': name, 'idempotency_key': nonce,
                                               'parameters': parameters})

            started = command('mission.begin', 'begin', mission_id='training.1', team=['scout'])
            command('mission.complete', 'win', battle_id=started['battle']['battle_id'],
                    mission_id='training.1', outcome='victory', stars=3)
            command('mission.sweep', 'sweep', mission_id='training.1', quantity=1)
            command('shop.purchase', 'purchase', offer_id='training-kit', quantity=1)
            command('objective.claim', 'earn-pass-points', objective_ids=['win'])
            snapshot = old.get_player('migrant')
            self.assertEqual(old.get_passes('migrant')['passes']['season']['current']['points'], 10)
            old_points = deepcopy(snapshot['activity_windows'])
            source_inventory = deepcopy(snapshot['inventory'])
            source_wallet = deepcopy(snapshot['wallet'])
            # The private archived-pass translator emits these exact two
            # fields; receipts and reward balances are preserved separately.
            snapshot['pass_sources'] = {'season': {'0': {'selected_at': 1000, 'premium': True}}}
            snapshot['pass_claims'] = {'season': {'0': ['free-zero', 'free-ten']}}
            manifest = {
                'source_digest': 'a' * 64,
                'source_sections': ['account', 'wallet', 'inventory', 'roster',
                                    'missions', 'offers', 'pass-history', 'receipts'],
                'section_map': {
                    'account': ['player_id', 'display_name', 'version', 'level', 'experience'],
                    'wallet': ['wallet'], 'inventory': ['inventory'],
                    'roster': ['characters'], 'missions': ['missions'],
                    'offers': ['offer_purchases'],
                    'pass-history': ['pass_sources', 'pass_claims', 'activity_windows',
                                     'created_at', 'login_days', 'objective_claims',
                                     'level_reached_at'],
                    'receipts': ['legacy_receipts']},
                'used_command_keys': ['historic-free-zero', 'historic-free-ten']}
            mapped = {field for fields in manifest['section_map'].values()
                      for field in fields}
            unmapped = sorted(set(snapshot) - mapped)
            if unmapped:
                manifest['source_sections'].append('generated-fields')
                manifest['section_map']['generated-fields'] = unmapped
            server = GameService(Path(tmp) / 'target.sqlite', deepcopy(catalog), clock=lambda: 1010)
            invalid = []
            changed = deepcopy(snapshot)
            changed['pass_claims']['season']['0'].append('unknown-prize')
            invalid.append(changed)
            changed = deepcopy(snapshot)
            changed['pass_claims']['season']['0'].append('free-ten')
            invalid.append(changed)
            changed = deepcopy(snapshot)
            changed['pass_sources']['season']['0']['premium'] = False
            changed['pass_claims']['season']['0'].append('premium-ten')
            invalid.append(changed)
            changed = deepcopy(snapshot)
            changed['pass_sources']['season']['bad-period'] = changed['pass_sources']['season'].pop('0')
            invalid.append(changed)
            changed = deepcopy(snapshot)
            changed['pass_sources']['season'] = {}
            invalid.append(changed)
            changed = deepcopy(snapshot)
            changed['pass_claims']['unknown-pass'] = {'0': ['free-zero']}
            invalid.append(changed)
            for candidate in invalid:
                with self.assertRaises(DomainError):
                    validate_snapshot(server, candidate)
            result = import_complete_player(server, snapshot, manifest)
            self.assertEqual(result['status'], 'imported')
            imported = server.get_player('migrant')
            self.assertEqual(imported['activity_windows'], old_points)
            self.assertEqual(imported['wallet'], source_wallet)
            self.assertEqual(imported['inventory'], source_inventory)
            state = server.get_passes('migrant')['passes']['season']['current']
            self.assertEqual(state['points'], 10)
            self.assertTrue(state['premium'])
            self.assertEqual({row['reward_id'] for row in state['rewards'] if row['claimed']},
                             {'free-zero', 'free-ten'})
            before = server.get_player('migrant')
            with self.assertRaises(DomainError):
                server.execute('migrant', {'command': 'pass.claim', 'idempotency_key': 'repeat',
                    'parameters': {'pass_id': 'season', 'period_key': '0',
                                   'reward_ids': ['free-ten']}})
            with self.assertRaises(DomainError):
                server.execute('migrant', {'command': 'pass.claim',
                    'idempotency_key': 'historic-free-ten',
                    'parameters': {'pass_id': 'season', 'period_key': '0',
                                   'reward_ids': ['free-ten']}})
            self.assertEqual(server.get_player('migrant'), before)
            self.assertEqual(import_complete_player(server, snapshot, manifest)['status'],
                             'already_imported')


if __name__ == '__main__':
    unittest.main()

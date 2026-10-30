"""Trusted original access grants do not replay unlock prizes or silently reset."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError
from game_server.profile_cutover import import_complete_player
from game_server.service import GameService


class HistoricAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['access_rules'] = {
            'earned': {'required_level': 2, 'mission_ids': ['training.1'],
                       'item_id': 'earned-license'},
            'locked': {'required_level': 4, 'mission_ids': [],
                       'item_id': 'locked-license'}}
        self.catalog['offers']['training-kit']['rewards'] = {'experience': 300}
        self.catalog['offers']['training-kit']['purchase_limit'] = 10
        self.service = GameService(Path(self.tmp.name) / 'target.sqlite', self.catalog,
                                   clock=lambda: 1500)
        basic = deepcopy(self.catalog)
        basic.pop('access_rules')
        source = GameService(Path(self.tmp.name) / 'fixture.sqlite', basic,
                             clock=lambda: 1000)
        self.snapshot = source.create_player({'player_id': 'migrant', 'display_name': 'Migrant'})
        self.snapshot.update(level=2, experience=100,
                             missions={'training.1': {'wins': 1, 'best_stars': 3}},
                             access_grants={'earned': 1100})

    def manifest(self, snapshot):
        return {'source_digest': 'f' * 64,
                'source_sections': ['source', 'operations'],
                'section_map': {'source': sorted(snapshot), 'operations': ['legacy_receipts']},
                'used_command_keys': ['historical-unlock']}

    def test_existing_grant_persists_without_duplicate_token_after_future_level(self):
        snapshot = deepcopy(self.snapshot)
        receipt = import_complete_player(self.service, snapshot, self.manifest(snapshot))
        self.assertEqual(receipt['status'], 'imported')
        self.assertEqual(self.service.get_access('migrant')['access']['earned']['granted_at'], 1100)
        self.assertEqual(self.service.get_access('migrant')['access']['locked']['unlocked'], False)
        self.assertEqual(self.service.get_player('migrant')['inventory'].get('earned-license', 0), 0)
        request = {'command': 'shop.purchase', 'idempotency_key': 'new-purchase',
                   'parameters': {'offer_id': 'training-kit', 'quantity': 1}}
        awarded = self.service.execute('migrant', request)
        self.assertEqual(awarded['access_grants'], ['locked'])
        self.assertEqual(awarded['player']['access_grants']['earned'], 1100)
        self.assertEqual(awarded['player']['inventory'].get('earned-license', 0), 0)
        self.assertEqual(awarded['player']['inventory']['locked-license'], 1)
        self.assertEqual(self.service.execute('migrant', request), awarded)
        restarted = GameService(self.service.database, self.catalog, clock=lambda: 1501)
        self.assertEqual(restarted.get_access('migrant')['access']['earned']['granted_at'], 1100)

    def test_incomplete_future_unknown_or_eligible_but_unrecorded_grant_fails(self):
        cases = []
        missing = deepcopy(self.snapshot)
        missing.pop('access_grants')
        cases.append(missing)
        for grants in ({'unknown': 1100}, {'earned': 1501}, {'earned': True}, {},
                       {'earned': 1100, 'locked': -1}):
            bad = deepcopy(self.snapshot)
            bad['access_grants'] = grants
            cases.append(bad)
        with_token = deepcopy(self.snapshot)
        with_token['access_grants']['earned'] = 1100
        with_token['inventory']['locked-license'] = 1
        cases.append(with_token)
        for snapshot in cases:
            with self.subTest(grants=snapshot.get('access_grants')):
                with self.assertRaises(DomainError):
                    import_complete_player(self.service, snapshot, self.manifest(snapshot))
        with self.assertRaises(DomainError) as missing_player:
            self.service.get_player('migrant')
        self.assertEqual(missing_player.exception.code, 'player_not_found')

    def test_reward_granted_access_before_prerequisites_stays_unlocked_without_new_token(self):
        snapshot = deepcopy(self.snapshot)
        snapshot['missions'] = {}
        self.assertEqual(import_complete_player(self.service, snapshot,
                                               self.manifest(snapshot))['status'], 'imported')
        access = self.service.get_access('migrant')['access']['earned']
        self.assertTrue(access['unlocked'])
        self.assertFalse(access['eligible'])
        self.assertEqual(access['granted_at'], 1100)
        self.assertEqual(self.service.get_player('migrant')['inventory'].get('earned-license', 0), 0)


if __name__ == '__main__':
    unittest.main()

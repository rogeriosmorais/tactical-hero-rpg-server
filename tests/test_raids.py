"""Independent raid launch/participation and map ownership regressions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.raids import accessible_rooms
from game_server.service import GameService, canonical
from test_alliances import alliance_rules


def raid_catalog():
    catalog = deepcopy(DEMO_CATALOG)
    catalog['alliances'] = alliance_rules()
    catalog['alliances']['required_level'] = 1
    catalog['starting_wallet']['keys'] = 100
    catalog['raids'] = {'expedition': {'required_level': 1, 'duration_seconds': 3600,
        'join_cooldown_seconds': 10, 'lock_seconds': 600, 'team_count': 3, 'launch_cost': {'keys': 20},
        'starting_room_ids': ['entry'],
        'rooms': {'entry': {'next_room_ids': ['first'], 'node_ids': []},
                  'first': {'next_room_ids': ['exit'], 'node_ids': ['guard', 'leader']},
                  'exit': {'next_room_ids': [], 'node_ids': ['final']}},
        'nodes': {key: {'encounter_id': key + '-encounter'} for key in ('guard', 'leader', 'final')},
        'difficulties': [None, {'difficulty': 0, 'completion_percent': 60}, {'difficulty': 1, 'completion_percent': 100}],
        'maximum_launch_difficulty': 2, 'nonsequential_difficulties': False}}
    return catalog


class RaidTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000000
        self.catalog = raid_catalog()
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def command(self, command, key, **parameters):
        return self.service.execute('a', {'command': command, 'idempotency_key': key, 'parameters': parameters})

    def launch(self, key='launch', **extra):
        return self.command('raid.launch', key, raid_id='expedition', **extra)

    def join(self, raid, key='join', team=0):
        return self.command('raid.join', key, raid_id='expedition', instance_id=raid['instance_id'], team_index=team)

    def mutate_fixture(self, function):
        # Seed states produced by later combat/closure, without exposing an import API.
        with self.service.transaction() as connection:
            player = self.service._player(connection, 'a')
            function(player)
            connection.execute('UPDATE players SET data=? WHERE id=?', (canonical(player), 'a'))

    def test_concurrent_launch_cost_identity_and_receipt_survive_restart(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.launch(), range(4)))
        self.assertTrue(all(row == results[0] for row in results))
        self.assertEqual(results[0]['player']['wallet']['keys'], 80)
        self.assertEqual(results[0]['raid']['expires_at'], self.now + 3600)
        restarted = GameService(self.path, self.catalog, clock=lambda: self.now + 99999)
        self.assertEqual(restarted.execute('a', {'command': 'raid.launch', 'idempotency_key': 'launch',
            'parameters': {'raid_id': 'expedition'}}), results[0])
        with self.assertRaises(DomainError):
            self.launch('different')
        with self.assertRaises(DomainError):
            self.launch(difficulty=1)

    def test_failed_access_currency_unknown_fields_and_difficulty_leave_no_receipt(self):
        before = self.service.get_player('a')
        for parameters in ({'raid_id': 'missing'}, {'raid_id': 'expedition', 'difficulty': True},
                           {'raid_id': 'expedition', 'difficulty': 1}, {'raid_id': 'expedition', 'cost': 0}):
            with self.assertRaises(DomainError):
                self.command('raid.launch', 'retry', **parameters)
            self.assertEqual(self.service.get_player('a'), before)
        self.mutate_fixture(lambda p: p['wallet'].update(keys=19))
        poor = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.launch()
        self.assertEqual(self.service.get_player('a'), poor)
        self.mutate_fixture(lambda p: p['wallet'].update(keys=20))
        self.assertEqual(self.launch()['player']['wallet']['keys'], 0)

    def test_join_cooldown_team_and_roster_snapshot_and_profile_isolation(self):
        raid = self.launch()['raid']
        with self.assertRaises(DomainError):
            self.join(raid)
        self.now += 10
        joined = self.join(raid)['raid']
        self.assertEqual(joined['teams']['0']['members'], ['a'])
        self.assertEqual(joined['teams']['1']['members'], [])
        self.command('character.train', 'train', character_id='scout', levels=1)
        again = self.join(raid, 'again')['raid']
        self.assertEqual(again['characters'], joined['characters'])
        with self.assertRaises(DomainError):
            self.join(raid, 'switch', team=1)
        with self.assertRaises(DomainError):
            self.join(raid, 'invalid', team=True)
        self.service.create_player({'player_id': 'b', 'display_name': 'B'})
        self.assertEqual(self.service.get_raids('b')['raids'], [])
        with self.assertRaises(DomainError):
            self.service.get_raids('b', raid['instance_id'])

    def test_empty_connectors_clear_gates_and_completed_node_and_foreign_lock(self):
        raid = self.launch()['raid']
        self.now += 10
        joined = self.join(raid)['raid']
        self.assertTrue(joined['teams']['0']['rooms']['first']['accessible'])
        self.assertFalse(joined['teams']['0']['rooms']['entry']['completed'])
        self.assertFalse(joined['teams']['0']['rooms']['exit']['accessible'])
        parameters = {'raid_id': 'expedition', 'instance_id': raid['instance_id'], 'team_index': 0}
        with self.assertRaises(DomainError):
            self.command('raid.lock', 'exit', **parameters, node_id='final')
        self.mutate_fixture(lambda p: p['raids'][raid['instance_id']]['completed'].update({'0': {'guard': {'stars': 3}}}))
        with self.assertRaises(DomainError):
            self.command('raid.lock', 'complete', **parameters, node_id='guard')
        self.mutate_fixture(lambda p: p['raids'][raid['instance_id']]['completed']['0'].update(leader={'stars': 2}))
        result = self.command('raid.lock', 'final', **parameters, node_id='final')
        self.assertTrue(result['raid']['teams']['0']['rooms']['exit']['accessible'])
        self.mutate_fixture(lambda p: p['raids'][raid['instance_id']]['locks']['0'].update(leader={
            'player_id': 'other', 'display_name': 'Other', 'locked_at': self.now, 'expires_at': self.now + 30}))
        with self.assertRaises(DomainError):
            self.command('raid.release', 'foreign', **parameters, node_id='leader')

    def test_lock_expiry_release_restart_and_read_only_projection(self):
        raid = self.launch()['raid']
        self.now += 3500
        self.join(raid)
        parameters = {'raid_id': 'expedition', 'instance_id': raid['instance_id'], 'team_index': 0, 'node_id': 'guard'}
        locked = self.command('raid.lock', 'lock', **parameters)
        self.assertEqual(locked['raid']['locks']['0']['guard']['expires_at'], raid['expires_at'])
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.command('raid.lock', 'lock', **parameters), locked)
        released = self.command('raid.release', 'release', **parameters)
        self.assertIsNone(released['raid']['teams']['0']['rooms']['first']['nodes']['guard']['lock'])
        self.command('raid.lock', 'relock', **parameters)
        before = self.service.get_player('a')
        self.now = raid['expires_at']
        view = self.service.get_raids('a', raid['instance_id'])['raid']
        self.assertFalse(view['available'])
        self.assertIsNone(view['teams']['0']['rooms']['first']['nodes']['guard']['lock'])
        self.assertEqual(self.service.get_player('a'), before)
        with self.assertRaises(DomainError):
            self.command('raid.lock', 'late', **parameters)

    def test_membership_guard_expiry_and_new_instance_use_same_alliance_ledger(self):
        raid = self.launch()['raid']
        before = self.service.get_player('a')
        for command, parameters in [('alliance.leave', {'alliance_id': '1'}),
                                    ('alliance.create', {'name': 'Second Team', 'tag': 'NEXT'})]:
            with self.assertRaises(DomainError):
                self.command(command, command, **parameters)
            self.assertEqual(self.service.get_player('a'), before)
        self.now = raid['expires_at']
        self.command('alliance.create', 'new', name='Second Team', tag='NEXT')
        self.assertEqual(self.service.get_raids('a')['raids'], [])
        second = self.launch('second')['raid']
        self.assertNotEqual(second['instance_id'], raid['instance_id'])
        with self.assertRaises(DomainError):
            self.join(raid)

    def test_sequential_difficulty_requires_same_alliance_settled_history_and_preference_is_not_progress(self):
        raid = self.launch()['raid']
        self.mutate_fixture(lambda p: p['raids'][raid['instance_id']].update(status='ended', settlement={'completion_percent': 60}))
        selected = self.command('raid.set_difficulty', 'select', raid_id='expedition', difficulty=1)
        self.assertEqual(selected['progress'], {'difficulty': 0, 'completion_percent': 60, 'selected_difficulty': 1})
        self.assertEqual(self.service.get_alliances('a')['current']['raid_progress']['expedition'], selected['progress'])
        self.assertEqual(selected['player']['wallet']['keys'], 80)
        self.assertEqual(self.launch('hard', difficulty=1)['raid']['difficulty'], 1)
        with self.assertRaises(DomainError):
            self.command('raid.set_difficulty', 'locked', raid_id='expedition', difficulty=2)
        self.now += 3600
        self.command('alliance.create', 'new', name='Other Team', tag='OTHER')
        with self.assertRaises(DomainError):
            self.launch('other-alliance', difficulty=1)

    def test_catalog_rejects_orphans_duplicate_nodes_invalid_starts_and_prerequisites(self):
        for change in (lambda r: r['rooms']['entry'].update(next_room_ids=['missing']),
                       lambda r: r.update(starting_room_ids=[]),
                       lambda r: r['rooms']['entry'].update(node_ids=['guard']),
                       lambda r: r['difficulties'][1].update(difficulty=1),
                       lambda r: r.update(maximum_launch_difficulty=3),
                       lambda r: r['rooms']['first'].update(next_room_ids=[])):
            catalog = deepcopy(self.catalog)
            change(catalog['raids']['expedition'])
            with self.assertRaises(DomainError):
                validate_catalog(catalog)
        # Cycles terminate, and uncleared nodes still gate traversal.
        rule = deepcopy(self.catalog['raids']['expedition'])
        rule['rooms']['exit']['next_room_ids'] = ['entry']
        self.assertEqual(accessible_rooms(rule, {'completed': {}}, 0), {'entry', 'first'})

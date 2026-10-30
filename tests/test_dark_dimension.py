"""Public Dark Dimension runs, persistent node damage and healing prices."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


def dark_catalog():
    catalog = deepcopy(DEMO_CATALOG)
    catalog['starting_wallet'].update(heal=20, cores=100)
    catalog['dark_dimension'] = {
        'required_level': 1,
        'health_caps': {key: 100 for key in catalog['characters']},
        'healing': {'currency_id': 'heal', 'premium_currency_id': 'cores',
                    'premium_conversion': 2, 'heal_costs': [4, 8],
                    'revive_costs': [6, 12]},
        'campaigns': {'sample': {
            'starting_room': 'entry',
            'rooms': {'entry': {'next_rooms': ['final'], 'node_ids': ['first']},
                      'final': {'next_rooms': [], 'node_ids': ['last']}},
            'nodes': {
                'first': {'maximum_team_size': 5, 'required_character_ids': ['scout'],
                          'enemies': {'enemy-one': 100, 'enemy-two': 200},
                          'rewards': {'wallet': {'credits': 10}},
                          'first_run_rewards': {'inventory': {'first-badge': 1}},
                          'reward_tables': [], 'first_run_reward_tables': []},
                'last': {'maximum_team_size': 5, 'required_character_ids': ['scout'],
                         'enemies': {'boss': 500},
                         'rewards': {'wallet': {'credits': 20}},
                         'first_run_rewards': {'wallet': {'credits': 5}},
                         'reward_tables': [], 'first_run_reward_tables': []}},
            'completion_rewards': {'wallet': {'credits': 30}},
            'completion_reward_tables': []}}}
    return catalog


class DarkDimensionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'dark.sqlite'
        self.now = 1000000
        self.catalog = dark_catalog()
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'hero', 'display_name': 'Hero'})

    def command(self, key, name, **parameters):
        return self.service.execute('hero', {
            'command': name, 'idempotency_key': key, 'parameters': parameters})

    def start(self, key='start'):
        return self.command(key, 'dark.start', campaign_id='sample')

    def begin(self, run_id, node='first', key='begin'):
        return self.command(key, 'dark.begin', campaign_id='sample',
                            run_id=run_id, node_id=node, team=['scout'])['battle']

    def complete(self, run_id, battle, *, key, win, hp=50, enemy_health=None):
        if enemy_health is None:
            enemy_health = ({name: 0 for name in battle['enemies']} if win else
                            {name: max(1, hp) for name in battle['enemies']})
        return self.command(key, 'dark.complete', campaign_id='sample',
                            run_id=run_id, node_id=battle['node_id'],
                            battle_id=battle['battle_id'], outcome='victory' if win else 'defeat',
                            duration_seconds=25,
                            allies={'scout': {'health': hp, 'energy': [0, 1, 2]}},
                            enemy_health=enemy_health)

    def test_access_initialization_rooms_and_no_mutating_reads(self):
        before = self.service.get_player('hero')
        self.assertIsNone(self.service.get_dark_dimension('hero', 'sample')['run'])
        self.assertEqual(self.service.get_player('hero'), before)
        with self.assertRaises(DomainError):
            self.command('malformed-refresh', 'dark.refresh', campaign_id='sample')
        self.assertEqual(self.service.get_player('hero'), before)
        started = self.start()
        self.assertEqual(started['run']['number'], 1)
        self.assertEqual(started['accessible_rooms'], ['entry'])
        self.assertEqual(started['available_nodes'], ['first'])
        self.assertEqual(started['run']['heroes']['scout']['health'], 100)
        self.assertEqual(self.start('idempotent')['run'], started['run'])
        self.assertEqual(self.start()['run'], started['run'])
        self.assertEqual(self.service.get_dark_dimension('hero', 'sample')['run']['run'],
                         started['run'])
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.service.get_dark_dimension('hero')['history'], {'sample': 1})

    def test_defeat_retains_partial_damage_hero_health_and_daily_heal_no_rewards(self):
        run_id = self.start()['run']['run_id']
        battle = self.begin(run_id)
        lost = self.complete(run_id, battle, key='loss', win=False, hp=10,
                             enemy_health={'enemy-one': 40, 'enemy-two': 200})
        self.assertEqual(lost['run']['run']['enemy_states']['first'],
                         {'enemy-one': 40, 'enemy-two': 200})
        self.assertEqual(lost['run']['run']['heroes']['scout']['health'], 10)
        self.assertAlmostEqual(lost['run']['run']['wave_progress']['first'], 60 / 300)
        self.assertEqual(lost['run']['run']['completed'], {})
        self.assertEqual(lost['rewards'], {})
        self.now += 86400
        before = self.service.get_player('hero')
        self.assertTrue(self.service.get_dark_dimension('hero', 'sample')['needs_reset'])
        self.assertEqual(self.service.get_player('hero'), before)
        fresh = self.command('daily', 'dark.refresh', campaign_id='sample', run_id=run_id)
        self.assertTrue(fresh['daily_restored'])
        self.assertEqual(fresh['run']['heroes']['scout']['health'], 100)
        self.assertEqual(fresh['run']['enemy_states']['first'],
                         {'enemy-one': 40, 'enemy-two': 200})
        self.assertFalse(self.command('again', 'dark.refresh', campaign_id='sample',
                                      run_id=run_id)['daily_restored'])
        next_battle = self.begin(run_id, key='resume')
        self.assertEqual(next_battle['enemies'], {'enemy-one': 40, 'enemy-two': 200})
        self.assertEqual(self.complete(run_id, next_battle, key='win', win=True)['run']['available_nodes'], ['last'])

    def test_completed_first_run_final_and_repeat_no_double_first_reward(self):
        run_id = self.start()['run']['run_id']
        first = self.begin(run_id)
        first_response = self.complete(run_id, first, key='first', win=True)
        self.assertEqual(first_response['rewards']['wallet']['credits'], 10)
        self.assertEqual(first_response['rewards']['inventory']['first-badge'], 1)
        last = self.begin(run_id, 'last', key='final-begin')
        ended = self.complete(run_id, last, key='final', win=True)
        self.assertEqual(ended['rewards']['wallet']['credits'], 55)
        self.assertTrue(ended['completed_run'])
        self.assertTrue(ended['run']['run']['completed_run'])
        self.assertEqual(self.command('final', 'dark.complete', campaign_id='sample',
            run_id=run_id, node_id=last['node_id'], battle_id=last['battle_id'],
            outcome='victory', duration_seconds=25,
            allies={'scout': {'health': 50, 'energy': [0, 1, 2]}},
            enemy_health={'boss': 0}), ended)
        with self.assertRaises(DomainError):
            self.begin(run_id, key='blocked-replay')
        repeated = self.start('second-run')['run']
        self.assertEqual(repeated['number'], 2)
        second = self.begin(repeated['run_id'], key='second-begin')
        scored = self.complete(repeated['run_id'], second, key='second-first', win=True)
        self.assertEqual(scored['rewards']['inventory'], {})
        self.assertEqual(scored['rewards']['wallet']['credits'], 10)
        other = self.begin(repeated['run_id'], 'last', key='second-final')
        second_ending = self.complete(repeated['run_id'], other, key='second-end', win=True)
        self.assertEqual(second_ending['rewards']['wallet']['credits'], 20)

    def test_atomic_heal_revive_price_escalation_premium_fallback_and_rollback(self):
        run_id = self.start()['run']['run_id']
        battle = self.begin(run_id)
        self.complete(run_id, battle, key='loss', win=False, hp=0,
                      enemy_health={'enemy-one': 50, 'enemy-two': 200})
        with self.assertRaises(DomainError):
            self.begin(run_id, key='dead')
        before = self.service.get_player('hero')
        for request in ([{'character_id': 'scout', 'action': 'heal'}],
                        [{'character_id': 'scout', 'action': 'revive', 'expected_cost': 7}],
                        [{'character_id': 'unknown', 'action': 'revive'}]):
            with self.assertRaises(DomainError):
                self.command('bad-heal' + str(len(request)), 'dark.heal',
                             campaign_id='sample', run_id=run_id, heroes=request)
            self.assertEqual(self.service.get_player('hero'), before)
        revived = self.command('revive', 'dark.heal', campaign_id='sample', run_id=run_id,
                               heroes=[{'character_id': 'scout', 'action': 'revive',
                                        'expected_cost': 6}])
        self.assertEqual(revived['payment'], {'heal': 6})
        self.assertEqual(revived['healed']['scout']['health'], 100)
        self.assertEqual(self.command('revive', 'dark.heal', campaign_id='sample', run_id=run_id,
                          heroes=[{'character_id': 'scout', 'action': 'revive',
                                   'expected_cost': 6}]), revived)
        second = self.begin(run_id, key='another')
        self.complete(run_id, second, key='second-loss', win=False, hp=0,
                      enemy_health={'enemy-one': 25, 'enemy-two': 200})
        with self.service.transaction() as db:
            state = self.service._player(db, 'hero')
            state['wallet']['heal'] = 0
            from game_server.service import canonical
            db.execute('UPDATE players SET data=? WHERE id=?', (canonical(state), 'hero'))
        recovered = self.command('premium', 'dark.heal', campaign_id='sample', run_id=run_id,
            heroes=[{'character_id': 'scout', 'action': 'revive', 'expected_cost': 12}])
        self.assertEqual(recovered['payment'], {'cores': 24})
        self.assertEqual(recovered['run']['run']['heal_counts']['scout']['revive'], 2)

    def test_victory_must_consume_enemy_health_and_surviving_heroes(self):
        run_id = self.start()['run']['run_id']
        battle = self.begin(run_id)
        before = self.service.get_player('hero')
        for enemy_hp, hero_hp in (
            ({'enemy-one': 1, 'enemy-two': 0}, 50),
            ({'enemy-one': 0, 'enemy-two': 0}, 0),
            ({'enemy-one': -1, 'enemy-two': 0}, 50),
            ({'enemy-one': 0, 'other': 0}, 50),
        ):
            with self.assertRaises(DomainError):
                self.complete(run_id, battle, key='bad-' + str(enemy_hp),
                              win=True, hp=hero_hp, enemy_health=enemy_hp)
            self.assertEqual(self.service.get_player('hero'), before)
        won = self.complete(run_id, battle, key='real', win=True)
        self.assertEqual(won['battle']['stars'], 3)
        self.assertEqual(won['run']['run']['completed']['first'], battle['battle_id'])

    def test_parallel_exact_start_and_account_isolation_and_bad_catalog(self):
        self.service.create_player({'player_id': 'other', 'display_name': 'Other'})
        with ThreadPoolExecutor(max_workers=4) as pool:
            starts = list(pool.map(lambda _: self.start('parallel'), range(4)))
        self.assertEqual(starts, [starts[0]] * 4)
        self.assertEqual(self.service.get_dark_dimension('other')['runs'], {})
        with self.assertRaises(DomainError):
            self.service.get_battle('other', 'nonexistent')
        for mutate in (
            lambda p: p['campaigns']['sample']['rooms']['entry'].update(next_rooms=['unknown']),
            lambda p: p['campaigns']['sample']['nodes']['first'].update(enemies={}),
            lambda p: p['healing'].update(premium_conversion=float('nan')),
            lambda p: p['campaigns']['sample']['nodes']['first'].update(reward_tables=['not-a-table']),
            lambda p: p['health_caps'].pop('scout'),
        ):
            catalog = dark_catalog()
            mutate(catalog['dark_dimension'])
            with self.assertRaises(DomainError):
                validate_catalog(catalog)


if __name__ == '__main__':
    unittest.main()

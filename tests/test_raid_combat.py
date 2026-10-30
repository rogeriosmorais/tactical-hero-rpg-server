"""Issued raid attempts, persistent injuries/damage and atomic settlements."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import unittest

from game_server.catalog import DomainError, validate_catalog
from game_server.service import GameService
import test_raids as fixtures


def combat_catalog():
    catalog = fixtures.raid_catalog()
    rule = catalog['raids']['expedition']
    rule['combat'] = {'energy_caps': {'scout': [[0], [4], [5]], 'guardian': [[0], [3], [6]]}, 'nodes': {}}
    for key, node in rule['nodes'].items():
        rule['combat']['nodes'][key] = [{'encounter_id': node['encounter_id'] + '-' + str(i),
            'cost': {'energy': 2 + i}, 'maximum_team_size': 5, 'gates': [], 'required_character_ids': [],
            'enemies': {'enemy-one': 100 + i * 50, 'enemy-two': 200 + i * 50},
            'rewards': {'wallet': {'credits': 10 + i}, 'inventory': {'badge': 2}},
            'contribution_points': 50 + i, 'time_limit_seconds': 300} for i in range(3)]
    return catalog


class RaidCombatTests(unittest.TestCase):
    command = fixtures.RaidTests.command
    mutate_fixture = fixtures.RaidTests.mutate_fixture

    def setUp(self):
        fixtures.RaidTests.setUp(self)
        self.configure(combat_catalog())

    def configure(self, catalog):
        self.catalog = catalog
        self.path = self.path.with_name('combat.sqlite')
        if self.path.exists():
            self.path = self.path.with_name('configured-combat.sqlite')
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        raid = self.command('raid.launch', 'launch', raid_id='expedition')['raid']
        self.now += 10
        self.command('raid.join', 'join', raid_id='expedition', instance_id=raid['instance_id'], team_index=0)
        self.context = {'raid_id': 'expedition', 'instance_id': raid['instance_id'], 'team_index': 0, 'node_id': 'guard'}
        self.command('raid.lock', 'lock', **self.context)

    def begin(self, key='begin', **extra):
        return self.command('raid.begin', key, **self.context, team=['scout'], **extra)

    def finish(self, battle, key='complete', *, victory=True, health=75, enemies=None, energy=None, duration=10):
        return self.command('raid.complete', key, **self.context, battle_id=battle['battle_id'],
            outcome='victory' if victory else 'defeat', duration_seconds=duration,
            allies={'scout': {'health': health, 'maximum_health': 100, 'energy': energy or [0, 2, 3]}},
            enemy_health=enemies if enemies is not None else {'enemy-one': 0, 'enemy-two': 0})

    def test_begin_debits_once_freezes_owned_roster_boosts_and_replays_after_restart(self):
        self.mutate_fixture(lambda p: p.update(alliance_boosts={'health': {'origin': 2}}))
        before = self.service.get_player('a')
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(lambda _: self.begin(), range(3)))
        self.assertTrue(all(row == rows[0] for row in rows))
        battle = rows[0]['battle']
        self.assertEqual(rows[0]['player']['wallet']['energy'], before['wallet']['energy'] - 2)
        self.assertEqual(battle['team_state'], {'scout': before['characters']['scout']})
        self.assertEqual(battle['alliance_boosts'], before['alliance_boosts'])
        self.command('character.train', 'train', character_id='scout', levels=1)
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.begin(), rows[0])
        self.assertEqual(self.service.get_battle('a', battle['battle_id'])['team_state'], battle['team_state'])

    def test_loss_persists_injury_energy_enemy_health_and_real_damage_then_retry_pays_victory_once(self):
        battle = self.begin()['battle']
        lost = self.finish(battle, victory=False, enemies={'enemy-one': 30, 'enemy-two': 200}, health=40)
        self.assertEqual(lost['damage'], 70)
        self.assertEqual(lost['rewards']['wallet'], {})
        self.assertEqual(lost['raid']['damage']['0'], 70)
        self.assertAlmostEqual(lost['raid']['teams']['0']['rooms']['first']['nodes']['guard']['progress'], 70 / 300)
        self.command('raid.lock', 'relock', **self.context)
        resumed = self.begin('retry')['battle']
        self.assertEqual(resumed['enemy_health'], {'enemy-one': 30, 'enemy-two': 200})
        self.assertEqual(resumed['character_states']['scout'], {'health': 40, 'maximum_health': 100, 'energy': [0, 2, 3]})
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.finish(resumed, 'win'), range(3)))
        self.assertTrue(all(row == results[0] for row in results))
        result = results[0]
        self.assertEqual(result['damage'], 230)
        self.assertEqual(result['raid']['damage']['0'], 300)
        self.assertEqual(result['raid']['contribution_points'], 50)
        self.assertEqual(result['rewards']['inventory'], {'badge': 2})
        self.assertEqual(result['raid']['completed']['0']['guard']['stars'], 3)
        self.assertNotIn('guard', result['raid']['locks']['0'])

    def test_invalid_results_cost_failure_and_late_results_are_atomic(self):
        battle = self.begin()['battle']
        before = self.service.get_player('a')
        for call in (lambda: self.finish(battle, health=101), lambda: self.finish(battle, energy=[0, 99, 0]),
                     lambda: self.finish(battle, enemies={'enemy-one': 1, 'enemy-two': 0}),
                     lambda: self.finish(battle, enemies={'alien': 0}), lambda: self.finish(battle, health=0),
                     lambda: self.finish(battle, duration=301)):
            with self.assertRaises(DomainError):
                call()
            self.assertEqual(self.service.get_player('a'), before)
        self.now = before['raids'][battle['instance_id']]['expires_at']
        with self.assertRaises(DomainError):
            self.finish(battle)
        self.assertEqual(self.service.get_player('a'), before)

    def test_valid_replacement_abandons_without_refund_but_invalid_replacement_preserves_attempt(self):
        battle = self.begin()['battle']
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.begin('invalid', formation=[True])
        self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(self.service.get_battle('a', battle['battle_id'])['status'], 'active')
        self.mutate_fixture(lambda p: p['wallet'].update(energy=1))
        poor = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.begin('poor')
        self.assertEqual(self.service.get_player('a'), poor)
        self.assertEqual(self.service.get_battle('a', battle['battle_id'])['status'], 'active')
        self.mutate_fixture(lambda p: p['wallet'].update(energy=before['wallet']['energy']))
        replacement = self.begin('replace')
        self.assertEqual(replacement['player']['wallet']['energy'], before['wallet']['energy'] - 2)
        self.assertEqual(replacement['raid']['damage'], {})
        self.assertEqual(self.service.get_battle('a', battle['battle_id'])['status'], 'abandoned')
        with self.assertRaises(DomainError):
            self.finish(battle)
        self.assertEqual(self.service.get_player('a'), replacement['player'])

    def test_checkpoint_is_bound_to_begin_and_cannot_spend_or_grant_progress(self):
        initial = {'enemy_health': {'enemy-one': 100, 'enemy-two': 200}, 'progress': 0}
        battle = self.begin(checkpoint=initial)['battle']
        before = self.service.get_player('a')
        result = self.command('raid.checkpoint', 'keepalive', **self.context, battle_id=battle['battle_id'], checkpoint=initial)
        self.assertEqual(result['battle_id'], battle['battle_id'])
        after = self.service.get_player('a')
        self.assertEqual(after['wallet'], before['wallet'])
        self.assertEqual(after['raids'], before['raids'])
        with self.assertRaises(DomainError):
            self.command('raid.checkpoint', 'bad', **self.context, battle_id=battle['battle_id'],
                checkpoint={'enemy_health': {'enemy-one': 0, 'enemy-two': 200}, 'progress': 0})
        self.assertEqual(self.service.get_player('a'), after)

    def test_reset_restores_only_enemy_attempt_and_damage_preserves_injuries_and_spending(self):
        battle = self.begin()['battle']
        with self.assertRaises(DomainError):
            self.command('raid.reset', 'active', **self.context)
        lost = self.finish(battle, victory=False, enemies={'enemy-one': 0, 'enemy-two': 150}, health=20)
        with self.assertRaises(DomainError):
            self.begin('difficulty', difficulty=1)
        reset = self.command('raid.reset', 'reset', **self.context)
        self.assertEqual(reset['raid']['damage']['0'], 0)
        self.assertEqual(reset['raid']['character_states'], lost['raid']['character_states'])
        self.assertEqual(reset['player']['wallet'], lost['player']['wallet'])
        self.assertEqual(reset['reset_node_ids'], ['guard', 'leader'])
        self.command('raid.lock', 'again', **self.context)
        harder = self.begin('harder', difficulty=1)['battle']
        self.assertEqual(harder['enemy_health'], {'enemy-one': 150, 'enemy-two': 250})

    def test_death_blocks_reuse_and_other_modes_cannot_be_abandoned(self):
        battle = self.begin()['battle']
        self.finish(battle, victory=False, health=0, enemies={'enemy-one': 50, 'enemy-two': 200})
        self.command('raid.lock', 'again', **self.context)
        with self.assertRaises(DomainError):
            self.begin('dead')
        self.command('mission.begin', 'ordinary', mission_id='training.1', team=['guardian'])
        with self.assertRaises(DomainError):
            self.command('raid.begin', 'other-mode', **self.context, team=['guardian'])
        self.assertEqual(self.service.get_battle('a', battle['battle_id'])['status'], 'complete')

    def test_gates_use_the_joined_snapshot_and_preserve_alternatives_and_mandatory_members(self):
        catalog = combat_catalog()
        encounter = catalog['raids']['expedition']['combat']['nodes']['guard'][0]
        encounter['gates'] = [{'minimum_team_size': 1, 'characters': {
            'scout': [{'level': 2}, {'promotion_rank': 2}], 'guardian': [{}]}}]
        encounter['required_character_ids'] = ['guardian']
        self.configure(catalog)
        self.command('character.train', 'train', character_id='scout', levels=1)
        before = self.service.get_player('a')
        for team in (['scout'], ['scout', 'guardian']):
            with self.assertRaises(DomainError):
                self.command('raid.begin', 'invalid-team', **self.context, team=team)
            self.assertEqual(self.service.get_player('a'), before)
        valid = self.command('raid.begin', 'valid', **self.context, team=['guardian'])
        self.assertEqual(valid['battle']['team'], ['guardian'])

    def test_scalar_energy_limits_initial_enemy_health_and_absent_timer_preserve_source_semantics(self):
        catalog = combat_catalog()
        combat = catalog['raids']['expedition']['combat']
        combat['energy_caps']['scout'] = [0, 4, 5]
        combat['nodes']['guard'][0].update(time_limit_seconds=None, initial_enemy_health={'enemy-one': 80, 'enemy-two': 150})
        self.configure(catalog)
        self.mutate_fixture(lambda p: p['raids'][self.context['instance_id']]['characters']['scout'].update(abilities=[5, 5, 5]))
        battle = self.begin()['battle']
        self.assertEqual(battle['energy_caps']['scout'], [0, 4, 5])
        self.assertEqual(battle['enemy_health'], {'enemy-one': 80, 'enemy-two': 150})
        result = self.finish(battle, duration=100000)
        self.assertEqual(result['damage'], 230)

    def test_malformed_combat_rules_are_rejected_before_database_creation(self):
        for mutate in (lambda r: r['nodes'].pop('guard'), lambda r: r['energy_caps']['scout'].append([1]),
                       lambda r: r['nodes']['guard'][0]['enemies'].update({'enemy-one': 0}),
                       lambda r: r['nodes']['guard'].pop()):
            catalog = deepcopy(self.catalog)
            mutate(catalog['raids']['expedition']['combat'])
            with self.assertRaises(DomainError):
                validate_catalog(catalog)

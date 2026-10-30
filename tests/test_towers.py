"""Independent authored tower fixtures; no original game identifiers/content."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


def tower_catalog():
    catalog = deepcopy(DEMO_CATALOG)
    floors = [
        {'encounter_id': 'trial.alpha', 'unlock_offset': 0, 'reset_cooldowns': False,
         'score': 10, 'cost': {}, 'rewards': {'wallet': {'credits': 3}}},
        {'encounter_id': 'trial.beta', 'unlock_offset': 0, 'reset_cooldowns': True,
         'score': 20, 'cost': {}, 'rewards': {'inventory': {'badge': 1}}},
        {'encounter_id': 'trial.gamma', 'unlock_offset': 100, 'reset_cooldowns': False,
         'score': 30, 'cost': {}, 'rewards': {}},
    ]
    restricted = deepcopy(floors)
    restricted[0]['eligible_characters'] = ['guardian']
    catalog['towers'] = {'trial': {'starts_at': 1000, 'ends_at': 1400, 'cooldown_seconds': 100,
        'team_size': 1, 'difficulties': {'intro': floors, 'advanced': restricted},
        'milestones': [{'milestone_id': 'first', 'points': 10, 'rewards': {'inventory': {'badge': 2}}},
                       {'milestone_id': 'finish', 'points': 60, 'rewards': {'wallet': {'credits': 7}}}]}}
    return catalog


class TowerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'tower.sqlite'
        self.now = 1001
        self.catalog = tower_catalog()
        self.service = GameService(self.database, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.sequence = 0

    def tearDown(self):
        self.temp.cleanup()

    def command(self, command, key=None, player='a', **parameters):
        self.sequence += 1
        return self.service.execute(player, {'command': command, 'idempotency_key': key or f'cmd-{self.sequence}',
                                            'parameters': parameters})

    def start(self, difficulty='intro', **kwargs):
        return self.command('tower.start', tower_id='trial', difficulty_id=difficulty, **kwargs)['tower']['active_run']

    def begin(self, run, index=0, team=None, **kwargs):
        return self.command('tower.begin', tower_id='trial', run_id=run['run_id'], floor_index=index,
                            encounter_id=self.catalog['towers']['trial']['difficulties'][run['difficulty_id']][index]['encounter_id'],
                            team=team or ['scout'], **kwargs)['battle']

    def finish(self, battle, outcome='victory', **kwargs):
        return self.command('tower.complete', battle_id=battle['battle_id'], outcome=outcome,
                            stars=3 if outcome == 'victory' else 0, **kwargs)

    def test_all_difficulties_accessible_at_level_one_and_roster_rules_enforced(self):
        self.assertEqual(self.service.get_player('a')['level'], 1)
        run = self.start('advanced')
        with self.assertRaisesRegex(DomainError, 'eligibility'):
            self.begin(run)
        self.assertEqual(self.begin(run, team=['guardian'])['encounter_id'], 'trial.alpha')

    def test_server_controls_floor_order_team_cooldowns_and_active_battle(self):
        run = self.start()
        for params in ({'index': 1}, {'team': ['missing']}, {'team': ['scout', 'guardian']}):
            with self.subTest(params=params), self.assertRaises(DomainError):
                self.begin(run, **params)
        battle = self.begin(run)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.begin(run)
        with self.assertRaises(DomainError):
            self.command('tower.end', tower_id='trial', run_id=run['run_id'])
        self.assertEqual(self.service.get_player('a'), before)
        self.finish(battle)
        with self.assertRaisesRegex(DomainError, 'cooldown'):
            self.begin(run, 1)
        self.assertEqual(self.begin(run, 1, ['guardian'])['floor_index'], 1)

    def test_exact_begin_complete_and_claim_replays_survive_restart(self):
        run = self.start(key='start')
        battle = self.begin(run, key='begin')
        self.assertEqual(self.begin(run, key='begin'), battle)
        win = self.finish(battle, key='win')
        claim = self.command('tower.claim', 'claim', tower_id='trial', milestone_id='first')
        self.assertEqual(claim['player']['inventory']['badge'], 2)
        self.service = GameService(self.database, self.catalog, clock=lambda: self.now)
        self.assertEqual(self.finish(battle, key='win'), win)
        self.assertEqual(self.command('tower.claim', 'claim', tower_id='trial', milestone_id='first'), claim)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.finish(battle)
        with self.assertRaises(DomainError):
            self.command('tower.claim', tower_id='trial', milestone_id='first')
        self.assertEqual(self.service.get_player('a'), before)

    def test_defeat_and_retreat_preserve_floor_and_do_not_award_or_cool_down(self):
        run = self.start()
        for outcome in ('defeat', 'retreat'):
            result = self.finish(self.begin(run), outcome)
            self.assertEqual(result['tower']['active_run']['next_floor'], 0)
            self.assertEqual(result['tower']['high_score'], 0)
            self.assertEqual(result['tower']['cooldowns'], {})
            self.assertEqual(result['player']['wallet']['credits'], 100)
        self.assertEqual(self.begin(run)['floor_index'], 0)

    def test_checkpoint_and_weekly_time_gates(self):
        run = self.start()
        self.finish(self.begin(run))
        checkpoint = self.finish(self.begin(run, 1, ['guardian']))
        self.assertEqual(checkpoint['tower']['cooldowns'], {})
        with self.assertRaises(DomainError):
            self.begin(run, 2)
        self.now = 1100
        complete = self.finish(self.begin(run, 2))
        self.assertEqual(complete['tower']['active_run']['status'], 'complete')
        self.assertEqual(complete['tower']['high_score'], 60)
        self.assertEqual(complete['player']['inventory']['badge'], 1)
        prize = self.command('tower.claim', tower_id='trial', milestone_id='finish')
        self.assertEqual(prize['player']['wallet']['credits'], 110)

    def test_cooldowns_and_prize_history_survive_end_new_run_and_difficulty_change(self):
        run = self.start()
        self.finish(self.begin(run))
        self.command('tower.end', tower_id='trial', run_id=run['run_id'])
        next_run = self.start()
        with self.assertRaisesRegex(DomainError, 'cooldown'):
            self.begin(next_run)
        self.now = 1100
        replay_floor = self.finish(self.begin(next_run))
        self.assertEqual(replay_floor['rewards']['wallet'], {})
        self.assertEqual(replay_floor['player']['wallet']['credits'], 103)
        self.assertEqual(replay_floor['tower']['high_score'], 10)

    def test_win_across_week_boundary_cools_down_in_settlement_period(self):
        run = self.start()
        battle = self.begin(run)
        self.now = 1101
        result = self.finish(battle)
        self.assertEqual(result['tower']['cooldowns']['scout'], 1200)
        with self.assertRaises(DomainError):
            self.begin(run, 1)

    def test_ownership_mode_and_payload_forgery_rejected_atomically(self):
        run = self.start()
        battle = self.begin(run)
        before = self.service.get_player('a')
        self.service.create_player({'player_id': 'b', 'display_name': 'B'})
        for command, fields in [
            ('tower.complete', {'battle_id': battle['battle_id'], 'outcome': 'victory', 'stars': 3, 'rewards': {}}),
            ('mission.complete', {'battle_id': battle['battle_id'], 'mission_id': 'training.1', 'outcome': 'victory', 'stars': 3}),
            ('tower.claim', {'tower_id': 'trial', 'milestone_id': 'first'}),
            ('tower.start', {'tower_id': 'trial', 'difficulty_id': 'advanced'}),
        ]:
            with self.subTest(command=command), self.assertRaises(DomainError):
                self.command(command, **fields)
        with self.assertRaises(DomainError):
            self.finish(battle, player='b')
        self.assertEqual(self.service.get_player('a'), before)
        mission = self.command('mission.begin', mission_id='training.1', team=['scout'])['battle']
        with self.assertRaises(DomainError):
            self.finish(mission)

    def test_concurrent_duplicate_victories_and_claims_pay_once(self):
        battle = self.begin(self.start())
        request = {'command': 'tower.complete', 'idempotency_key': 'concurrent-win',
                   'parameters': {'battle_id': battle['battle_id'], 'outcome': 'victory', 'stars': 3}}
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.service.execute('a', request), range(6)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(self.service.get_player('a')['wallet']['credits'], 103)
        request = {'command': 'tower.claim', 'idempotency_key': 'concurrent-claim',
                   'parameters': {'tower_id': 'trial', 'milestone_id': 'first'}}
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.service.execute('a', request), range(6)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(self.service.get_player('a')['inventory']['badge'], 2)

    def test_event_boundaries_and_catalog_integrity(self):
        for now in (999, 1400):
            self.now = now
            with self.assertRaises(DomainError):
                self.start()
        for change in ('level', 'negative', 'bool', 'offset', 'undefined', 'duplicate'):
            value = tower_catalog()
            tower = value['towers']['trial']
            floor = tower['difficulties']['intro'][0]
            if change == 'level': tower['required_level'] = 65
            if change == 'negative': floor['rewards']['wallet']['credits'] = -1
            if change == 'bool': floor['score'] = True
            if change == 'offset': floor['unlock_offset'] = 500
            if change == 'undefined': floor['eligible_characters'] = ['missing']
            if change == 'duplicate': tower['milestones'][1]['milestone_id'] = 'first'
            with self.subTest(change=change), self.assertRaises(DomainError):
                validate_catalog(value)

    def test_read_projection_and_stale_renderer_cannot_mutate_state(self):
        before = self.service.get_player('a')
        self.assertTrue(self.service.get_tower('a', 'trial')['available'])
        self.assertEqual(self.service.get_player('a'), before)
        run = self.start()
        before = self.service.get_player('a')
        with self.assertRaisesRegex(DomainError, 'Encounter does not match'):
            self.command('tower.begin', tower_id='trial', run_id=run['run_id'], floor_index=0,
                         encounter_id='stale-encounter', team=['scout'])
        self.assertEqual(self.service.get_player('a'), before)
        self.finish(self.begin(run))
        self.now = 1100
        before = self.service.get_player('a')
        self.assertEqual(self.service.get_tower('a', 'trial')['tower']['cooldowns'], {})
        self.assertEqual(self.service.get_player('a'), before)

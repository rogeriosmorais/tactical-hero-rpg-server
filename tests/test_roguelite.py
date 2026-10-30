"""Authored run/economy examples, independent of any native content or sibling."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


def authored_catalog():
    catalog = deepcopy(DEMO_CATALOG)
    starters = ['scout', 'guardian', 'healer', 'blaster', 'striker', 'warden']
    recipients = ['ranger', 'sentinel', 'adept', 'vanguard']
    catalog.update(starting_wallet={}, starting_characters={hero: 1 for hero in starters},
        characters={hero: {'maximum_level': 3, 'training_cost': {'credits': 2}, 'power_per_level': 12}
            for hero in starters + recipients}, missions={}, offers={}, experience_per_level=10,
        maximum_player_level=3, experience_thresholds=[0, 10, 20])
    def cost(credits, hero=None, quantity=0):
        return {'wallet': {'credits': credits}, 'inventory': {f'shards.{hero}': quantity} if hero else {}}
    catalog['development'] = {'characters': {},
        'training_items': {'training': {'experience': 5, 'cost': {'credits': 2}}}, 'recipes': {}}
    for hero in catalog['characters']:
        catalog['development']['characters'][hero] = {'experience_thresholds': [0, 5, 15],
            'recruitment': cost(5, hero, 5), 'initial_rank': 1, 'rank_upgrades': [cost(2, hero, 3)],
            'initial_abilities': [1], 'abilities': [[
                {'required_level': 1, 'required_tier': 1, 'payment': cost(0)},
                {'required_level': 2, 'required_tier': 1,
                 'payment': {'wallet': {'credits': 2}, 'inventory': {'ability-token': 1}}}]], 'tiers': [{}]}
    bundle = {'wallet': {'credits': 10}, 'inventory': {'training': 1, 'ability-token': 1}, 'experience': 10}
    catalog['roguelite'] = {'minimum_team_size': 1, 'maximum_team_size': 5,
        'enemy_teams': {'patrol': starters[:5], 'guard': starters[1:]},
        'enemy_base_level': 1, 'enemy_level_step': 1, 'victory_rewards': deepcopy(bundle),
        'defeat_rewards': {}, 'choice_count': 3,
        'shard_pool': [{'character_id': hero, 'item_id': f'shards.{hero}', 'quantity': 5}
                      for hero in recipients]}
    return catalog


class RogueliteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'roguelite.sqlite'
        self.catalog = authored_catalog()
        self.service = GameService(self.database, self.catalog)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.sequence = 0

    def command(self, name, key=None, **parameters):
        self.sequence += 1
        return self.service.execute('a', {'command': name, 'idempotency_key': key or f'op-{self.sequence}',
            'parameters': parameters})

    def active_run(self):
        return self.service.get_roguelite('a')['roguelite']['active_run']

    def start(self):
        return self.command('roguelite.start')['roguelite']['active_run']

    def begin(self, team, **extra):
        run = self.active_run()
        return self.command('roguelite.begin', run_id=run['run_id'],
            encounter_id=run['encounter']['encounter_id'], team=team, **extra)['battle']

    def finish(self, battle, outcome='victory', **extra):
        return self.command('roguelite.complete', battle_id=battle['battle_id'], outcome=outcome,
            stars=3 if outcome == 'victory' else 0, **extra)

    def choose(self, **extra):
        run = self.active_run()
        choice = run['pending_choice']
        return self.command('roguelite.choose', run_id=run['run_id'], choice_id=choice['choice_id'],
            option_id=choice['options'][0]['option_id'], **extra)

    def test_short_teams_use_exact_unordered_combinations_and_have_five_enemies(self):
        self.assertEqual(self.service.get_player('a')['level'], 1)
        self.start()
        for team in (['scout'], ['scout', 'guardian', 'healer'],
                     ['scout', 'guardian', 'healer', 'blaster', 'striker']):
            battle = self.begin(team)
            self.assertEqual(battle['team'], team)
            self.assertEqual(len(battle['enemies']), 5)
            self.assertEqual(len(battle['formation']), len(team))
            self.finish(battle)
            self.choose()
            before = self.service.get_player('a')
            with self.assertRaisesRegex(DomainError, 'combination'):
                self.begin(list(reversed(team)))
            self.assertEqual(before, self.service.get_player('a'))
        self.assertEqual(self.active_run()['floor'], 4)
        self.assertEqual(self.active_run()['score'], 3)

    def test_invalid_begin_does_not_consume_squads_or_change_player(self):
        self.start()
        for team in ([], ['scout'] * 2, ['missing'], list(self.catalog['starting_characters']), [True]):
            before = self.service.get_player('a')
            with self.subTest(team=team), self.assertRaises(DomainError):
                self.begin(team)
            self.assertEqual(before, self.service.get_player('a'))
        with self.assertRaises(DomainError):
            self.begin(['scout'], formation=[False])
        self.assertEqual(self.active_run()['used_squads'], [])
        self.assertEqual(self.begin(['scout'], formation=[9])['formation'], [9])

    def test_authored_three_hero_opener_uses_equal_initial_states_without_auto_victory(self):
        catalog = deepcopy(self.catalog)
        starters = ['scout', 'striker', 'healer']
        catalog['starting_characters'] = {hero: 1 for hero in starters}
        catalog['roguelite']['opening_enemy_team'] = starters
        self.service = GameService(Path(self.temp.name) / 'opening.sqlite', catalog)
        player = self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.assertEqual(set(player['characters']), set(starters))
        run = self.start()
        self.assertEqual(run['encounter']['template_id'], 'opening')
        for enemy in run['encounter']['enemies']:
            self.assertEqual(enemy['state'], player['characters'][enemy['character_id']])
        battle = self.begin(starters)
        self.assertEqual(battle['status'], 'active')
        self.assertEqual((self.service.get_player('a')['experience'], self.active_run()['score']), (0, 0))
        self.finish(battle, 'defeat')
        self.assertEqual(self.service.get_player('a')['experience'], 0)
        self.assertIsNone(self.active_run()['pending_choice'])
        self.command('roguelite.end', run_id=run['run_id'])
        self.start()
        self.finish(self.begin(starters))
        self.assertEqual(len(self.active_run()['encounter']['enemies']), 5)

    def test_first_victory_funds_real_training_ability_and_specific_recruitment(self):
        self.start()
        old_encounter = self.active_run()['encounter']
        self.finish(self.begin(['scout']))
        claimed = self.choose()
        recipient = claimed['choice']['option']['character_id']
        another = next(row['character_id'] for row in self.catalog['roguelite']['shard_pool']
                       if row['character_id'] != recipient)
        with self.assertRaises(DomainError):
            self.command('character.recruit', character_id=another)
        self.command('character.train_items', character_id='scout', expected_level=1, items={'training': 1})
        self.command('character.ability_upgrade', character_id='scout', ability_index=0, unlock=False)
        player = self.command('character.recruit', character_id=recipient)['player']
        self.assertEqual(player['level'], 2)
        self.assertEqual(player['characters']['scout']['level'], 2)
        self.assertEqual(player['characters']['scout']['abilities'], [2])
        self.assertIn(recipient, player['characters'])
        self.assertEqual(player['wallet']['credits'], 1)
        self.assertEqual(player['inventory'][f'shards.{recipient}'], 0)
        self.assertEqual(player['inventory']['training'], 0)
        self.assertEqual(player['inventory']['ability-token'], 0)
        self.assertNotEqual(self.begin(['guardian'])['encounter_id'], old_encounter['encounter_id'])

    def test_zero_win_run_earns_nothing_and_loss_keeps_earlier_victory_earnings(self):
        # Obsolete catalog bundles must not re-enable progression without a win.
        legacy = deepcopy(self.catalog)
        legacy['roguelite']['defeat_rewards'] = deepcopy(legacy['roguelite']['victory_rewards'])
        self.service = GameService(Path(self.temp.name) / 'legacy-rewards.sqlite', legacy)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.start()
        encounter = deepcopy(self.active_run()['encounter'])
        lost = self.finish(self.begin(['scout']), 'defeat', key='loss')
        self.assertEqual(lost['rewards'], {'wallet': {}, 'inventory': {}, 'experience': 0})
        self.assertIsNone(self.active_run()['pending_choice'])
        self.assertEqual(self.active_run()['encounter'], encounter)
        self.assertEqual((lost['player']['experience'], lost['player']['wallet'], lost['player']['inventory']), (0, {}, {}))
        self.command('roguelite.end', run_id=self.active_run()['run_id'])
        self.start()
        self.finish(self.begin(['scout']))
        self.choose()
        earned = self.service.get_player('a')
        floor = deepcopy(self.active_run()['encounter'])
        defeat = self.finish(self.begin(['guardian']), 'defeat', key='later-loss')
        self.assertIsNone(self.active_run()['pending_choice'])
        self.assertEqual(self.active_run()['encounter'], floor)
        self.assertEqual((self.active_run()['floor'], self.active_run()['score']), (2, 1))
        for field in ('experience', 'wallet', 'inventory', 'characters'):
            self.assertEqual(defeat['player'][field], earned[field])
        self.assertEqual(self.finish(defeat['battle'], 'defeat', key='later-loss'), defeat)
        self.command('roguelite.end', run_id=self.active_run()['run_id'])
        final = self.service.get_player('a')
        for field in ('experience', 'wallet', 'inventory', 'characters'):
            self.assertEqual(final[field], earned[field])

    def test_fifty_wins_accumulate_fifty_floor_rewards(self):
        from itertools import combinations, islice
        self.start()
        teams = (list(team) for size in range(1, 6)
                 for team in combinations(self.catalog['starting_characters'], size))
        for team in islice(teams, 50):
            self.finish(self.begin(team))
            self.choose()
        player = self.service.get_player('a')
        self.assertEqual((self.active_run()['floor'], self.active_run()['score']), (51, 50))
        self.assertEqual(player['experience'], 500)
        self.assertEqual(player['wallet']['credits'], 500)
        self.assertEqual(player['inventory']['training'], 50)
        self.assertEqual(player['inventory']['ability-token'], 50)
        self.assertEqual(sum(value for key, value in player['inventory'].items() if key.startswith('shards.')), 250)

    def test_retirement_auto_resolves_once_and_restart_has_no_timer(self):
        first = self.start()
        self.finish(self.begin(['scout']))
        option = self.active_run()['pending_choice']['options'][0]
        ended = self.command('roguelite.end', key='end', run_id=first['run_id'])
        self.assertTrue(ended['choice']['automatic'])
        self.assertEqual(ended['player']['inventory'][option['item_id']], option['quantity'])
        second = self.start()
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertEqual(second['used_squads'], [])
        self.begin(['scout'])
        before = self.service.get_player('a')
        self.assertEqual(self.command('roguelite.end', key='end', run_id=first['run_id']), ended)
        self.assertEqual(before, self.service.get_player('a'))

    def test_abandon_and_late_result_cannot_pay_or_reroll(self):
        self.start()
        old = self.active_run()['encounter']
        battle = self.begin(['scout'])
        result = self.command('roguelite.abandon', battle_id=battle['battle_id'])
        self.assertEqual(result['battle']['status'], 'abandoned')
        self.assertEqual(self.active_run()['encounter'], old)
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.finish(battle)
        self.assertEqual(self.service.get_player('a'), before)
        battle = self.begin(['guardian'])
        run_id = self.active_run()['run_id']
        self.command('roguelite.end', run_id=run_id)
        self.start()
        with self.assertRaises(DomainError):
            self.finish(battle)
        self.assertEqual(self.service.get_player('a')['experience'], 0)

    def test_reads_receipts_and_frozen_choices_survive_restart(self):
        self.start()
        battle = self.begin(['scout'], key='begin')
        self.service = GameService(self.database, self.catalog)
        self.assertEqual(self.service.get_battle('a', battle['battle_id']), battle)
        self.assertEqual(self.active_run()['battle_id'], battle['battle_id'])
        settled = self.finish(battle, key='complete')
        choice = deepcopy(self.active_run()['pending_choice'])
        before = self.service.get_player('a')
        self.service = GameService(self.database, self.catalog)
        self.assertEqual(self.active_run()['pending_choice'], choice)
        self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(self.finish(battle, key='complete'), settled)
        with self.assertRaises(DomainError):
            self.finish(battle)
        chosen = self.choose(key='choose')
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('roguelite.choose', run_id=self.active_run()['run_id'], choice_id=choice['choice_id'],
                option_id=choice['options'][0]['option_id'])
        self.assertEqual(self.service.get_player('a'), before)
        self.service = GameService(self.database, self.catalog)
        replay = self.command('roguelite.choose', key='choose', run_id=self.active_run()['run_id'],
            choice_id=choice['choice_id'], option_id=choice['options'][0]['option_id'])
        self.assertEqual(replay, chosen)

    def test_concurrent_begin_and_claim_each_commit_once(self):
        run = self.start()
        def call(key, command, parameters):
            try:
                return self.service.execute('a', {'command': command, 'idempotency_key': key, 'parameters': parameters})
            except DomainError as error:
                return error.code
        params = {'run_id': run['run_id'], 'encounter_id': run['encounter']['encounter_id'], 'team': ['scout']}
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda key: call(key, 'roguelite.begin', params), ('begin-a', 'begin-b')))
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual(len(self.active_run()['used_squads']), 1)
        self.finish(self.service.get_battle('a', self.active_run()['battle_id']))
        choice = self.active_run()['pending_choice']
        option = choice['options'][0]
        params = {'run_id': run['run_id'], 'choice_id': choice['choice_id'], 'option_id': option['option_id']}
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda key: call(key, 'roguelite.choose', params), ('claim-a', 'claim-b')))
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual(self.service.get_player('a')['inventory'][option['item_id']], option['quantity'])

    def test_choice_receipt_fingerprint_recovers_without_mutable_run_context(self):
        self.start()
        self.finish(self.begin(['scout']))
        issued = self.choose(key='fingerprinted', request_fingerprint='a' * 64)
        before = self.service.get_player('a')
        self.command('roguelite.end', run_id=self.active_run()['run_id'])
        self.start()
        request = {'idempotency_key': 'fingerprinted', 'command': 'roguelite.choose',
                   'request_fingerprint': 'a' * 64}
        self.assertEqual(self.service.lookup_receipt('a', request), {'status': 'match', 'result': issued})
        request['request_fingerprint'] = 'b' * 64
        self.assertEqual(self.service.lookup_receipt('a', request), {'status': 'conflict'})
        self.assertEqual(self.service.get_player('a')['inventory'], before['inventory'])

    def test_fixed_seeds_vary_materialized_teams_and_offer_sets(self):
        from game_server.roguelite import encounter, rng
        rules = self.catalog['roguelite']
        templates, options = set(), set()
        for seed in ('one', 'two', 'three', 'four', 'five', 'six'):
            run = {'run_id': 'fixed', 'seed': seed, 'floor': 1}
            first = encounter(self.service, rules, run)
            self.assertEqual(first, encounter(self.service, rules, run))
            templates.add(first['template_id'])
            offered = rng(seed, 'choice', 'fixed-battle').sample(rules['shard_pool'], 3)
            options.add(tuple(sorted(row['character_id'] for row in offered)))
        self.assertEqual(templates, {'patrol', 'guard'})
        self.assertGreater(len(options), 1)

    def test_full_388_character_shard_pool_remains_selectable_without_buckets(self):
        catalog = authored_catalog()
        from game_server.roguelite import weighted_shard_options
        template = deepcopy(catalog['characters']['ranger'])
        development = deepcopy(catalog['development']['characters']['ranger'])
        for index in range(10, 388):
            hero = f'hero-{index:03d}'
            item = f'shards.{hero}'
            catalog['characters'][hero] = deepcopy(template)
            entry = deepcopy(development)
            entry['recruitment']['inventory'] = {item: 5}
            catalog['development']['characters'][hero] = entry
        catalog['roguelite']['shard_pool'] = [
            {'character_id': hero, 'item_id': f'shards.{hero}', 'quantity': 5}
            for hero in catalog['characters'] if hero not in catalog['starting_characters']]
        self.assertEqual(len(catalog['roguelite']['shard_pool']), 382)
        # Include the six already-owned heroes too: their own shard currency
        # can upgrade them, so the full 388-character universe is selectable.
        catalog['roguelite']['shard_pool'].extend(
            {'character_id': hero, 'item_id': f'shards.{hero}', 'quantity': 5}
            for hero in catalog['starting_characters'])
        self.assertEqual(len(catalog['roguelite']['shard_pool']), 388)
        validate_catalog(catalog)
        service = GameService(Path(self.temp.name) / 'large.sqlite', catalog)
        player = service.create_player({'player_id': 'large', 'display_name': 'Large'})
        self.assertEqual(len(player['characters']), 6)
        for sample in range(60):
            issued = weighted_shard_options(player, catalog['roguelite'], catalog,
                                            'large-' + str(sample), 'battle')
            self.assertEqual(len(issued), 3)
            self.assertEqual(len({row['character_id'] for row in issued}), 3)
            self.assertTrue(all(row in catalog['roguelite']['shard_pool'] for row in issued))
        catalog['roguelite']['shard_pool'] *= 2
        with self.assertRaises(DomainError):
            validate_catalog(catalog)

    def test_shard_age_and_top_roster_synergy_change_chances_without_forcing_slots(self):
        from game_server.roguelite import weighted_shard_options
        catalog = deepcopy(self.catalog)
        policy = catalog['roguelite']
        policy['choice_count'] = 1
        policy['shard_pool'] = [
            {'character_id': 'ranger', 'item_id': 'shards.ranger', 'quantity': 5, 'era': 0},
            {'character_id': 'sentinel', 'item_id': 'shards.sentinel', 'quantity': 5, 'era': 100},
            {'character_id': 'adept', 'item_id': 'shards.adept', 'quantity': 5}]
        validate_catalog(catalog)
        original = self.service.get_player('a')
        low = deepcopy(original)
        high = deepcopy(original)
        high['level'] = catalog['maximum_player_level']
        def frequency(player):
            counts = {row['character_id']: 0 for row in policy['shard_pool']}
            for seed in range(600):
                choice = weighted_shard_options(player, policy, catalog,
                                                f'age-{seed}', 'battle')[0]
                counts[choice['character_id']] += 1
            return counts
        low_counts = frequency(low)
        high_counts = frequency(high)
        self.assertGreater(low_counts['ranger'], low_counts['sentinel'] * 2)
        self.assertGreater(high_counts['sentinel'], high_counts['ranger'] * 2)
        self.assertGreater(min(low_counts.values()), 0)
        self.assertGreater(min(high_counts.values()), 0)

        policy['shard_pool'] = [
            {'character_id': 'ranger', 'item_id': 'shards.ranger', 'quantity': 5,
             'affinities': ['scout']},
            {'character_id': 'sentinel', 'item_id': 'shards.sentinel', 'quantity': 5}]
        top = deepcopy(original)
        top['characters']['scout']['power'] = 1200
        counts = frequency(top)
        self.assertGreater(counts['ranger'], counts['sentinel'] * 3)
        self.assertGreater(counts['sentinel'], 0)
        policy['shard_pool'][0]['affinities'] = ['nonexistent']
        with self.assertRaises(DomainError):
            validate_catalog(catalog)

    def test_real_upgrades_change_synergy_odds_even_when_level_power_does_not(self):
        from game_server.roguelite import upgrade_strength, weighted_shard_options
        catalog = deepcopy(self.catalog)
        policy = catalog['roguelite']
        policy['choice_count'] = 1
        policy['shard_pool'] = [
            {'character_id': 'ranger', 'item_id': 'shards.ranger', 'quantity': 5,
             'affinities': ['scout']},
            {'character_id': 'sentinel', 'item_id': 'shards.sentinel', 'quantity': 5,
             'affinities': ['guardian']}]
        validate_catalog(catalog)
        player = self.service.get_player('a')
        baseline = {row: 0 for row in ('ranger', 'sentinel')}
        upgraded = {row: 0 for row in baseline}

        def tally(snapshot, counts):
            for index in range(1000):
                offer = weighted_shard_options(snapshot, policy, catalog,
                                               f'upgrade-seed-{index}', 'battle')[0]
                counts[offer['character_id']] += 1

        tally(player, baseline)
        self.assertLess(abs(baseline['ranger'] - baseline['sentinel']), 80)
        version = deepcopy(player)
        version['characters']['scout'].update(rank=7, tier=5, abilities=[3, 2],
                                              equipment={'head': 'earned-gear'})
        self.assertEqual(version['characters']['scout']['power'],
                         player['characters']['scout']['power'])
        self.assertGreater(upgrade_strength(version['characters']['scout']),
                           3 * upgrade_strength(version['characters']['guardian']))
        tally(version, upgraded)
        self.assertGreater(upgraded['ranger'], upgraded['sentinel'] * 2)
        self.assertGreater(upgraded['sentinel'], 0)
        self.assertEqual(weighted_shard_options(version, policy, catalog,
                         'fixed', 'battle'), weighted_shard_options(version, policy, catalog,
                                                                    'fixed', 'battle'))

    def test_snapshots_and_mode_guard_do_not_depend_on_later_roster(self):
        self.start()
        battle = self.begin(['scout'])
        self.assertEqual(battle['team_state']['scout']['level'], 1)
        before = self.service.get_player('a')
        with self.assertRaisesRegex(DomainError, 'roguelite.complete'):
            self.command('mission.complete', mission_id='invented', battle_id=battle['battle_id'],
                         outcome='victory', stars=3)
        self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(self.service.get_battle('a', battle['battle_id']), battle)

    def test_old_catalog_is_unchanged_and_mode_is_opt_in(self):
        service = GameService(Path(self.temp.name) / 'old.sqlite', DEMO_CATALOG)
        service.create_player({'player_id': 'a', 'display_name': 'A'})
        with self.assertRaises(DomainError):
            service.get_roguelite('a')
        self.assertNotIn('roguelite', service.get_player('a'))

    def test_catalog_rejects_bad_enemy_counts_shard_aliases_and_team_policy(self):
        for mutate in (lambda rules: rules['enemy_teams'].update(patrol=['scout']),
                       lambda rules: rules.update(minimum_team_size=5),
                       lambda rules: rules.update(maximum_team_size=True),
                       lambda rules: rules.update(opening_enemy_team=[]),
                       lambda rules: rules.update(opening_enemy_team=['missing']),
                       lambda rules: rules.update(opening_enemy_team=['scout'] * 6),
                       lambda rules: rules['shard_pool'][0].update(item_id='shards.scout')):
            catalog = deepcopy(self.catalog)
            mutate(catalog['roguelite'])
            with self.assertRaises(DomainError):
                validate_catalog(catalog)


if __name__ == '__main__':
    unittest.main()

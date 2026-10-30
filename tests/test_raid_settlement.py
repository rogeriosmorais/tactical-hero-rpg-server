"""Deferred raid rewards, closure boundaries, first-completion and atomic mail."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import unittest
from unittest.mock import patch

from game_server.catalog import DomainError, validate_catalog
from game_server.service import GameService
import test_raid_combat as combat
import test_raids as fixtures


def settlement_catalog():
    catalog = combat.combat_catalog()
    table = lambda bundle: {'groups': [[{'weight': 1, 'multiplier': 1, 'items': [
        {'weight': 1, 'minimum': 1, 'maximum': 1, 'multiplier': 1, 'rewards': bundle}]}]]}
    catalog['loot_tables'] = {'participation': table({'wallet': {'credits': 10}}),
        'tier': {'groups': []}, 'first': table({'inventory': {'medal': 2}})}
    bonus = {'tier_rewards': {'wallet': {'credits': 5}},
        'completion_bonuses': [{'minimum_percent': 100, 'rewards': {'wallet': {'credits': 20}}}],
        'first_completion_bonuses': [{'minimum_percent': 100, 'rewards': {'inventory': {'trophy': 1}}}],
        'first_table_id': 'first', 'first_table_minimum_percent': 100}
    rule = catalog['raids']['expedition']
    rule['team_count'] = 1
    rule['settlement'] = {'missions_per_team': 3, 'thresholds': [60, 100], 'tier_table_ids': ['tier', 'tier'],
        'consolation_table_id': 'participation', 'difficulty_bonuses': [deepcopy(bonus) for _ in rule['difficulties']],
        'mail_duration_seconds': 2592000}
    return catalog


class RaidSettlementTests(unittest.TestCase):
    command = fixtures.RaidTests.command
    mutate_fixture = fixtures.RaidTests.mutate_fixture
    begin = combat.RaidCombatTests.begin
    finish = combat.RaidCombatTests.finish

    def setUp(self):
        fixtures.RaidTests.setUp(self)
        combat.RaidCombatTests.configure(self, settlement_catalog())

    def settle(self, key='settle'):
        return self.command('raid.settle', key, instance_id=self.context['instance_id'])

    def expire(self):
        self.now += 3600

    def mail(self):
        return self.service.get_mail('a')['messages'][0]

    def test_server_only_decides_pending_raid_closure_without_a_read_side_effect(self):
        original = self.service.get_player('a')
        self.assertEqual(self.service.get_raids('a')['pending_settlement_ids'], [])
        self.assertEqual(self.service.get_player('a'), original)
        self.expire()
        due = self.service.get_raids('a')
        self.assertEqual(due['pending_settlement_ids'], [self.context['instance_id']])
        self.assertEqual(self.service.get_player('a'), original)
        settled = self.command('raid.refresh', 'close-pending')
        self.assertEqual(len(settled['closed_raids']), 1)
        self.assertEqual(self.service.get_raids('a')['pending_settlement_ids'], [])
        self.assertEqual(self.service.get_raids('a', self.context['instance_id'])['raid']['status'], 'ended')
        self.assertEqual(self.command('raid.refresh', 'close-pending'), settled)

    def claim(self, key='claim', **extra):
        mail = self.mail()
        return self.command('mail.read_claim', key, message_ids=[mail['message_id']], gift_ids=[mail['gift_id']], **extra)

    def test_expiry_creates_one_unclaimed_gift_and_concurrent_claim_receipt_survives_restart(self):
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.settle()
        self.assertEqual(self.service.get_player('a'), before)
        self.expire()
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(lambda _: self.settle(), range(3)))
        self.assertTrue(all(row == rows[0] for row in rows))
        result = rows[0]
        self.assertEqual(result['raid']['status'], 'ended')
        self.assertEqual(result['settlement']['tier'], None)
        self.assertEqual(result['settlement']['completion_percent'], 0)
        self.assertEqual(result['player']['wallet'], before['wallet'])
        self.assertEqual(self.mail()['rewards']['wallet'], {'credits': 10})
        with ThreadPoolExecutor(max_workers=3) as pool:
            claimed = list(pool.map(lambda _: self.claim(), range(3)))
        self.assertTrue(all(row == claimed[0] for row in claimed))
        self.assertEqual(claimed[0]['player']['wallet']['credits'], before['wallet']['credits'] + 10)
        self.assertIsNotNone(self.mail()['read_at'])
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now + 9999999)
        mail = claimed[0]['messages'][0]
        self.assertEqual(self.command('mail.read_claim', 'claim', message_ids=[mail['message_id']], gift_ids=[mail['gift_id']]), claimed[0])
        self.assertEqual(self.service.get_mail('a')['messages'], [])

    def test_full_clear_closes_inside_combat_transaction_and_first_completion_rewards_once(self):
        def prior_nodes(p):
            p['raids'][self.context['instance_id']]['completed']['0'] = {
                'leader': {'stars': 3}, 'final': {'stars': 3}}
        self.mutate_fixture(prior_nodes)
        result = self.finish(self.begin()['battle'])
        self.assertEqual(result['raid']['status'], 'ended')
        self.assertEqual(result['settlement']['completion_percent'], 100)
        self.assertEqual(result['settlement']['rewards']['wallet'], {'credits': 25})
        self.assertEqual(result['settlement']['rewards']['inventory'], {'trophy': 1, 'medal': 2})
        before = self.service.get_player('a')
        self.assertEqual(before['inventory'].get('trophy', 0), 0)
        claimed = self.claim()
        self.assertEqual(claimed['player']['inventory']['trophy'], 1)
        raid = self.command('raid.launch', 'second-launch', raid_id='expedition')['raid']
        self.command('raid.join', 'second-join', raid_id='expedition', instance_id=raid['instance_id'], team_index=0)
        def all_nodes(p):
            p['raids'][raid['instance_id']]['completed']['0'] = {key: {'stars': 3} for key in ('guard', 'leader', 'final')}
        self.mutate_fixture(all_nodes)
        second = self.command('raid.settle', 'second-settle', instance_id=raid['instance_id'])
        self.assertEqual(second['settlement']['first_table_ids'], [])
        self.assertEqual(second['settlement']['rewards']['inventory'], {})
        self.assertEqual(second['settlement']['rewards']['wallet'], {'credits': 25})

    def test_empty_tier_does_not_substitute_consolation_and_unjoined_gets_no_mail(self):
        def progress(p):
            raid = p['raids'][self.context['instance_id']]
            raid['completed']['0'] = {key: {'stars': 3} for key in ('guard', 'leader')}
            # Difficulty tier bonus deliberately empty for this configured test.
        catalog = settlement_catalog()
        for level in catalog['raids']['expedition']['settlement']['difficulty_bonuses']:
            level['tier_rewards'] = {}
        # New database/catalog instead of altering a catalog-pinned save.
        self.catalog = catalog
        combat.RaidCombatTests.configure(self, catalog)
        self.mutate_fixture(progress)
        self.expire()
        result = self.settle()
        self.assertEqual(result['settlement']['tier'], 1)
        self.assertEqual(result['settlement']['rewards']['wallet'], {})
        mail = self.mail()
        self.assertIsNone(mail['gift_id'])
        self.command('mail.delete', 'delete-empty', message_ids=[mail['message_id']])
        raid = self.command('raid.launch', 'unjoined-launch', raid_id='expedition')['raid']
        self.now += 3600
        unjoined = self.command('raid.settle', 'unjoined-settle', instance_id=raid['instance_id'])
        self.assertIsNone(unjoined['settlement']['rank'])
        self.assertEqual(self.service.get_mail('a')['messages'], [])

    def test_failed_batch_gift_context_and_unclaimed_delete_roll_back_and_key_is_reusable(self):
        self.expire()
        self.settle()
        mail = self.mail()
        before = self.service.get_player('a')
        for command, parameters in [('mail.delete', {'message_ids': [mail['message_id']]}),
            ('mail.claim', {'message_ids': [mail['message_id']], 'gift_ids': ['wrong']}),
            ('mail.read_claim', {'message_ids': [mail['message_id'], 'missing'], 'gift_ids': [mail['gift_id'], 'missing']})]:
            with self.assertRaises(DomainError):
                self.command(command, 'retry', **parameters)
            self.assertEqual(self.service.get_player('a'), before)
        claimed = self.claim('retry')
        with self.assertRaises(DomainError):
            self.claim('again')
        self.command('mail.delete', 'delete', message_ids=[mail['message_id']])
        self.assertEqual(self.service.get_mail('a')['messages'], [])
        self.assertEqual(self.command('mail.read_claim', 'retry', message_ids=[mail['message_id']], gift_ids=[mail['gift_id']]), claimed)

    def test_invalid_reward_rolls_back_closure_and_catalog_rejects_unknown_tables(self):
        self.expire()
        before = self.service.get_player('a')
        with patch.object(self.service, '_reward', side_effect=DomainError('invalid_reward', 'bad reward')):
            with self.assertRaises(DomainError):
                self.settle()
        self.assertEqual(self.service.get_player('a'), before)
        self.assertEqual(self.settle()['raid']['status'], 'ended')
        for mutate in (lambda p: p.update(thresholds=[0, 'wrong']),
                       lambda p: p.update(tier_table_ids=['missing', 'tier']),
                       lambda p: p.update(missions_per_team=False)):
            catalog = settlement_catalog()
            mutate(catalog['raids']['expedition']['settlement'])
            with self.assertRaises(DomainError):
                validate_catalog(catalog)

    def test_source_disabled_first_clear_threshold_never_awards_its_table(self):
        catalog = settlement_catalog()
        for level in catalog['raids']['expedition']['settlement']['difficulty_bonuses']:
            level['first_table_minimum_percent'] = 200
        self.catalog = catalog
        combat.RaidCombatTests.configure(self, catalog)
        def all_nodes(p):
            p['raids'][self.context['instance_id']]['completed']['0'] = {
                key: {'stars': 3} for key in ('guard', 'leader', 'final')}
        self.mutate_fixture(all_nodes)
        result = self.settle()
        self.assertEqual(result['settlement']['completion_percent'], 100)
        self.assertEqual(result['settlement']['first_table_ids'], [])
        self.assertEqual(result['settlement']['rewards']['inventory'], {'trophy': 1})

    def test_mail_claim_closes_and_grants_atomically_without_prior_refresh(self):
        import hashlib
        self.expire()
        before = self.service.get_player('a')
        with self.assertRaises(DomainError):
            self.command('mail.claim', 'retry', gift_id='missing')
        self.assertEqual(self.service.get_player('a'), before)
        bid = hashlib.sha256(('raid-box:' + self.context['instance_id']).encode()).hexdigest()[:24]
        result = self.command('mail.claim', 'retry', gift_id=bid)
        self.assertEqual(len(result['closed_raids']), 1)
        self.assertEqual(result['closed_raids'][0]['status'], 'ended')
        self.assertEqual(result['player']['wallet']['credits'], before['wallet']['credits'] + 10)
        self.assertIsNotNone(result['messages'][0]['claimed_at'])
        self.assertEqual(self.command('mail.claim', 'retry', gift_id=bid), result)

    def test_refresh_all_closures_is_one_atomic_transaction(self):
        def another(p):
            raid = deepcopy(p['raids'][self.context['instance_id']])
            raid['instance_id'] = 'another'
            raid['raid_id'] = 'expedition'
            p['raids']['another'] = raid
        self.mutate_fixture(another)
        self.expire()
        before = self.service.get_player('a')
        reward = self.service._reward
        calls = []
        def fail_second(player, bundle, *args):
            calls.append(bundle)
            if len(calls) == 2:
                raise DomainError('invalid_reward', 'second delivery failed')
            return reward(player, bundle, *args)
        with patch.object(self.service, '_reward', side_effect=fail_second):
            with self.assertRaises(DomainError):
                self.command('raid.refresh', 'refresh', **{})
        self.assertEqual(self.service.get_player('a'), before)
        result = self.command('raid.refresh', 'refresh')
        self.assertEqual(len(result['closed_raids']), 2)
        self.assertEqual(len(self.service.get_mail('a')['messages']), 2)
        self.assertEqual(self.command('raid.refresh', 'refresh'), result)

    def test_archived_mail_is_read_delete_only_and_reads_are_pure(self):
        catalog = settlement_catalog()
        catalog['mail_messages'] = {'welcome': {'created_at': self.now - 10, 'expires_at': self.now + 100}}
        self.service = GameService(self.path.with_name('archived.sqlite'), catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        before = self.service.get_player('a')
        self.assertEqual(self.service.get_mail('a')['messages'][0]['kind'], 'archive')
        self.assertEqual(self.service.get_player('a'), before)
        with self.assertRaises(DomainError):
            self.command('mail.claim', 'claim', message_ids=['welcome'], gift_ids=['fake'])
        self.assertEqual(self.service.get_player('a'), before)
        first = self.command('mail.read', 'read', message_ids=['welcome'])
        self.assertEqual(first['messages'][0]['read_at'], self.now)
        self.now += 1
        self.assertEqual(self.command('mail.read', 'read-again', message_ids=['welcome'])['messages'][0]['read_at'], self.now - 1)
        self.command('mail.delete', 'delete', message_ids=['welcome'])
        self.assertEqual(self.service.get_mail('a')['messages'], [])


if __name__ == '__main__':
    unittest.main()

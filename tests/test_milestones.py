"""Shared objective milestones, expected-period claims and real UTC month boundaries."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


def timestamp(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp())


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = timestamp('2028-02-28T12:00:00')
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['missions']['training.1']['cost'] = {'energy': 6}
        self.catalog['offers']['training-kit'].update(purchase_limit=20,
            rewards={'signals': [{'labels': ['alpha', 'beta'], 'quantity': 3}]})
        activity = {'kind': 'activity', 'rules': [{'command': 'shop.purchase', 'measure': 'quantity'}]}
        self.catalog['objectives'] = {'counters': {
            'daily': {'source': deepcopy(activity), 'period': {'seconds': 86400, 'anchor': 0}},
            'monthly': {'source': deepcopy(activity), 'period': {'calendar': 'month', 'anchor': 0}},
            'onboarding': {'source': {'kind': 'activity', 'rules': [
                {'command': 'mission.begin', 'measure': 'wallet_spent', 'currency_id': 'energy'}]},
                'period': {'seconds': 30, 'anchor': 'player_created', 'repeat': False}},
            'tags': {'source': {'kind': 'signals', 'label_ids': ['alpha', 'beta'], 'multiplier': 5},
                     'period': {'seconds': 86400, 'anchor': 0}}}, 'entries': {}}
        entries = self.catalog['objectives']['entries']
        for key, counter, threshold in [('daily-one', 'daily', 1), ('daily-two', 'daily', 2),
                                         ('monthly-one', 'monthly', 1), ('onboarding-one', 'onboarding', 6),
                                         ('tags-one', 'tags', 15)]:
            entries[key] = {'counter_id': counter, 'threshold': threshold, 'required_level': 1,
                            'maximum_level': 100, 'rewards': {'inventory': {key + '-prize': 1}}}
        self.catalog['milestones'] = {key: {'counter_id': key, 'objective_ids': [target for target in entries
            if entries[target]['counter_id'] == key], 'display_grace_seconds': 10} for key in ('daily', 'monthly', 'onboarding', 'tags')}
        self.service = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})

    def command(self, name, key, **parameters):
        return self.service.execute('a', {'command': name, 'idempotency_key': key, 'parameters': parameters})

    def buy(self, key='buy', quantity=1):
        return self.command('shop.purchase', key, offer_id='training-kit', quantity=quantity)

    def test_expected_period_guards_old_urls_and_exact_receipts_survive_rollover_restart(self):
        self.buy(quantity=2)
        before = self.service.get_player('a')
        view = self.service.get_milestones('a')
        self.assertEqual(self.service.get_player('a'), before)
        old_key = view['milestones']['daily']['current']['period']['key']
        first = self.command('objective.claim', 'claim', objective_ids=['daily-one'], period_keys={'daily-one': old_key})
        self.assertTrue(first['milestone_state']['milestones']['daily']['current']['objectives']['daily-one']['claimed'])
        self.now += 86400
        self.buy('new-day')
        current = self.service.get_milestones('a')['milestones']['daily']
        self.assertEqual(current['current']['score'], 1)
        self.assertEqual(current['history'][0]['score'], 2)
        self.assertFalse(current['history'][0]['visible'])
        before = self.service.get_player('a')
        with self.assertRaises(DomainError) as error:
            self.command('objective.claim', 'stale', objective_ids=['daily-one'], period_keys={'daily-one': old_key})
        self.assertEqual(error.exception.code, 'objective_period_conflict')
        self.assertEqual(self.service.get_player('a'), before)
        restarted = GameService(self.path, self.catalog, clock=lambda: self.now)
        self.assertEqual(restarted.execute('a', {'command': 'objective.claim', 'idempotency_key': 'claim',
            'parameters': {'objective_ids': ['daily-one'], 'period_keys': {'daily-one': old_key}}}), first)

    def test_monthly_counter_rolls_on_real_leap_month_and_archived_claims_persist(self):
        self.buy()
        february = self.service.get_milestones('a')['milestones']['monthly']['current']
        self.assertEqual(february['period']['ends_at'], timestamp('2028-03-01'))
        key = february['period']['key']
        self.command('objective.claim', 'feb-claim', objective_ids=['monthly-one'], period_keys={'monthly-one': key})
        self.now = timestamp('2028-02-29T12:00:00')
        self.buy('leap-day')
        self.now = timestamp('2028-03-01')
        view = self.service.get_milestones('a')['milestones']['monthly']
        self.assertEqual(view['current']['score'], 0)
        self.assertEqual(view['history'][0]['score'], 2)
        self.assertTrue(view['history'][0]['objectives']['monthly-one']['claimed'])
        self.assertEqual(view['current']['period']['ends_at'], timestamp('2028-04-01'))

    def test_oneoff_scoring_stops_and_display_grace_does_not_authorize_claims(self):
        self.command('mission.begin', 'begin', mission_id='training.1', team=['scout'])
        current = self.service.get_milestones('a')['milestones']['onboarding']['current']
        self.assertEqual(current['score'], 6)
        self.assertTrue(current['objectives']['onboarding-one']['claimable'])
        self.now += 30
        current = self.service.get_milestones('a')['milestones']['onboarding']['current']
        self.assertTrue(current['visible'])
        self.assertFalse(current['objectives']['onboarding-one']['claimable'])
        with self.assertRaises(DomainError):
            self.command('objective.claim', 'expired', objective_ids=['onboarding-one'], period_keys={'onboarding-one': '0'})
        self.now += 11
        self.assertFalse(self.service.get_milestones('a')['milestones']['onboarding']['current']['visible'])

    def test_signal_multiplier_counts_multilabel_action_once_in_its_period(self):
        result = self.buy()
        view = result['milestone_state']['milestones']['tags']['current']
        self.assertEqual(view['score'], 15)
        self.assertTrue(view['objectives']['tags-one']['claimable'])
        self.now += 86400
        view = self.service.get_milestones('a')['milestones']['tags']
        self.assertEqual(view['current']['score'], 0)
        self.assertEqual(view['history'][0]['score'], 15)

    def test_invalid_periods_group_thresholds_and_partial_claim_guards_are_rejected(self):
        for period in ({'calendar': 'month', 'anchor': 1}, {'calendar': 'week', 'anchor': 0},
                       {'calendar': 'month', 'anchor': 0, 'local_time': True}):
            catalog = deepcopy(self.catalog)
            catalog['objectives']['counters']['monthly']['period'] = period
            with self.assertRaises(DomainError):
                validate_catalog(catalog)
        catalog = deepcopy(self.catalog)
        catalog['milestones']['daily']['objective_ids'] = ['daily-two', 'daily-one']
        with self.assertRaises(DomainError):
            validate_catalog(catalog)
        self.buy(quantity=2)
        before = self.service.get_player('a')
        current_key = self.service.get_milestones('a')['milestones']['daily']['current']['period']['key']
        for expected in (None, {}, {'daily-one': 'wrong'}, {'daily-one': current_key, 'daily-two': 'wrong'}):
            with self.assertRaises(DomainError):
                self.command('objective.claim', 'invalid', objective_ids=['daily-one', 'daily-two'], period_keys=expected)
        self.assertEqual(self.service.get_player('a'), before)

    def test_monthly_pass_history_uses_the_same_calendar_period_resolver(self):
        catalog = deepcopy(self.catalog)
        catalog['passes'] = {'season': {'counter_id': 'monthly', 'required_level': 1, 'maximum_level': 100,
            'premium_required_level': 1, 'rewards': [{'reward_id': 'earned', 'threshold': 1, 'track': 'free',
                                                   'rewards': {'wallet': {'credits': 1}}}]}}
        self.service = GameService(Path(self.temp.name) / 'monthly-pass.sqlite', catalog, clock=lambda: self.now)
        self.service.create_player({'player_id': 'a', 'display_name': 'A'})
        self.buy()
        period_key = self.service.get_passes('a')['passes']['season']['current']['period']['key']
        self.command('pass.claim', 'claim-pass', pass_id='season', period_key=period_key, reward_ids=['earned'])
        self.now = timestamp('2028-03-01')
        state = self.service.get_passes('a')['passes']['season']
        self.assertEqual(state['current']['points'], 0)
        self.assertEqual(state['history'][0]['points'], 1)
        self.assertTrue(state['history'][0]['rewards'][0]['claimed'])

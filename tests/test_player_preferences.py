"""Earned appearance and reward-free guide state share application receipts."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from game_server.catalog import DEMO_CATALOG, DomainError, validate_catalog
from game_server.service import GameService


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = deepcopy(DEMO_CATALOG)
        self.catalog['appearance'] = {'default': 'plain', 'choices': {'plain': {}, 'earned': {'required_item': 'badge'}}}
        self.catalog['guides'] = {'intro': {'events': {
            'start': {'completes': False}, 'save': {'completes': False, 'checkpoint': 'step-a'},
            'finish': {'completes': True, 'checkpoint': 'step-b'}}}, 'other': {'events': {}}}
        self.database = Path(self.temp.name) / 'game.sqlite'
        self.service = GameService(self.database, self.catalog)
        for key in ('a', 'b'):
            self.service.create_player({'player_id': key, 'display_name': key})

    def command(self, name, key, **parameters):
        return self.service.execute('a', {'command': name, 'idempotency_key': key, 'parameters': parameters})

    def test_appearance_requires_earned_item_and_does_not_spend_it(self):
        self.assertEqual(self.service.get_preferences('a')['appearance_id'], 'plain')
        before = self.service.get_player('a')
        with self.assertRaisesRegex(DomainError, 'earned'):
            self.command('appearance.select', 'select', appearance_id='earned')
        self.assertEqual(self.service.get_player('a'), before)
        battle = self.command('mission.begin', 'begin', mission_id='training.1', team=['scout'])['battle']
        self.command('mission.complete', 'win', mission_id='training.1', battle_id=battle['battle_id'], outcome='victory', stars=3)
        before = self.service.get_player('a')
        first = self.command('appearance.select', 'select', appearance_id='earned')
        self.assertEqual(first['player']['wallet'], before['wallet'])
        self.assertEqual(first['player']['inventory'], before['inventory'])
        self.assertEqual(self.command('appearance.select', 'select', appearance_id='earned'), first)
        self.command('appearance.select', 'reset', appearance_id='plain')
        self.assertEqual(self.service.get_preferences('b')['appearance_id'], 'plain')
        self.assertEqual(GameService(self.database, self.catalog).get_preferences('a')['appearance_id'], 'plain')
        with self.assertRaises(DomainError):
            self.command('appearance.select', 'select', appearance_id='plain')

    def test_events_persist_only_successful_savepoints_and_completion_without_rewards(self):
        before = self.service.get_player('a')
        for event_id, success in (('start', True), ('save', False), ('finish', False)):
            result = self.command('guide.event', event_id, guide_id='intro', event_id=event_id, success=success)
            self.assertEqual(result['guides']['intro'], {'completed': False})
        saved = self.command('guide.event', 'checkpoint', guide_id='intro', event_id='save', success=True)
        self.assertEqual(saved['guides']['intro'], {'completed': False, 'checkpoint': 'step-a'})
        final = self.command('guide.event', 'final', guide_id='intro', event_id='finish', success=True)
        restarted = GameService(self.database, self.catalog)
        self.assertEqual(restarted.get_preferences('a')['guides']['intro'], {'completed': True, 'checkpoint': 'step-b'})
        replay = restarted.execute('a', {'command': 'guide.event', 'idempotency_key': 'final',
                                        'parameters': {'guide_id': 'intro', 'event_id': 'finish', 'success': True}})
        self.assertEqual(replay, final)
        after = self.service.get_player('a')
        for field in ('wallet', 'inventory', 'missions', 'characters', 'level', 'experience'):
            self.assertEqual(before[field], after[field], field)
        self.assertEqual(self.service.get_preferences('b')['guides']['intro'], {'completed': False})

    def test_acknowledgement_batch_and_invalid_events_are_atomic(self):
        before = self.service.get_player('a')
        for parameters in ({'guide_ids': ['intro', 'undefined']}, {'guide_ids': ['intro', 'intro']},
                           {'guide_ids': []}, {'guide_ids': [True]}, {'guide_ids': ['intro'], 'reward': 1}):
            with self.subTest(parameters=parameters), self.assertRaises(DomainError):
                self.command('guide.acknowledge', 'ack', **parameters)
            self.assertEqual(self.service.get_player('a'), before)
        for event, success in (('undefined', True), ('save', 1)):
            with self.assertRaises(DomainError):
                self.command('guide.event', 'bad', guide_id='intro', event_id=event, success=success)
            self.assertEqual(self.service.get_player('a'), before)
        result = self.command('guide.acknowledge', 'ack', guide_ids=['intro', 'other'])
        self.assertTrue(all(value['completed'] for value in result['guides'].values()))
        self.assertEqual(self.command('guide.acknowledge', 'ack', guide_ids=['intro', 'other']), result)

    def test_catalog_closes_native_fields_and_requires_free_default(self):
        for path, value in ((('appearance', 'default'), 'undefined'),
                            (('appearance',), None),
                            (('appearance', 'choices', 'plain'), {'required_item': 'badge'}),
                            (('guides', 'intro', 'events', 'save'), {'completes': 1}),
                            (('guides', 'intro', 'events', 'save'), {'completes': False, 'external_module': 'wire'})):
            catalog = deepcopy(self.catalog)
            parent = catalog
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = value
            with self.subTest(path=path, value=value), self.assertRaises(DomainError):
                validate_catalog(catalog)

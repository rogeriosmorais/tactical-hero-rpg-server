"""Atomic application commands, progression, balances and replay protection."""
from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3

from .catalog import (DEMO_CATALOG, DomainError, fail, identifier, integer,
                      object_fields, validate_catalog)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def awarded(*bundles):
    result = {'wallet': {}, 'inventory': {}, 'experience': 0}
    for bundle, count in bundles:
        if count == 0:
            continue
        for category in ('wallet', 'inventory'):
            for key, value in bundle.get(category, {}).items():
                result[category][key] = integer(result[category].get(key, 0) + value * count)
        result['experience'] = integer(result['experience'] + bundle.get('experience', 0) * count)
    return result


class GameService:
    def __init__(self, database, catalog=None):
        self.database = Path(database)
        self.catalog = validate_catalog(DEMO_CATALOG if catalog is None else catalog)
        self.catalog_digest = hashlib.sha256(canonical(self.catalog).encode()).hexdigest()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and not {'settings', 'players', 'commands', 'battles'} <= tables:
                fail('foreign_database', 'Database does not belong to this application service', 409)
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS players (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS commands (
                    player_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(player_id,idempotency_key));
                CREATE TABLE IF NOT EXISTS battles (id TEXT PRIMARY KEY, player_id TEXT NOT NULL, data TEXT NOT NULL);
            ''')
            connection.execute('BEGIN IMMEDIATE')
            old = connection.execute("SELECT value FROM settings WHERE key='catalog'").fetchone()
            if old and old[0] != self.catalog_digest:
                fail('catalog_mismatch', 'Database belongs to a different catalog; explicit migration is required', 409)
            connection.execute("INSERT OR IGNORE INTO settings VALUES ('catalog',?)", (self.catalog_digest,))

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=15)
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def create_player(self, request):
        object_fields(request, ('player_id', 'display_name'))
        player_id = identifier(request['player_id'])
        name = request['display_name']
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            fail('invalid_name', 'Display name must contain 1–64 characters')
        with self.transaction() as connection:
            existing = connection.execute('SELECT data FROM players WHERE id=?', (player_id,)).fetchone()
            if existing:
                return json.loads(existing[0])
            player = {'player_id': player_id, 'display_name': name, 'version': 0, 'level': 1, 'experience': 0,
                      'wallet': deepcopy(self.catalog['starting_wallet']), 'inventory': {},
                      'characters': {key: {'level': level, 'power': integer(level * self.catalog['characters'][key]['power_per_level'])}
                                     for key, level in self.catalog['starting_characters'].items()},
                      'missions': {}, 'offer_purchases': {}}
            connection.execute('INSERT INTO players VALUES (?,?)', (player_id, canonical(player)))
            return player

    @staticmethod
    def _player(connection, player_id):
        row = connection.execute('SELECT data FROM players WHERE id=?', (identifier(player_id),)).fetchone()
        if not row:
            fail('player_not_found', 'Player does not exist', 404)
        return json.loads(row[0])

    def get_player(self, player_id):
        with closing(self.connect()) as connection:
            return self._player(connection, player_id)

    def get_battle(self, player_id, battle_id):
        with closing(self.connect()) as connection:
            self._player(connection, player_id)
            row = connection.execute('SELECT data FROM battles WHERE id=? AND player_id=?',
                                     (identifier(battle_id), player_id)).fetchone()
            if not row:
                fail('battle_not_found', 'Battle does not belong to this player', 404)
            return json.loads(row[0])

    @staticmethod
    def _spend(player, cost, count=1):
        for key, value in cost.items():
            total = integer(value * count)
            if player['wallet'].get(key, 0) < total:
                fail('insufficient_funds', 'The configured cost cannot be paid', 409)
        for key, value in cost.items():
            player['wallet'][key] = player['wallet'].get(key, 0) - value * count

    def _reward(self, player, rewards, count=1):
        for category in ('wallet', 'inventory'):
            for key, value in rewards.get(category, {}).items():
                player[category][key] = integer(player[category].get(key, 0) + value * count)
        player['experience'] = integer(player['experience'] + rewards.get('experience', 0) * count)
        player['level'] = min(self.catalog['maximum_player_level'],
                              1 + player['experience'] // self.catalog['experience_per_level'])

    def _mission(self, player, mission_id):
        definition = self.catalog['missions'].get(identifier(mission_id))
        if definition is None:
            fail('mission_not_found', 'Mission does not exist', 404)
        prerequisite = definition.get('prerequisite')
        if player['level'] < definition['required_level'] or (prerequisite and not player['missions'].get(prerequisite, {}).get('wins')):
            fail('mission_locked', 'Mission requirements are not met', 409)
        return definition

    def execute(self, player_id, request):
        object_fields(request, ('command', 'idempotency_key', 'parameters'))
        command, key, parameters = request['command'], identifier(request['idempotency_key']), request['parameters']
        if not isinstance(command, str) or not isinstance(parameters, dict):
            fail('invalid_command', 'Command and parameters are required')
        digest = hashlib.sha256(canonical(request).encode()).hexdigest()
        with self.transaction() as connection:
            player = self._player(connection, player_id)
            old = connection.execute('SELECT request_hash,result FROM commands WHERE player_id=? AND idempotency_key=?',
                                     (player_id, key)).fetchone()
            if old:
                if old[0] != digest:
                    fail('idempotency_conflict', 'Key was already used for a different command', 409)
                return json.loads(old[1])
            previous_wallet, previous_inventory = deepcopy(player['wallet']), deepcopy(player['inventory'])
            result = self._apply(connection, player, command, parameters)
            player['version'] += 1
            result['player'] = player
            result['wallet_changes'] = [
                {'currency_id': key, 'previous_balance': previous_wallet.get(key, 0), 'balance': player['wallet'].get(key, 0)}
                for key in sorted(set(previous_wallet) | set(player['wallet']))
                if previous_wallet.get(key, 0) != player['wallet'].get(key, 0)]
            result['inventory_changes'] = [
                {'item_id': key, 'previous_quantity': previous_inventory.get(key, 0), 'quantity': player['inventory'].get(key, 0)}
                for key in sorted(set(previous_inventory) | set(player['inventory']))
                if previous_inventory.get(key, 0) != player['inventory'].get(key, 0)]
            connection.execute('UPDATE players SET data=? WHERE id=?', (canonical(player), player_id))
            connection.execute('INSERT INTO commands VALUES (?,?,?,?)', (player_id, key, digest, canonical(result)))
            return result

    def _apply(self, connection, player, command, parameters):
        if command == 'character.train':
            object_fields(parameters, ('character_id', 'levels'))
            key, count = identifier(parameters['character_id']), integer(parameters['levels'], 1, 100)
            if key not in player['characters']:
                fail('character_not_owned', 'Character is not owned', 409)
            definition = self.catalog['characters'][key]
            if player['characters'][key]['level'] + count > definition['maximum_level']:
                fail('maximum_level', 'Character level would exceed its limit', 409)
            self._spend(player, definition['training_cost'], count)
            player['characters'][key]['level'] += count
            player['characters'][key]['power'] = integer(player['characters'][key]['level'] * definition['power_per_level'])
            return {'character_id': key}
        if command == 'shop.purchase':
            object_fields(parameters, ('offer_id', 'quantity'))
            key, count = identifier(parameters['offer_id']), integer(parameters['quantity'], 1, 100)
            definition = self.catalog['offers'].get(key)
            if not definition:
                fail('offer_not_found', 'Offer does not exist', 404)
            if player['offer_purchases'].get(key, 0) + count > definition['purchase_limit']:
                fail('purchase_limit', 'Offer purchase limit exceeded', 409)
            self._spend(player, definition['cost'], count)
            self._reward(player, definition['rewards'], count)
            player['offer_purchases'][key] = player['offer_purchases'].get(key, 0) + count
            return {'offer_id': key, 'quantity': count, 'rewards': awarded((definition['rewards'], count))}
        if command == 'mission.begin':
            object_fields(parameters, ('mission_id', 'team'))
            mission_id = parameters['mission_id']
            mission = self._mission(player, mission_id)
            team = parameters['team']
            if not isinstance(team, list) or not 1 <= len(team) <= 5 or any(not isinstance(x, str) for x in team):
                fail('invalid_team', 'Team must contain 1–5 owned characters')
            if len(set(team)) != len(team) or any(x not in player['characters'] for x in team):
                fail('invalid_team', 'Team must contain distinct owned characters')
            self._spend(player, mission['cost'])
            battle = {'battle_id': secrets.token_hex(16), 'mission_id': mission_id, 'team': team, 'status': 'active'}
            connection.execute('INSERT INTO battles VALUES (?,?,?)', (battle['battle_id'], player['player_id'], canonical(battle)))
            return {'battle': battle}
        if command == 'mission.complete':
            object_fields(parameters, ('battle_id', 'mission_id', 'outcome', 'stars'))
            battle_id = identifier(parameters['battle_id'])
            outcome, stars = parameters['outcome'], integer(parameters['stars'], 0, 3)
            if outcome not in ('victory', 'defeat', 'retreat') or (outcome == 'victory') != (stars > 0):
                fail('invalid_outcome', 'Outcome and star count disagree')
            row = connection.execute('SELECT data FROM battles WHERE id=? AND player_id=?',
                                     (battle_id, player['player_id'])).fetchone()
            if not row:
                fail('battle_not_found', 'Battle does not belong to this player', 404)
            battle = json.loads(row[0])
            if battle['mission_id'] != parameters['mission_id']:
                fail('battle_mission_conflict', 'Battle belongs to a different mission', 409)
            if battle['status'] != 'active':
                fail('battle_finished', 'Battle was already completed', 409)
            rewards = {}
            if outcome == 'victory':
                mission_id = battle['mission_id']
                mission = self.catalog['missions'][mission_id]
                previous = player['missions'].get(mission_id, {'best_stars': 0, 'wins': 0})
                rewards = awarded((mission['rewards'], 1),
                                  (mission.get('first_clear_rewards', {}), 1 if previous['wins'] == 0 else 0))
                self._reward(player, rewards)
                player['missions'][mission_id] = {'best_stars': max(previous['best_stars'], stars), 'wins': previous['wins'] + 1}
            battle.update(status='complete', outcome=outcome, stars=stars)
            connection.execute('UPDATE battles SET data=? WHERE id=?', (canonical(battle), battle_id))
            return {'battle': battle, 'rewards': rewards}
        if command == 'mission.sweep':
            object_fields(parameters, ('mission_id', 'quantity'))
            mission_id, count = parameters['mission_id'], integer(parameters['quantity'], 1, 100)
            definition = self._mission(player, mission_id)
            progress = player['missions'].get(mission_id, {})
            if progress.get('best_stars') != 3:
                fail('sweep_locked', 'A three-star clear is required', 409)
            self._spend(player, definition['cost'], count)
            self._reward(player, definition['rewards'], count)
            progress['wins'] += count
            return {'mission_id': mission_id, 'quantity': count, 'rewards': awarded((definition['rewards'], count))}
        fail('unsupported_command', 'Command is not part of API v1', 404)

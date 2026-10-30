"""Atomic application commands, progression, balances and replay protection."""
from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time

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
        if bundle.get('signals'):
            result.setdefault('signals', []).extend({'labels': deepcopy(signal['labels']),
                'quantity': integer(signal['quantity'] * count)} for signal in bundle['signals'])
    return result


class GameService:
    def __init__(self, database, catalog=None, *, clock=None):
        self.database = Path(database)
        self.catalog = validate_catalog(DEMO_CATALOG if catalog is None else catalog)
        self.clock = clock or time.time
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

    def backup(self, destination):
        """Move legacy ProfileStore.backup ownership to the public database.

        This is an explicit trusted maintenance operation, not a client/API
        command. SQLite's online backup reads a consistent committed snapshot
        of the public player, battle, receipt and catalog state.
        """
        target = Path(destination)
        if target.resolve() == self.database.resolve() or target.exists():
            fail('backup_already_exists', 'Backup destination must be new', 409)
        target.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with closing(self.connect()) as source:
                # SQLite's URI mode=rw refuses an absent source database;
                # the source is already initialized by GameService.
                target.touch(exist_ok=False)
                created = True
                with closing(sqlite3.connect(target)) as backup_connection:
                    source.backup(backup_connection)
                    check = backup_connection.execute('PRAGMA integrity_check').fetchone()
                    if check != ('ok',):
                        fail('backup_integrity', 'Backup failed SQLite integrity validation')
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
        return {'database': str(target), 'catalog_digest': self.catalog_digest}

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
        object_fields(request, ('player_id', 'display_name'), ('reset_offset_seconds', 'alternate_reset'))
        player_id = identifier(request['player_id'])
        name = request['display_name']
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            fail('invalid_name', 'Display name must contain 1–64 characters')
        if 'reset_offset_seconds' in request:
            integer(request['reset_offset_seconds'], -86400, 86400)
        if 'alternate_reset' in request and type(request['alternate_reset']) is not bool:
            fail('invalid_reset_policy', 'Alternate reset selection must be boolean')
        with self.transaction() as connection:
            existing = connection.execute('SELECT data FROM players WHERE id=?', (player_id,)).fetchone()
            if existing:
                return json.loads(existing[0])
            player = {'player_id': player_id, 'display_name': name, 'version': 0, 'level': 1, 'experience': 0,
                      'wallet': deepcopy(self.catalog['starting_wallet']), 'inventory': {},
                      'characters': {key: {'level': level, 'power': integer(level * self.catalog['characters'][key]['power_per_level'])}
                                     for key, level in self.catalog['starting_characters'].items()},
                      'missions': {}, 'offer_purchases': {}}
            for field in ('reset_offset_seconds', 'alternate_reset'):
                if field in request:
                    player[field] = request[field]
            if 'objectives' in self.catalog:
                player.update(created_at=int(self.clock()), login_days=[], activity_windows={}, objective_claims={})
                player['level_reached_at'] = {'1': player['created_at']}
            if 'development' in self.catalog:
                from .development import initialize
                for key, character in player['characters'].items():
                    definition = self.catalog['development']['characters'].get(key)
                    if definition is not None:
                        initialize(character, definition)
            if 'resource_rules' in self.catalog:
                from .resources import initialize
                initialize(player, self.catalog)
            if 'access_rules' in self.catalog:
                from .access import reconcile
                reconcile(self, player)
            if 'alliances' in self.catalog:
                from .alliances import reconcile
                reconcile(self, player)
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

    def get_squads(self, player_id):
        from .squads import view
        player = self.get_player(player_id)
        return {'squads': view(player, self.catalog.get('squad_groups', {})),
                'updated_at': player.get('squads_updated_at', 0)}

    def get_preferences(self, player_id):
        from .player_preferences import view
        return view(self.get_player(player_id), self.catalog)

    def get_client_settings(self, player_id):
        from .client_settings import view
        return view(self.get_player(player_id), self.catalog)

    def get_challenge_limits(self, player_id):
        from .challenge_limits import view
        return view(self.get_player(player_id), self.catalog, int(self.clock()))

    def get_augmentations(self, player_id):
        return self._augmentation_view(self.get_player(player_id))

    def get_objectives(self, player_id):
        from .objectives import view
        rules = self.catalog.get('objectives')
        if rules is None:
            fail('feature_unavailable', 'Objectives are not configured', 404)
        return view(self.get_player(player_id), rules, int(self.clock()))

    def get_passes(self, player_id):
        from .passes import view
        return view(self.get_player(player_id), self.catalog, int(self.clock()))

    def get_access(self, player_id):
        from .access import view
        return {'access': view(self.get_player(player_id), self.catalog.get('access_rules', {}))}

    def get_progression(self, player_id):
        from .player_progression import view
        return {'progression': view(self.get_player(player_id), self.catalog)}

    def get_resources(self, player_id):
        from .resources import view
        return {'resources': view(self.get_player(player_id), self.catalog)}

    def get_calendars(self, player_id):
        from .calendars import view
        return view(self.get_player(player_id), self.catalog.get('calendars', {}), int(self.clock()))

    def get_milestones(self, player_id):
        from .milestones import view
        return view(self.get_player(player_id), self.catalog, int(self.clock()))

    def get_alliances(self, player_id, alliance_id=None):
        result = self._alliance_view(self.get_player(player_id), int(self.clock()))
        if alliance_id is not None:
            row = next((row for row in result['alliances'] if row['alliance_id'] == identifier(alliance_id)), None)
            if row is None:
                fail('alliance_not_found', 'Alliance is not in this profile', 404)
            return {'alliance': row, 'server_time': result['server_time']}
        return result

    def get_raids(self, player_id, instance_id=None):
        from .raids import view
        return view(self.get_player(player_id), self.catalog, int(self.clock()), instance_id)

    def _alliance_view(self, player, now):
        from .alliances import view
        result = view(player, self.catalog.get('alliances'), now)
        if 'alliance_economy' in self.catalog:
            from .alliance_economy import view as economy_view
            result['economy'] = economy_view(player, self.catalog, now)
        if 'raids' in self.catalog:
            from .raids import progress
            for row in result['alliances']:
                row['raid_progress'] = {key: progress(player, row, key) for key in self.catalog['raids']}
        return result

    def search_alliances(self, player_id, parameters):
        from .alliances import search
        return search(self.get_player(player_id), self.catalog.get('alliances'), parameters, int(self.clock()))

    def _augmentation_view(self, player):
        from .enhancements import available
        promotions = self.catalog.get('promotions')
        return {'enhancements_available': available(player, self.catalog.get('enhancements')),
                'promotions_available': bool(promotions and player['level'] >= promotions['required_level'])}

    def get_free_crates(self, player_id):
        definition = self.catalog.get('free_crates')
        if definition is None:
            fail('feature_unavailable', 'No free-crate schedule is configured', 404)
        from .free_crates import advance
        now = int(self.clock())
        with closing(self.connect()) as connection:
            player = self._player(connection, player_id)
            # Reads never initialize or mutate durable profile state.
            return {'schedule': advance(player, definition, now), 'server_time': now}

    def get_tower(self, player_id, tower_id):
        from .towers import view
        with closing(self.connect()) as connection:
            return view(self, self._player(connection, player_id), tower_id, int(self.clock()))

    def get_roguelite(self, player_id):
        from .roguelite import view
        return view(self, self.get_player(player_id))

    def get_battle(self, player_id, battle_id):
        with closing(self.connect()) as connection:
            self._player(connection, player_id)
            row = connection.execute('SELECT data FROM battles WHERE id=? AND player_id=?',
                                     (identifier(battle_id), player_id)).fetchone()
            if not row:
                fail('battle_not_found', 'Battle does not belong to this player', 404)
            return json.loads(row[0])

    def _spend(self, player, cost, count=1):
        from .resources import unlimited
        for key, value in cost.items():
            total = integer(value * count)
            if not unlimited(self.catalog, key) and player['wallet'].get(key, 0) < total:
                fail('insufficient_funds', 'The configured cost cannot be paid', 409)
        for key, value in cost.items():
            if not unlimited(self.catalog, key):
                player['wallet'][key] = player['wallet'].get(key, 0) - value * count
            from .objectives import track
            track(player, 'wallet_spent', key, value * count)

    def _reward(self, player, rewards, count=1):
        for category in ('wallet', 'inventory'):
            for key, value in rewards.get(category, {}).items():
                if category == 'wallet':
                    from .resources import unlimited
                    if unlimited(self.catalog, key):
                        continue
                player[category][key] = integer(player[category].get(key, 0) + value * count)
                if category == 'inventory':
                    from .objectives import track
                    track(player, 'inventory_earned', key, value * count)
        old_level = player['level']
        player['experience'] = integer(player['experience'] + rewards.get('experience', 0) * count)
        from .player_progression import level_for_experience
        player['level'] = level_for_experience(self.catalog, player['experience'])
        if 'objectives' in self.catalog:
            for level in range(old_level + 1, player['level'] + 1):
                player.setdefault('level_reached_at', {})[str(level)] = self._command_time(player)
        for signal in rewards.get('signals', []):
            quantity = integer(signal['quantity'] * count)
            if quantity:
                player.setdefault('reward_signals', []).append({'signal_id': secrets.token_hex(16), 'at': self._command_time(player),
                    'labels': deepcopy(signal['labels']), 'quantity': quantity})
        if 'alliances' in self.catalog:
            from .alliances import reconcile
            reconcile(self, player)

    def _command_time(self, player):
        transaction = player.get('_transaction_activity')
        return transaction['at'] if transaction is not None else int(self.clock())

    def _mission(self, player, mission_id):
        definition = self.catalog['missions'].get(identifier(mission_id))
        if definition is None:
            fail('mission_not_found', 'Mission does not exist', 404)
        prerequisite = definition.get('prerequisite')
        if player['level'] < definition['required_level'] or (prerequisite and not player['missions'].get(prerequisite, {}).get('wins')):
            fail('mission_locked', 'Mission requirements are not met', 409)
        return definition

    def lookup_receipt(self, player_id, request):
        """Inspect the existing command hash and receipt without applying a command."""
        object_fields(request, ('idempotency_key',), ('request_hash', 'command', 'request_fingerprint'))
        key = identifier(request['idempotency_key'])
        fingerprint = request.get('request_fingerprint')
        if 'command' in request or 'request_fingerprint' in request:
            if ('request_hash' in request or request.get('command') not in ('raid.heal', 'mail.read', 'mail.claim', 'mail.read_claim', 'mail.delete', 'roguelite.choose') or
                    not isinstance(fingerprint, str) or len(fingerprint) != 64 or
                    any(c not in '0123456789abcdef' for c in fingerprint)):
                fail('invalid_receipt_identity', 'Recovery lookup requires command and SHA-256 request identity')
        digest = request.get('request_hash')
        if 'request_hash' in request and (not isinstance(digest, str) or len(digest) != 64 or
                any(c not in '0123456789abcdef' for c in digest)):
            fail('invalid_request_hash', 'Request hash must be a SHA-256 hex digest')
        with closing(self.connect()) as connection:
            self._player(connection, player_id)
            old = connection.execute('SELECT request_hash,result FROM commands WHERE player_id=? AND idempotency_key=?',
                                     (player_id, key)).fetchone()
        if old is None:
            return {'status': 'missing'}
        if fingerprint is not None:
            result = json.loads(old[1])
            if result.get('receipt_command') == request['command'] and result.get('request_fingerprint') == fingerprint:
                return {'status': 'match', 'result': result}
            return {'status': 'conflict'}
        if old[0] != digest:
            return {'status': 'conflict'}
        return {'status': 'match', 'result': json.loads(old[1])}

    def get_raid_energy(self, player_id):
        from .raid_energy import view
        with closing(self.connect()) as connection:
            return view(self._player(connection, player_id), self.catalog, int(self.clock()))

    def get_mail(self, player_id):
        from .mail import view
        with closing(self.connect()) as connection:
            return view(self._player(connection, player_id), self.catalog, int(self.clock()))

    def get_raid_schedule(self, player_id):
        from .raid_schedule import view
        with closing(self.connect()) as connection:
            return view(self._player(connection, player_id), self.catalog, int(self.clock()))

    def get_ranked_arena(self, player_id):
        from .ranked_arena import view
        with closing(self.connect()) as connection:
            return view(self._player(connection, player_id), self.catalog, int(self.clock()))

    def get_seasonal_boost(self, player_id):
        from .seasonal_boost import view
        with closing(self.connect()) as connection:
            return view(self._player(connection, player_id), self.catalog, int(self.clock()))

    def get_blitz(self, player_id):
        from .blitz import view
        with closing(self.connect()) as connection:
            return view(self._player(connection, player_id), self.catalog, int(self.clock()))

    def get_dark_dimension(self, player_id, campaign_id=None):
        from .dark_dimension import view
        with closing(self.connect()) as connection:
            return view(self._player(connection, player_id), self.catalog, int(self.clock()), campaign_id)

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
            previous_level = player['level']
            previous_experience = player['experience']
            if any(field in self.catalog for field in ('objectives', 'calendars', 'alliances')):
                player['_transaction_activity'] = {'at': int(self.clock()), 'wallet_spent': {}, 'inventory_spent': {}, 'inventory_earned': {}}
            result = self._apply(connection, player, command, parameters)
            if command == 'raid.auto_launch' and result.get('skipped') == 'insufficient_funds':
                # The legacy native read/probe skipped unaffordable schedules
                # without consuming the daily cursor OR an idempotency key.
                # No player update or receipt is committed on this read-like
                # no-op, so a later refill can retry the very same slot/key.
                return result
            if 'level_rewards' in self.catalog or 'experience_thresholds' in self.catalog:
                from .player_progression import award, view as progression_view
                result['level_rewards'] = award(self, player, previous_level)
                result['progression'] = progression_view(player, self.catalog)
                result['levels_gained'] = list(range(previous_level + 1, player['level'] + 1))
                result['experience_change'] = {'previous_balance': previous_experience, 'balance': player['experience']}
                result.setdefault('server_time', self._command_time(player))
            if 'access_rules' in self.catalog:
                from .access import reconcile, view as access_view
                result['access_grants'] = reconcile(self, player)
                result['access'] = access_view(player, self.catalog['access_rules'])
            if 'alliances' in self.catalog:
                from .alliances import reconcile
                reconcile(self, player)
                result['alliance_state'] = self._alliance_view(player, self._command_time(player))
            if 'objectives' in self.catalog:
                from .objectives import record, view
                transaction = player.pop('_transaction_activity')
                now = transaction['at']
                record(player, self.catalog['objectives'], command, parameters, result, transaction, now)
                result['objective_state'] = view(player, self.catalog['objectives'], now)
                result.setdefault('server_time', now)
                if 'milestones' in self.catalog:
                    from .milestones import view as milestone_view
                    result['milestone_state'] = milestone_view(player, self.catalog, now)
            elif 'calendars' in self.catalog or 'alliances' in self.catalog:
                now = player.pop('_transaction_activity')['at']
                result.setdefault('server_time', now)
            if 'passes' in self.catalog:
                from .passes import reconcile, view as pass_view
                reconcile(player, self.catalog, now)
                result['pass_state'] = pass_view(player, self.catalog, now)
            if 'enhancements' in self.catalog or 'promotions' in self.catalog:
                result['augmentations'] = self._augmentation_view(player)
                result.setdefault('server_time', int(self.clock()))
            if 'resource_rules' in self.catalog:
                from .resources import view as resource_view
                result['resources'] = resource_view(player, self.catalog)
            if 'calendars' in self.catalog:
                from .calendars import view as calendar_view
                now = result.setdefault('server_time', int(self.clock()))
                result['calendar_state'] = calendar_view(player, self.catalog['calendars'], now)
            player['version'] += 1
            if command == 'loot.open':
                payment = result['payment']
                payment['balance'] = player['inventory'][payment['item_id']]
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
        if command == 'client_settings.patch':
            from .client_settings import apply
            return apply(player, self.catalog, parameters)
        if command in ('dark.start', 'dark.refresh', 'dark.begin', 'dark.complete', 'dark.heal'):
            from .dark_dimension import apply
            return apply(self, connection, player, command, parameters,
                         self._command_time(player), canonical)
        if command in ('blitz.refresh', 'blitz.recharge', 'blitz.begin', 'blitz.complete'):
            from .blitz import apply
            return apply(self, connection, player, command, parameters,
                         self._command_time(player), canonical)
        if command in ('seasonal.refresh', 'seasonal.claim', 'seasonal.buy'):
            from .seasonal_boost import apply
            return apply(self, player, command, parameters, self._command_time(player))
        if command in ('arena.refresh', 'arena.set_defense', 'arena.begin', 'arena.complete'):
            from .ranked_arena import apply
            return apply(self, player, command, parameters, self._command_time(player),
                         connection, canonical)
        if command == 'raid.auto_launch':
            from .raid_schedule import apply
            return apply(self, player, parameters, self._command_time(player))
        if command == 'raid.refresh':
            from .raid_settlement import refresh
            return refresh(self, player, parameters, self._command_time(player), awarded)
        if command == 'raid.settle':
            from .raid_settlement import apply
            return apply(self, player, parameters, self._command_time(player), awarded)
        if command in ('mail.read', 'mail.claim', 'mail.read_claim', 'mail.delete'):
            from .mail import apply
            return apply(self, player, command, parameters, self._command_time(player), awarded)
        if command == 'raid.heal':
            from .raid_healing import apply
            return apply(self, connection, player, parameters, self._command_time(player))
        if command == 'raid.refill':
            from .raid_energy import apply
            return apply(self, player, parameters, self._command_time(player))
        if command == 'loot.open':
            from .loot_products import apply
            return apply(self, player, parameters, awarded)
        if command in ('raid.begin', 'raid.complete', 'raid.checkpoint', 'raid.reset'):
            from .raid_combat import apply
            now = self._command_time(player)
            result = apply(self, connection, player, command, parameters, now, canonical, awarded)
            if command == 'raid.complete':
                from .raid_settlement import ready, settle
                raid = player['raids'][parameters['instance_id']]
                if ready(raid, self.catalog['raids'][raid['raid_id']], now):
                    result.update(settle(self, player, raid, now, awarded))
            return result
        if command.startswith('raid.'):
            from .raids import apply
            return apply(self, player, command, parameters, self._command_time(player))
        if command in ('alliance.donate', 'alliance.upgrade_boost', 'alliance.upgrade_category'):
            from .alliance_economy import apply
            return apply(self, player, command, parameters, awarded, self._command_time(player))
        if command.startswith('alliance.'):
            from .alliances import apply
            return apply(self, player, command, parameters, self._command_time(player))
        if command == 'calendar.advance':
            from .calendars import advance
            return advance(self, player, parameters, awarded, self._command_time(player))
        if command.startswith('pass.'):
            from .passes import apply
            return apply(self, player, command, parameters, awarded, self._command_time(player))
        if command == 'player.login':
            object_fields(parameters, ())
            if 'objectives' not in self.catalog:
                fail('feature_unavailable', 'Login tracking is not configured', 404)
            from .objectives import login
            return login(player, self._command_time(player))
        if command == 'objective.claim':
            from .objectives import claim
            return claim(self, player, parameters, awarded, self._command_time(player))
        if command.startswith('enhancement.'):
            from .enhancements import apply
            return apply(self, player, command, parameters)
        if command.startswith('promotion.'):
            from .promotions import apply
            return apply(self, player, command, parameters, awarded)
        if command in ('character.recruit', 'character.rank_up', 'character.train_items',
                       'character.ability_upgrade') or command.startswith('equipment.'):
            from .development import apply
            return apply(self, player, command, parameters)
        if command in ('appearance.select', 'guide.acknowledge', 'guide.event'):
            from .player_preferences import apply
            return apply(player, self.catalog, command, parameters)
        if command == 'squad.save':
            from .squads import save
            return save(player, self.catalog.get('squad_groups', {}), parameters, int(self.clock()))
        if command == 'free_crate.claim':
            object_fields(parameters, ())
            definition = self.catalog.get('free_crates')
            if definition is None:
                fail('feature_unavailable', 'No free-crate schedule is configured', 404)
            from .free_crates import claim
            crate, rewards, schedule = claim(player, definition, int(self.clock()))
            self._reward(player, rewards)
            return {'crate_id': crate, 'rewards': awarded((rewards, 1)), 'schedule': schedule}
        if command.startswith('roguelite.'):
            from .roguelite import apply
            return apply(self, connection, player, command, parameters,
                         self._command_time(player), canonical, awarded)
        if command.startswith('tower.'):
            from .towers import apply
            now = int(self.clock())
            result = apply(self, connection, player, command, parameters, now, canonical, awarded)
            result['server_time'] = now
            return result
        if command == 'character.train':
            object_fields(parameters, ('character_id', 'levels'))
            key, count = identifier(parameters['character_id']), integer(parameters['levels'], 1, 100)
            if key in self.catalog.get('development', {}).get('characters', {}):
                fail('material_training_required', 'This character requires training materials', 409)
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
            if definition.get('reset_policy') == 'utc_day_once':
                # Original Supplies slots permit one batch of 1–10 buys per
                # UTC day, not ten separate single-unit purchases. The
                # command receipt and the stock window share one transaction.
                day = int(self.clock()) // 86400
                used = player.get('offer_daily_purchases', {}).get(key)
                if count > definition['purchase_limit'] or (used and used['day'] == day):
                    fail('purchase_limit', 'Offer daily stock is exhausted', 409)
            elif player['offer_purchases'].get(key, 0) + count > definition['purchase_limit']:
                fail('purchase_limit', 'Offer purchase limit exceeded', 409)
            self._spend(player, definition['cost'], count)
            self._reward(player, definition['rewards'], count)
            if definition.get('reset_policy') == 'utc_day_once':
                player.setdefault('offer_daily_purchases', {})[key] = {'day': day, 'count': count}
            else:
                player['offer_purchases'][key] = player['offer_purchases'].get(key, 0) + count
            return {'offer_id': key, 'quantity': count, 'rewards': awarded((definition['rewards'], count))}
        if command == 'mission.begin':
            object_fields(parameters, ('mission_id', 'team'))
            mission_id = parameters['mission_id']
            mission = self._mission(player, mission_id)
            from .challenge_limits import require as require_challenge
            require_challenge(player, self.catalog, mission, 1, self._command_time(player))
            team = parameters['team']
            if not isinstance(team, list) or not 1 <= len(team) <= 5 or any(not isinstance(x, str) for x in team):
                fail('invalid_team', 'Team must contain 1–5 owned characters')
            if len(set(team)) != len(team) or any(x not in player['characters'] for x in team):
                fail('invalid_team', 'Team must contain distinct owned characters')
            from .mission_eligibility import require as require_mission_eligibility
            require_mission_eligibility(player, mission, team, self._command_time(player), mission_id)
            self._spend(player, mission['cost'])
            battle = {'battle_id': secrets.token_hex(16), 'mission_id': mission_id, 'team': team, 'status': 'active'}
            if 'alliance_economy' in self.catalog:
                battle['alliance_boosts'] = deepcopy(player.get('alliance_boosts', {}))
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
            if battle.get('mode') == 'roguelite':
                fail('battle_mode_conflict', 'Roguelite battles require roguelite.complete', 409)
            if battle.get('mode') == 'tower':
                fail('battle_mode_conflict', 'Tower battles require tower.complete', 409)
            if battle['mission_id'] != parameters['mission_id']:
                fail('battle_mission_conflict', 'Battle belongs to a different mission', 409)
            if battle['status'] != 'active':
                fail('battle_finished', 'Battle was already completed', 409)
            rewards = {}
            if outcome == 'victory':
                mission_id = battle['mission_id']
                mission = self.catalog['missions'][mission_id]
                from .challenge_limits import consume as consume_challenge
                consume_challenge(player, self.catalog, mission, 1, self._command_time(player))
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
            from .mission_eligibility import require_sweep
            require_sweep(player, definition, self._command_time(player), mission_id)
            from .challenge_limits import consume as consume_challenge
            consume_challenge(player, self.catalog, definition, count, self._command_time(player))
            self._spend(player, definition['cost'], count)
            self._reward(player, definition['rewards'], count)
            progress['wins'] += count
            return {'mission_id': mission_id, 'quantity': count, 'rewards': awarded((definition['rewards'], count))}
        fail('unsupported_command', 'Command is not part of API v1', 404)

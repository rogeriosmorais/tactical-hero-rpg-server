"""Independent normalized Dark Dimension runs, persistent damage and rewards.

Original Insanity mission data, native hero filters, battle sessions and
transcripts remain the private gateway's responsibility. The server stores
only normal resource, character, map/encounter and receipt identities.
"""
from copy import deepcopy
import json
import math
import secrets

from .catalog import fail, identifier, integer, object_fields, rewards, amounts


def _identifiers(values, maximum=1000):
    if (not isinstance(values, list) or len(values) > maximum or
            any(not isinstance(key, str) for key in values)):
        fail('invalid_catalog', 'Dark Dimension identifier list is invalid')
    for key in values:
        identifier(key)
    if len(set(values)) != len(values):
        fail('invalid_catalog', 'Dark Dimension identifiers must be distinct')


def _reward_references(definition, catalog):
    rewards(definition)
    for currency in definition.get('wallet', {}):
        if currency not in set(catalog['starting_wallet']) | set(catalog.get('resource_rules', {})):
            fail('invalid_catalog', 'Dark Dimension reward has undefined currency')


def validate(policy, catalog):
    object_fields(policy, ('required_level', 'health_caps', 'healing', 'campaigns'))
    integer(policy['required_level'], 1, catalog['maximum_player_level'])
    health = policy['health_caps']
    if not isinstance(health, dict) or set(health) != set(catalog['characters']):
        fail('invalid_catalog', 'Dark Dimension requires health caps for each configured character')
    for value in health.values():
        integer(value, 1, 2_000_000_000)
    healing = policy['healing']
    object_fields(healing, ('currency_id', 'premium_currency_id', 'premium_conversion',
                           'heal_costs', 'revive_costs'))
    allowed = set(catalog['starting_wallet']) | set(catalog.get('resource_rules', {}))
    for key in ('currency_id', 'premium_currency_id'):
        if identifier(healing[key]) not in allowed:
            fail('invalid_catalog', 'Dark Dimension healing currency is unavailable')
    if healing['currency_id'] == healing['premium_currency_id']:
        fail('invalid_catalog', 'Dark Dimension healing and premium resources must differ')
    rate = healing['premium_conversion']
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 1_000_000:
        fail('invalid_catalog', 'Dark Dimension premium conversion must be finite and positive')
    for category in ('heal_costs', 'revive_costs'):
        costs = healing[category]
        if not isinstance(costs, list) or not 1 <= len(costs) <= 100:
            fail('invalid_catalog', 'Dark Dimension healing cost ladder is required')
        for value in costs:
            integer(value, 1)
    campaigns = policy['campaigns']
    if not isinstance(campaigns, dict) or not 1 <= len(campaigns) <= 100:
        fail('invalid_catalog', 'Dark Dimension requires bounded original campaign definitions')
    for campaign_id, campaign in campaigns.items():
        identifier(campaign_id)
        object_fields(campaign, ('starting_room', 'rooms', 'nodes', 'completion_rewards',
                                 'completion_reward_tables'))
        rooms, nodes = campaign['rooms'], campaign['nodes']
        if not isinstance(rooms, dict) or not rooms or not isinstance(nodes, dict) or not nodes:
            fail('invalid_catalog', 'Dark Dimension campaigns require rooms and encounters')
        if identifier(campaign['starting_room']) not in rooms:
            fail('invalid_catalog', 'Dark Dimension starting room is unknown')
        assigned = set()
        for room_id, room in rooms.items():
            identifier(room_id)
            object_fields(room, ('next_rooms', 'node_ids'))
            _identifiers(room['next_rooms'])
            _identifiers(room['node_ids'], 10000)
            if set(room['next_rooms']) - set(rooms) or set(room['node_ids']) - set(nodes):
                fail('invalid_catalog', 'Dark Dimension room has unknown connectors or encounters')
            if assigned & set(room['node_ids']):
                fail('invalid_catalog', 'Dark Dimension encounter belongs to multiple rooms')
            assigned.update(room['node_ids'])
        if set(nodes) != assigned:
            fail('invalid_catalog', 'Dark Dimension every encounter must belong to one room')
        visited = {campaign['starting_room']}
        pending = [campaign['starting_room']]
        while pending:
            for next_room in rooms[pending.pop()]['next_rooms']:
                if next_room not in visited:
                    visited.add(next_room)
                    pending.append(next_room)
        if visited != set(rooms):
            fail('invalid_catalog', 'Dark Dimension contains unreachable rooms')
        for node_id, encounter in nodes.items():
            identifier(node_id)
            object_fields(encounter, ('maximum_team_size', 'required_character_ids', 'enemies',
                                      'rewards', 'first_run_rewards', 'reward_tables',
                                      'first_run_reward_tables'))
            integer(encounter['maximum_team_size'], 1, 5)
            _identifiers(encounter['required_character_ids'], 5)
            if set(encounter['required_character_ids']) - set(health):
                fail('invalid_catalog', 'Dark Dimension mandatory hero is undefined')
            enemies = encounter['enemies']
            if not isinstance(enemies, dict) or not 1 <= len(enemies) <= 1000:
                fail('invalid_catalog', 'Dark Dimension requires health for each enemy')
            for enemy_id, value in enemies.items():
                identifier(enemy_id)
                integer(value, 1)
            for key in ('rewards', 'first_run_rewards'):
                _reward_references(encounter[key], catalog)
            for key in ('reward_tables', 'first_run_reward_tables'):
                _identifiers(encounter[key], 50)
                if set(encounter[key]) - set(catalog.get('loot_tables', {})):
                    fail('invalid_catalog', 'Dark Dimension reward table is undefined')
        _reward_references(campaign['completion_rewards'], catalog)
        _identifiers(campaign['completion_reward_tables'], 50)
        if set(campaign['completion_reward_tables']) - set(catalog.get('loot_tables', {})):
            fail('invalid_catalog', 'Dark Dimension final reward table is undefined')


def _root(player):
    return player.setdefault('dark_dimension', {'runs': {}, 'history': {}})


def _run(player, campaign_id, run_id=None):
    run = player.get('dark_dimension', {}).get('runs', {}).get(campaign_id)
    if run is None or (run_id is not None and run['run_id'] != identifier(run_id)):
        fail('dark_run_not_found', 'Dark Dimension run does not belong to this player', 404)
    return run


def accessible(campaign, run):
    rooms = campaign['rooms']
    reached = {campaign['starting_room']}
    pending = [campaign['starting_room']]
    while pending:
        room_id = pending.pop()
        room = rooms[room_id]
        if not all(node in run['completed'] for node in room['node_ids']):
            continue
        for destination in room['next_rooms']:
            if destination not in reached:
                reached.add(destination)
                pending.append(destination)
    return reached


def _room(campaign, node_id):
    return next(name for name, room in campaign['rooms'].items() if node_id in room['node_ids'])


def _day_reset(run, policy, now):
    if run['completed_run'] or now // 86400 <= run['last_reset_at'] // 86400:
        return False
    for name, hero in run['heroes'].items():
        if hero['health'] < hero['maximum_health']:
            hero.update(health=hero['maximum_health'], energy=[0, -1, -1])
    run['last_reset_at'] = now
    return True


def _view_run(run, campaign, now):
    rooms = accessible(campaign, run)
    return {'run': deepcopy(run), 'accessible_rooms': sorted(rooms),
            'available_nodes': [node for room_id in sorted(rooms)
                                for node in campaign['rooms'][room_id]['node_ids']
                                if node not in run['completed']],
            'server_time': now}


def view(player, catalog, now, campaign_id=None):
    policy = catalog.get('dark_dimension')
    if policy is None:
        fail('feature_unavailable', 'Dark Dimension is not configured', 404)
    unlocked = player['level'] >= policy['required_level']
    runs = deepcopy(player.get('dark_dimension', {}).get('runs', {})) if unlocked else {}
    if campaign_id is not None:
        identifier(campaign_id)
        if campaign_id not in policy['campaigns']:
            fail('dark_campaign_not_found', 'Dark Dimension campaign is undefined', 404)
        run = runs.get(campaign_id)
        return {'available': unlocked, 'run': None if run is None else
                _view_run(run, policy['campaigns'][campaign_id], now),
                'needs_reset': run is not None and not run['completed_run'] and
                now // 86400 > run['last_reset_at'] // 86400,
                'server_time': now}
    return {'available': unlocked, 'runs': runs, 'history':
            deepcopy(player.get('dark_dimension', {}).get('history', {})) if unlocked else {},
            'server_time': now}


def _active_battle(connection, player):
    for raw, in connection.execute('SELECT data FROM battles WHERE player_id=?', (player['player_id'],)):
        battle = json.loads(raw)
        if battle['status'] == 'active':
            return battle
    return None


def _energy(values):
    if not isinstance(values, list) or len(values) != 3:
        fail('invalid_dark_energy', 'Dark Dimension requires three ability-energy slots')
    for value in values:
        integer(value, -1, 100000)
    return deepcopy(values)


def _reward(service, player, encounter, first, battle_id):
    from .loot import roll
    from .service import awarded
    bundles = [(encounter['rewards'], 1)]
    if first:
        bundles.append((encounter['first_run_rewards'], 1))
    tables = list(encounter['reward_tables'])
    if first:
        tables.extend(encounter['first_run_reward_tables'])
    rolls = []
    for table_id in tables:
        result = roll(service.catalog['loot_tables'], table_id, battle_id + ':' + table_id)
        bundles.append((result['rewards'], 1))
        rolls.append(result)
    bundle = awarded(*bundles)
    service._reward(player, bundle)
    return bundle, rolls


def apply(service, connection, player, command, parameters, now, canonical):
    policy = service.catalog.get('dark_dimension')
    if policy is None:
        fail('feature_unavailable', 'Dark Dimension is not configured', 404)
    if player['level'] < policy['required_level']:
        fail('dark_level_required', 'Dark Dimension account access has not been earned', 409)
    if command == 'dark.start':
        object_fields(parameters, ('campaign_id',))
    elif command == 'dark.refresh':
        object_fields(parameters, ('campaign_id', 'run_id'))
    elif command == 'dark.begin':
        object_fields(parameters, ('campaign_id', 'run_id', 'node_id', 'team'))
    elif command == 'dark.complete':
        object_fields(parameters, ('campaign_id', 'run_id', 'node_id', 'battle_id',
                                   'outcome', 'duration_seconds', 'allies', 'enemy_health'))
    elif command == 'dark.heal':
        object_fields(parameters, ('campaign_id', 'run_id', 'heroes'))
    else:
        fail('unknown_command', 'Dark Dimension command is undefined')
    campaign_id = identifier(parameters['campaign_id'])
    campaign = policy['campaigns'].get(campaign_id)
    if campaign is None:
        fail('dark_campaign_not_found', 'Dark Dimension campaign is undefined', 404)
    state = _root(player)
    if command == 'dark.start':
        run = state['runs'].get(campaign_id)
        if run is None or run['completed_run']:
            if _active_battle(connection, player):
                fail('battle_already_active', 'Active combat prevents starting another run', 409)
            number = state['history'].get(campaign_id, 0) + 1
            heroes = {name: {'health': policy['health_caps'][name],
                             'maximum_health': policy['health_caps'][name],
                             'energy': [0, -1, -1]}
                      for name in player['characters']}
            run = {'run_id': secrets.token_hex(12), 'campaign_id': campaign_id,
                   'number': number, 'started_at': now, 'last_reset_at': now,
                   'completed_run': False, 'completed': {}, 'enemy_states': {},
                   'wave_progress': {}, 'heroes': heroes, 'heal_counts': {},
                   'last_activity': {}}
            state['runs'][campaign_id] = run
            state['history'][campaign_id] = number
        return _view_run(run, campaign, now)
    run = _run(player, campaign_id, parameters['run_id'])
    if command == 'dark.refresh':
        changed = _day_reset(run, policy, now)
        result = _view_run(run, campaign, now)
        result['daily_restored'] = changed
        return result
    if run['completed_run']:
        fail('dark_run_complete', 'Dark Dimension run is already complete', 409)
    _day_reset(run, policy, now)
    if command == 'dark.heal':
        if _active_battle(connection, player):
            fail('dark_heal_active_battle', 'Cannot heal while combat is active', 409)
        requests = parameters['heroes']
        if not isinstance(requests, list) or not 1 <= len(requests) <= 5:
            fail('invalid_dark_heal', 'Dark Dimension heal requires one to five characters')
        seen, payments, changed = set(), {}, {}
        for request in requests:
            object_fields(request, ('character_id', 'action'), ('expected_cost',))
            name = identifier(request['character_id'])
            if name in seen or name not in run['heroes'] or name not in player['characters']:
                fail('dark_heal_character', 'Selected healing hero is not a distinct owned run hero', 409)
            seen.add(name)
            hero = run['heroes'][name]
            action = request['action']
            if action not in ('heal', 'revive') or (hero['health'] == 0) != (action == 'revive') or (
                    action == 'heal' and hero['health'] >= hero['maximum_health']):
                fail('dark_heal_state', 'Hero health does not match healing action', 409)
            levels = run['heal_counts'].setdefault(name, {})
            count = levels.get(action, 0)
            cost = policy['healing'][action + '_costs'][min(count, len(policy['healing'][action + '_costs']) - 1)]
            if 'expected_cost' in request and integer(request['expected_cost']) != cost:
                fail('dark_heal_price', 'Expected Dark Dimension healing cost does not match', 409)
            main, premium = (policy['healing'][k] for k in ('currency_id', 'premium_currency_id'))
            existing = payments.get(main, 0)
            currency = main if player['wallet'].get(main, 0) >= existing + cost else premium
            amount = cost if currency == main else math.ceil(cost * policy['healing']['premium_conversion'])
            payments[currency] = payments.get(currency, 0) + amount
            levels[action] = count + 1
            hero.update(health=hero['maximum_health'], energy=[0, -1, -1])
            changed[name] = deepcopy(hero)
        service._spend(player, payments)
        return {'run': _view_run(run, campaign, now), 'healed': changed,
                'payment': payments, 'server_time': now}
    node_id = identifier(parameters['node_id'])
    encounter = campaign['nodes'].get(node_id)
    if encounter is None:
        fail('dark_node_unknown', 'Dark Dimension node is undefined', 404)
    room = _room(campaign, node_id)
    if command == 'dark.begin':
        if node_id in run['completed'] or room not in accessible(campaign, run):
            fail('dark_node_inaccessible', 'Dark Dimension encounter is not available', 409)
        team = parameters['team']
        if not isinstance(team, list) or not 1 <= len(team) <= encounter['maximum_team_size']:
            fail('invalid_dark_team', 'Dark Dimension team size is invalid')
        for name in team:
            identifier(name)
        if (len(set(team)) != len(team) or any(name not in run['heroes']
                or name not in player['characters'] or run['heroes'][name]['health'] == 0
                for name in team) or
                not set(encounter['required_character_ids']) <= set(team)):
            fail('dark_team_ineligible', 'Dark Dimension requires available owned heroes', 409)
        if _active_battle(connection, player):
            fail('battle_already_active', 'Another combat session is still active', 409)
        enemy_state = deepcopy(run['enemy_states'].get(node_id, encounter['enemies']))
        battle = {'kind': 'dark_dimension', 'battle_id': secrets.token_hex(12), 'status': 'active',
                  'campaign_id': campaign_id, 'run_id': run['run_id'], 'node_id': node_id,
                  'team': deepcopy(team), 'character_states': {key: deepcopy(run['heroes'][key]) for key in team},
                  'enemies': enemy_state, 'encounter': deepcopy(encounter), 'started_at': now}
        connection.execute('INSERT INTO battles VALUES (?,?,?)',
                           (battle['battle_id'], player['player_id'], canonical(battle)))
        return {'battle': battle, 'run': _view_run(run, campaign, now), 'server_time': now}
    row = connection.execute('SELECT data FROM battles WHERE id=? AND player_id=?',
                             (identifier(parameters['battle_id']), player['player_id'])).fetchone()
    if row is None:
        fail('battle_not_found', 'Dark Dimension battle is not owned', 404)
    battle = json.loads(row[0])
    if (battle.get('kind') != 'dark_dimension' or battle['status'] != 'active' or
            any(battle[key] != parameters[key] for key in ('campaign_id', 'run_id', 'node_id'))):
        fail('dark_battle_conflict', 'Dark Dimension battle context is not active', 409)
    outcome = parameters['outcome']
    if outcome not in ('victory', 'defeat'):
        fail('invalid_outcome', 'Dark Dimension adjudicated outcome is undefined')
    integer(parameters['duration_seconds'], 0, 86400)
    allies, enemies = parameters['allies'], parameters['enemy_health']
    if not isinstance(allies, dict) or set(allies) != set(battle['team']) or (
            not isinstance(enemies, dict) or set(enemies) != set(battle['enemies'])):
        fail('dark_result_roster', 'Dark Dimension result identities differ from the session')
    for name, hero in allies.items():
        object_fields(hero, ('health', 'energy'))
        integer(hero['health'], 0, battle['character_states'][name]['maximum_health'])
        _energy(hero['energy'])
    for name, hp in enemies.items():
        integer(hp, 0, encounter['enemies'][name])
    surviving = sum(hero['health'] > 0 for hero in allies.values())
    if (outcome == 'victory' and (not surviving or any(enemies.values()))) or (
            outcome == 'defeat' and surviving and not any(enemies.values())):
        fail('dark_inconsistent_outcome', 'Dark Dimension outcome does not match surviving actors', 409)
    for name, hero in allies.items():
        run['heroes'][name].update(deepcopy(hero))
    run['enemy_states'][node_id] = deepcopy(enemies)
    max_health = sum(encounter['enemies'].values())
    run['wave_progress'][node_id] = 1 if outcome == 'victory' else (
        max(0, min(1, 1 - sum(enemies.values()) / max_health)))
    reward, rolls, completed = {}, [], False
    if outcome == 'victory':
        if node_id in run['completed']:
            fail('dark_node_complete', 'Dark Dimension node is already complete', 409)
        reward, rolls = _reward(service, player, encounter, run['number'] == 1, battle['battle_id'])
        run['completed'][node_id] = battle['battle_id']
        run['last_activity'] = {'node_id': node_id, 'room_id': room}
        if len(run['completed']) == len(campaign['nodes']):
            ending = {'rewards': campaign['completion_rewards'],
                      'first_run_rewards': {}, 'reward_tables': campaign['completion_reward_tables'],
                      'first_run_reward_tables': []}
            from .service import awarded
            final, extra = _reward(service, player, ending, run['number'] == 1,
                                   battle['battle_id'] + ':final') if run['number'] == 1 else (awarded(), [])
            reward = awarded((reward, 1), (final, 1))
            rolls.extend(extra)
            run['completed_run'] = completed = True
    battle.update(status='complete', outcome=outcome, stars=0 if outcome == 'defeat' else
                  max(1, 3 - (len(battle['team']) - surviving)), completed_at=now)
    connection.execute('UPDATE battles SET data=? WHERE id=?',
                       (canonical(battle), battle['battle_id']))
    return {'battle': battle, 'run': _view_run(run, campaign, now), 'rewards': reward,
            'loot_rolls': rolls, 'completed_run': completed, 'server_time': now}

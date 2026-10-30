"""Alliance-owned raid instances, participation, topology and selection locks."""
from copy import deepcopy
import secrets

from .alliances import current
from .catalog import amounts, fail, identifier, integer, object_fields


def identifiers(values, maximum=10000):
    if not isinstance(values, list) or len(values) > maximum:
        fail('invalid_catalog', 'Raid references must be a bounded list')
    for key in values:
        identifier(key)
    if len(set(values)) != len(values):
        fail('invalid_catalog', 'Raid references must be distinct')


def validate(definitions, catalog):
    if not isinstance(definitions, dict) or 'alliances' not in catalog:
        fail('invalid_catalog', 'Raids require alliance policy and explicit definitions')
    for key, rule in definitions.items():
        identifier(key)
        object_fields(rule, ('required_level', 'duration_seconds', 'join_cooldown_seconds', 'lock_seconds',
            'team_count', 'launch_cost', 'starting_room_ids', 'rooms', 'nodes', 'difficulties',
            'maximum_launch_difficulty', 'nonsequential_difficulties'), ('combat', 'healing', 'settlement'))
        integer(rule['required_level'], catalog['alliances']['required_level'], catalog['maximum_player_level'])
        integer(rule['duration_seconds'], 1, 31536000)
        integer(rule['join_cooldown_seconds'], 0, 31536000)
        integer(rule['lock_seconds'], 1, 86400)
        integer(rule['team_count'], 1, 100)
        amounts(rule['launch_cost'])
        if type(rule['nonsequential_difficulties']) is not bool:
            fail('invalid_catalog', 'Raid difficulty policy must be boolean')
        levels = rule['difficulties']
        if not isinstance(levels, list) or not 1 <= len(levels) <= 1000 or levels[0] is not None:
            fail('invalid_catalog', 'Raid base difficulty must have no prerequisite')
        integer(rule['maximum_launch_difficulty'], 0, len(levels) - 1)
        for index, requirement in enumerate(levels[1:], 1):
            object_fields(requirement, ('difficulty', 'completion_percent'))
            integer(requirement['difficulty'], 0, index - 1)
            integer(requirement['completion_percent'], 0, 100)
        for field in ('rooms', 'nodes'):
            if not isinstance(rule[field], dict) or not rule[field] or len(rule[field]) > 10000:
                fail('invalid_catalog', 'Raid maps require bounded rooms and nodes')
            for item in rule[field]:
                identifier(item)
        identifiers(rule['starting_room_ids'])
        if not rule['starting_room_ids'] or not set(rule['starting_room_ids']) <= set(rule['rooms']):
            fail('invalid_catalog', 'Raid starting rooms must exist')
        assigned = []
        for room in rule['rooms'].values():
            object_fields(room, ('next_room_ids', 'node_ids'))
            for field, collection in (('next_room_ids', rule['rooms']), ('node_ids', rule['nodes'])):
                identifiers(room[field])
                if not set(room[field]) <= set(collection):
                    fail('invalid_catalog', 'Raid map reference does not exist')
            assigned.extend(room['node_ids'])
        if len(assigned) != len(set(assigned)) or set(assigned) != set(rule['nodes']):
            fail('invalid_catalog', 'Every raid node must belong to exactly one room')
        for node in rule['nodes'].values():
            object_fields(node, ('encounter_id',))
            identifier(node['encounter_id'])
        reachable, pending = set(), list(rule['starting_room_ids'])
        while pending:
            room = pending.pop()
            if room not in reachable:
                reachable.add(room)
                pending.extend(rule['rooms'][room]['next_room_ids'])
        if reachable != set(rule['rooms']):
            fail('invalid_catalog', 'Every raid room must be reachable from a starting room')
        if 'combat' in rule:
            from .raid_combat import validate as validate_combat
            validate_combat(rule['combat'], rule, catalog)
        if 'healing' in rule:
            from .raid_healing import validate as validate_healing
            validate_healing(rule['healing'], catalog)
        if 'settlement' in rule:
            from .raid_settlement import validate as validate_settlement
            validate_settlement(rule['settlement'], rule, catalog)


def accessible_rooms(rule, raid, team):
    cleared = raid.get('completed', {}).get(str(team), {})
    reached, pending = set(), list(rule['starting_room_ids'])
    while pending:
        key = pending.pop()
        if key in reached:
            continue
        reached.add(key)
        room = rule['rooms'][key]
        if all(node in cleared for node in room['node_ids']):
            pending.extend(room['next_room_ids'])
    return reached


def launch_difficulty(player, rule, alliance_id, raid_id, difficulty):
    integer(difficulty, 0, len(rule['difficulties']) - 1)
    if difficulty > rule['maximum_launch_difficulty']:
        fail('raid_difficulty_limit', 'Raid launch difficulty exceeds its cap', 409)
    if not difficulty or rule['nonsequential_difficulties']:
        return difficulty
    requirement = rule['difficulties'][difficulty]
    for raid in player.get('raids', {}).values():
        if (raid['raid_id'] == raid_id and raid['alliance_id'] == alliance_id and raid['status'] == 'ended'
                and raid.get('settlement') and (raid['difficulty'] > requirement['difficulty'] or
                (raid['difficulty'] == requirement['difficulty'] and
                 raid['settlement']['completion_percent'] >= requirement['completion_percent']))):
            return difficulty
    fail('raid_difficulty_locked', 'Prior completed raid difficulty is required', 409)


def instance(player, instance_id, now, *, active=True):
    raid = player.get('raids', {}).get(identifier(instance_id))
    if raid is None or raid['alliance_id'] != player.get('alliance_id'):
        fail('raid_not_found', 'Raid instance is not in the current alliance', 404)
    if active and (raid['status'] != 'active' or now >= raid['expires_at']):
        fail('raid_ended', 'Raid instance has ended', 409)
    return raid


def progress(player, alliance, raid_id):
    ended = [row for row in player.get('raids', {}).values() if row['raid_id'] == raid_id and
        row['alliance_id'] == alliance['alliance_id'] and row['status'] == 'ended' and row.get('settlement')]
    best = max(ended, key=lambda row: (row['difficulty'], row['settlement']['completion_percent']), default=None)
    return {'difficulty': best['difficulty'] if best else 0,
            'completion_percent': best['settlement']['completion_percent'] if best else 0,
            'selected_difficulty': alliance.get('raid_difficulties', {}).get(raid_id, 0)}


def project(player, rule, raid, now):
    result = deepcopy(raid)
    result['available'] = raid['status'] == 'active' and now < raid['expires_at']
    result['teams'] = {}
    for team in range(rule['team_count']):
        reached = accessible_rooms(rule, raid, team)
        completed = raid['completed'].get(str(team), {})
        rooms = {}
        for key, room in rule['rooms'].items():
            nodes = {}
            for node in room['node_ids']:
                lock = raid['locks'].get(str(team), {}).get(node)
                nodes[node] = {'completed': deepcopy(completed.get(node)),
                    'lock': deepcopy(lock) if lock and now < lock['expires_at'] else None,
                    'encounter_id': rule['nodes'][node]['encounter_id']}
                attempt = raid.get('node_states', {}).get(str(team), {}).get(node)
                if attempt:
                    maximums = rule['combat']['nodes'][node][attempt['difficulty']]['enemies']
                    nodes[node].update(difficulty=attempt['difficulty'], enemy_health=deepcopy(attempt['enemy_health']),
                        damage=raid.get('node_damage', {}).get(str(team), {}).get(node, 0),
                        progress=1 if node in completed else max(0, min(1, 1 - sum(attempt['enemy_health'].values()) / sum(maximums.values()))))
            rooms[key] = {'accessible': key in reached, 'completed': bool(nodes) and all(
                row['completed'] is not None for row in nodes.values()), 'nodes': nodes}
        result['teams'][str(team)] = {'rooms': rooms, 'completed_nodes': len(completed),
            'members': [player['player_id']] if raid['team_index'] == team else []}
    # Expired locks are omitted from the projection without rewriting receipts or state.
    result['locks'] = {team: {node: deepcopy(lock) for node, lock in entries.items() if now < lock['expires_at']}
                       for team, entries in raid['locks'].items()}
    return result


def view(player, catalog, now, instance_id=None):
    definitions = catalog.get('raids', {})
    if instance_id is not None:
        raid = instance(player, instance_id, now, active=False)
        return {'raid': project(player, definitions[raid['raid_id']], raid, now), 'server_time': now}
    # Closure eligibility is an application rule, not a native gateway
    # decision. Include due instances across previous alliances too: the
    # public raid.refresh transaction settles all owned eligible instances.
    from .raid_settlement import ready
    pending_settlement_ids = sorted(
        key for key, raid in player.get('raids', {}).items()
        if raid['raid_id'] in definitions and ready(raid, definitions[raid['raid_id']], now))
    rows = [project(player, definitions[raid['raid_id']], raid, now) for raid in player.get('raids', {}).values()
            if raid['alliance_id'] == player.get('alliance_id') and raid['raid_id'] in definitions]
    return {'raids': rows, 'server_time': now, 'pending_settlement_ids': pending_settlement_ids}


def apply(service, player, command, parameters, now):
    fields = {'raid.launch': ('raid_id',), 'raid.join': ('raid_id', 'instance_id', 'team_index'),
              'raid.lock': ('raid_id', 'instance_id', 'team_index', 'node_id'),
              'raid.release': ('raid_id', 'instance_id', 'team_index', 'node_id'),
              'raid.set_difficulty': ('raid_id', 'difficulty')}
    if command not in fields:
        fail('unsupported_command', 'Raid action is undefined', 404)
    object_fields(parameters, fields[command], ('difficulty',) if command == 'raid.launch' else ())
    raid_id = identifier(parameters['raid_id'])
    rule = service.catalog.get('raids', {}).get(raid_id)
    if rule is None:
        fail('raid_definition_not_found', 'Raid definition is unavailable', 404)
    alliance = current(player)
    if alliance is None or player['level'] < rule['required_level']:
        fail('raid_access_required', 'Raid level and current alliance are required', 409)
    if command in ('raid.launch', 'raid.set_difficulty'):
        if player.get('alliance_rank') not in ('owner', 'officer', alliance['settings'].get('raid_rank', 'member')):
            fail('raid_launch_permission', 'Alliance rank cannot launch raids', 409)
        difficulty = launch_difficulty(player, rule, alliance['alliance_id'], raid_id, parameters.get('difficulty', 0))
        if command == 'raid.set_difficulty':
            alliance.setdefault('raid_difficulties', {})[raid_id] = difficulty
            if alliance.get('raid_schedule'):
                for entry in alliance['raid_schedule']['entries'].values():
                    if entry['raid_id'] == raid_id:
                        entry['difficulty'] = difficulty
            return {'raid_id': raid_id, 'difficulty': difficulty, 'server_time': now,
                    'progress': progress(player, alliance, raid_id)}
        if any(row['raid_id'] == raid_id and row['alliance_id'] == alliance['alliance_id'] and
               row['status'] == 'active' and row['expires_at'] > now for row in player.get('raids', {}).values()):
            fail('raid_already_active', 'This raid is already active in the current alliance', 409)
        service._spend(player, rule['launch_cost'])
        raid = {'instance_id': secrets.token_hex(12), 'raid_id': raid_id, 'creator_id': player['player_id'],
            'alliance_id': alliance['alliance_id'], 'started_at': now, 'expires_at': now + rule['duration_seconds'],
            'status': 'active', 'difficulty': difficulty, 'team_index': None, 'characters': {},
            'completed': {}, 'locks': {}, 'damage': {}}
        player.setdefault('raids', {})[raid['instance_id']] = raid
    else:
        raid = instance(player, parameters['instance_id'], now)
        if raid['raid_id'] != raid_id:
            fail('raid_context_conflict', 'Raid definition does not match the instance', 409)
        team = integer(parameters['team_index'], 0, rule['team_count'] - 1)
        if command == 'raid.join':
            if now < alliance['joined_at'] + rule['join_cooldown_seconds']:
                fail('raid_join_cooldown', 'Alliance joining cooldown prevents raid participation', 409)
            if raid['team_index'] is not None and raid['team_index'] != team:
                fail('raid_team_selected', 'Raid strike team is already selected', 409)
            if raid['team_index'] is None:
                raid['team_index'] = team
                raid['characters'] = deepcopy(player['characters'])
        else:
            if raid['team_index'] != team:
                fail('raid_team_required', 'Joined raid strike team is required', 409)
            node = identifier(parameters['node_id'])
            room_id = next((key for key, row in rule['rooms'].items() if node in row['node_ids']), None)
            if room_id is None:
                fail('raid_node_not_found', 'Raid node does not exist', 404)
            locks = raid['locks'].setdefault(str(team), {})
            lock = locks.get(node)
            if lock and lock['player_id'] != player['player_id'] and (command == 'raid.release' or now < lock['expires_at']):
                fail('raid_node_locked', 'Raid node is locked by another member', 409)
            if command == 'raid.lock':
                if room_id not in accessible_rooms(rule, raid, team):
                    fail('raid_room_locked', 'Raid room is not yet reachable', 409)
                if node in raid['completed'].get(str(team), {}):
                    fail('raid_node_complete', 'Raid node is already complete', 409)
                locks[node] = {'player_id': player['player_id'], 'display_name': player['display_name'],
                               'locked_at': now, 'expires_at': min(now + rule['lock_seconds'], raid['expires_at'])}
            else:
                locks.pop(node, None)
    return {'raid': project(player, rule, raid, now), 'server_time': now}

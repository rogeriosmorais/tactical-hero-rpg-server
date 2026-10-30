"""Raid attempts and normalized adjudicated results on the application ledger."""
from copy import deepcopy
import json
import secrets

from .catalog import amounts, fail, identifier, integer, object_fields, rewards
from .raids import accessible_rooms, instance, project


FIELDS = {'level', 'rank', 'tier', 'promotion_rank', 'enhancement_level'}


def validate(combat, rule, catalog):
    object_fields(combat, ('energy_caps', 'nodes'))
    if not isinstance(combat['energy_caps'], dict) or not combat['energy_caps']:
        fail('invalid_catalog', 'Raid combat requires character ability energy limits')
    for key, caps in combat['energy_caps'].items():
        if key not in catalog['characters'] or not isinstance(caps, list) or len(caps) != 3:
            fail('invalid_catalog', 'Raid energy limits require three defined character abilities')
        for levels in caps:
            if type(levels) is int:
                integer(levels)
                continue
            if not isinstance(levels, list) or not 1 <= len(levels) <= 100:
                fail('invalid_catalog', 'Raid ability energy levels must be bounded')
            for cap in levels:
                integer(cap)
    if not isinstance(combat['nodes'], dict) or set(combat['nodes']) != set(rule['nodes']):
        fail('invalid_catalog', 'Raid combat must cover every node')
    for encounters in combat['nodes'].values():
        if not isinstance(encounters, list) or len(encounters) != len(rule['difficulties']):
            fail('invalid_catalog', 'Raid combat must cover every difficulty')
        for encounter in encounters:
            object_fields(encounter, ('encounter_id', 'cost', 'maximum_team_size', 'gates',
                'required_character_ids', 'enemies', 'rewards', 'contribution_points', 'time_limit_seconds'), ('initial_enemy_health', 'reward_tables', 'contribution_counter_id'))
            identifier(encounter['encounter_id'])
            amounts(encounter['cost'])
            integer(encounter['maximum_team_size'], 1, 5)
            from .raids import identifiers
            identifiers(encounter['required_character_ids'], 5)
            if not set(encounter['required_character_ids']) <= set(combat['energy_caps']):
                fail('invalid_catalog', 'Mandatory raid characters must have energy limits')
            enemies = encounter['enemies']
            if not isinstance(enemies, dict) or not 1 <= len(enemies) <= 10000:
                fail('invalid_catalog', 'Raid encounters require bounded enemy health definitions')
            for key, maximum in enemies.items():
                identifier(key)
                integer(maximum, 1)
            if 'initial_enemy_health' in encounter:
                initial = encounter['initial_enemy_health']
                if not isinstance(initial, dict) or set(initial) != set(enemies):
                    fail('invalid_catalog', 'Initial raid enemy health must cover the encounter')
                for key, value in initial.items():
                    integer(value, 0, enemies[key])
            rewards(encounter['rewards'])
            references = encounter.get('reward_tables', [])
            if not isinstance(references, list) or len(references) > 100:
                fail('invalid_catalog', 'Raid reward references must be bounded')
            allowed = {identifier(encounter['contribution_counter_id'])} if 'contribution_counter_id' in encounter else set()
            for reference in references:
                if identifier(reference) not in catalog.get('loot_tables', {}):
                    fail('invalid_catalog', 'Raid reward table is not configured')
                for choices in catalog['loot_tables'][reference]['groups']:
                    for choice in choices:
                        for item in choice['items']:
                            if choice['weight'] and item['weight'] and set(item.get('counters', {})) - allowed:
                                fail('invalid_catalog', 'Raid loot counter has no declared application owner')
            integer(encounter['contribution_points'])
            if encounter['time_limit_seconds'] is not None:
                integer(encounter['time_limit_seconds'], 1, 86400)
            gates = encounter['gates']
            if not isinstance(gates, list) or len(gates) > 100:
                fail('invalid_catalog', 'Raid gates must be a bounded list')
            for gate in gates:
                object_fields(gate, ('minimum_team_size', 'characters'))
                integer(gate['minimum_team_size'], 0, 5)
                if not isinstance(gate['characters'], dict):
                    fail('invalid_catalog', 'Raid gates require explicit character alternatives')
                for key, clauses in gate['characters'].items():
                    if key not in combat['energy_caps'] or not isinstance(clauses, list) or not 1 <= len(clauses) <= 100:
                        fail('invalid_catalog', 'Raid gates require known characters and bounded alternatives')
                    for clause in clauses:
                        object_fields(clause, (), FIELDS)
                        for value in clause.values():
                            integer(value)


def character_values(character, catalog):
    values = {field: character.get(field, 1 if field in ('level', 'rank', 'tier') else 0) for field in FIELDS}
    state = character.get('enhancement', {})
    rules = catalog.get('enhancements')
    if rules and state:
        from .enhancements import quality
        selected = rules['classes'].get(state.get('class_id'), {}).get('skills', [])
        learned = state.get('learned', {}).get(state.get('class_id'), [])
        values['enhancement_level'] = quality(state, rules) or max((i for i, row in enumerate(selected, 1)
            if row['skill_id'] in learned), default=0)
    return values


def checkpoint(value, enemies):
    object_fields(value, ('enemy_health', 'progress'))
    health = value['enemy_health']
    if health is not None:
        if not isinstance(health, dict) or set(health) != set(enemies):
            fail('invalid_checkpoint', 'Checkpoint enemy identities must match the encounter')
        for key, hp in health.items():
            integer(hp, 0, enemies[key])
    progress = value['progress']
    if type(progress) not in (int, float) or not 0 <= progress <= 1:
        fail('invalid_checkpoint', 'Checkpoint progress must be a finite fraction')
    return deepcopy(value)


def context(service, player, parameters, now):
    raid = instance(player, parameters['instance_id'], now)
    raid_id, node = identifier(parameters['raid_id']), identifier(parameters['node_id'])
    if raid['raid_id'] != raid_id:
        fail('raid_context_conflict', 'Raid definition does not match this instance', 409)
    rule = service.catalog['raids'][raid_id]
    team = integer(parameters['team_index'], 0, rule['team_count'] - 1)
    if raid['team_index'] != team:
        fail('raid_team_required', 'Joined raid strike team is required', 409)
    if player['level'] < rule['required_level']:
        fail('raid_access_required', 'Raid account level is required', 409)
    combat = rule.get('combat')
    if combat is None or node not in combat['nodes']:
        fail('raid_combat_unavailable', 'Raid combat is not configured', 404)
    return raid, rule, combat, str(team), node


def load_battle(connection, player, key):
    row = connection.execute('SELECT data FROM battles WHERE id=? AND player_id=?',
                             (identifier(key), player['player_id'])).fetchone()
    if row is None:
        fail('battle_not_found', 'Battle does not belong to this player', 404)
    battle = json.loads(row[0])
    if battle.get('kind') != 'raid' or battle['status'] != 'active':
        fail('battle_not_active', 'Active raid battle is required', 409)
    return battle


def apply(service, connection, player, command, parameters, now, canonical, awarded):
    shared = ('raid_id', 'instance_id', 'team_index', 'node_id')
    fields = {'raid.begin': shared + ('team',),
        'raid.complete': shared + ('battle_id', 'outcome', 'duration_seconds', 'allies', 'enemy_health'),
        'raid.checkpoint': shared + ('battle_id', 'checkpoint'), 'raid.reset': shared}
    object_fields(parameters, fields[command], ('difficulty', 'formation', 'checkpoint') if command == 'raid.begin' else ())
    raid, rule, combat, team, node = context(service, player, parameters, now)
    room = next(key for key, value in rule['rooms'].items() if node in value['node_ids'])
    if command == 'raid.reset':
        return reset(service, connection, player, parameters, raid, rule, team, room, now, canonical)
    if command == 'raid.begin':
        difficulty = integer(parameters.get('difficulty', raid['difficulty']), 0, len(rule['difficulties']) - 1)
        encounter = combat['nodes'][node][difficulty]
        prior = raid.get('node_states', {}).get(team, {}).get(node)
        if prior is not None and prior['difficulty'] != difficulty:
            fail('raid_difficulty_reset_required', 'Saved node progress requires reset before changing difficulty', 409)
        if node in raid['completed'].get(team, {}):
            fail('raid_node_complete', 'Raid node is already complete', 409)
        if room not in accessible_rooms(rule, raid, team):
            fail('raid_room_locked', 'Raid room is not reachable', 409)
        lock = raid['locks'].get(team, {}).get(node)
        if not lock or lock['player_id'] != player['player_id'] or now >= lock['expires_at']:
            fail('raid_lock_required', 'An unexpired owned selection lock is required', 409)
        selected = parameters['team']
        if not isinstance(selected, list) or not 1 <= len(selected) <= encounter['maximum_team_size']:
            fail('invalid_team', 'Raid team size is invalid')
        for key in selected:
            identifier(key)
        if len(set(selected)) != len(selected) or any(key not in raid['characters'] or key not in combat['energy_caps'] for key in selected):
            fail('invalid_team', 'Raid team must contain distinct owned joined characters')
        if not set(encounter['required_character_ids']) <= set(selected):
            fail('raid_mandatory_character', 'Mandatory raid characters are required', 409)
        formation = parameters.get('formation', list(range(len(selected))))
        if not isinstance(formation, list) or len(formation) != len(selected):
            fail('invalid_formation', 'Each character requires a formation slot')
        for slot in formation:
            integer(slot, 0, 9)
        if len(set(formation)) != len(formation):
            fail('invalid_formation', 'Raid formation slots must be distinct')
        health = raid.get('character_states', {})
        for key in selected:
            if health.get(key, {}).get('health', 1) == 0:
                fail('raid_character_dead', 'A defeated raid character must be revived', 409)
            values = character_values(raid['characters'][key], service.catalog)
            for gate in encounter['gates']:
                clauses = gate['characters'].get(key, [])
                if len(selected) < gate['minimum_team_size'] or not any(all(values[field] >= minimum for field, minimum in clause.items()) for clause in clauses):
                    fail('raid_team_restriction', 'Raid roster does not meet the encounter requirements', 409)
        caps = {}
        for key in selected:
            levels = raid['characters'][key].get('abilities', [1, 1, 1])
            if len(levels) < 3 or any(isinstance(entries, list) and (levels[i] or 1) > len(entries)
                                    for i, entries in enumerate(combat['energy_caps'][key])):
                fail('raid_ability_unavailable', 'Raid ability energy definition does not cover this owned level', 409)
            caps[key] = [entries[max(0, (levels[i] or 1) - 1)] if isinstance(entries, list) else entries
                         for i, entries in enumerate(combat['energy_caps'][key])]
        pending = []
        for row in connection.execute('SELECT data FROM battles WHERE player_id=?', (player['player_id'],)):
            saved = json.loads(row[0])
            if saved['status'] == 'active':
                pending.append(saved)
        if any(battle.get('kind') != 'raid' for battle in pending):
            fail('battle_already_active', 'Another combat mode is already active', 409)
        state = {'difficulty': difficulty, 'enemy_health': deepcopy(prior['enemy_health'] if prior else
            encounter.get('initial_enemy_health', encounter['enemies']))}
        if prior and prior.get('settled_attempt'):
            state['settled_attempt'] = True
        initial_checkpoint = checkpoint(parameters.get('checkpoint', {'enemy_health': None, 'progress': 0}), encounter['enemies'])
        service._spend(player, encounter['cost'])
        for old in pending:
            old.update(status='abandoned', abandon_reason='replaced_by_raid_begin')
            connection.execute('UPDATE battles SET data=? WHERE id=?', (canonical(old), old['battle_id']))
            previous = player['raids'].get(old['instance_id'])
            if previous and (old['instance_id'], old['team_index'], old['node_id']) != (raid['instance_id'], int(team), node):
                old_locks = previous['locks'].get(str(old['team_index']), {})
                if old_locks.get(old['node_id'], {}).get('player_id') == player['player_id']:
                    old_locks.pop(old['node_id'], None)
        battle = {'kind': 'raid', 'battle_id': secrets.token_hex(12), 'raid_id': raid['raid_id'],
            'instance_id': raid['instance_id'], 'team_index': int(team), 'node_id': node, 'difficulty': difficulty,
            'encounter_id': encounter['encounter_id'], 'status': 'active', 'team': deepcopy(selected), 'formation': deepcopy(formation),
            'team_state': {key: deepcopy(raid['characters'][key]) for key in selected},
            'character_states': {key: deepcopy(health[key]) for key in selected if key in health},
            'alliance_boosts': deepcopy(player.get('alliance_boosts', {})), 'enemy_health': state['enemy_health'],
            'energy_caps': caps, 'checkpoint': initial_checkpoint, 'combat_rules': deepcopy(encounter), 'started_at': now}
        raid.setdefault('node_states', {}).setdefault(team, {})[node] = state
        connection.execute('INSERT INTO battles VALUES (?,?,?)', (battle['battle_id'], player['player_id'], canonical(battle)))
        return {'battle': battle, 'raid': project(player, rule, raid, now), 'server_time': now}
    battle = load_battle(connection, player, parameters['battle_id'])
    if any(battle[field] != parameters[field] for field in shared):
        fail('raid_context_conflict', 'Raid context differs from the issued battle', 409)
    if command == 'raid.checkpoint':
        if checkpoint(parameters['checkpoint'], battle['combat_rules']['enemies']) != battle['checkpoint']:
            fail('raid_checkpoint_conflict', 'Checkpoint differs from the issued begin state', 409)
        return {'battle_id': battle['battle_id'], 'server_time': now}
    outcome = parameters['outcome']
    if outcome not in ('victory', 'defeat'):
        fail('invalid_outcome', 'Raid outcome is undefined')
    duration = integer(parameters['duration_seconds'])
    limit = battle['combat_rules']['time_limit_seconds']
    if outcome == 'victory' and limit is not None and duration > limit:
        fail('raid_time_limit', 'Raid victory exceeds the encounter timer', 409)
    allies, enemies = parameters['allies'], parameters['enemy_health']
    if not isinstance(allies, dict) or set(allies) != set(battle['team']) or not isinstance(enemies, dict) or set(enemies) != set(battle['enemy_health']):
        fail('invalid_combat_roster', 'Result identities must match the issued battle')
    for key, state in allies.items():
        object_fields(state, ('health', 'maximum_health', 'energy'))
        maximum = integer(state['maximum_health'], 1)
        integer(state['health'], 0, maximum)
        energy = state['energy']
        if not isinstance(energy, list) or len(energy) != 3:
            fail('invalid_ability_energy', 'Three ability energy values are required')
        for i, value in enumerate(energy):
            integer(value, -1, battle['energy_caps'][key][i])
    for key, health in enemies.items():
        integer(health, 0, battle['combat_rules']['enemies'][key])
    alive = sum(row['health'] > 0 for row in allies.values())
    if ((outcome == 'victory' and (not alive or any(enemies.values()))) or
            (outcome == 'defeat' and alive and not any(enemies.values()))):
        fail('inconsistent_outcome', 'Raid outcome conflicts with surviving actors')
    damage = integer(sum(max(0, old - enemies[key]) for key, old in battle['enemy_health'].items()))
    raid.setdefault('character_states', {}).update(deepcopy(allies))
    raid['node_states'][team][node]['enemy_health'] = deepcopy(enemies)
    raid['node_states'][team][node]['settled_attempt'] = True
    node_damage = raid.setdefault('node_damage', {}).setdefault(team, {})
    node_damage[node] = integer(node_damage.get(node, 0) + damage)
    raid['damage'][team] = integer(raid['damage'].get(team, 0) + damage)
    stars = max(1, 3 - (len(battle['team']) - alive)) if outcome == 'victory' else 0
    bundle = awarded((battle['combat_rules']['rewards'], 1)) if stars else awarded()
    loot_rolls = []
    points = battle['combat_rules']['contribution_points'] if stars else 0
    if stars:
        from .loot import roll
        for table_id in battle['combat_rules'].get('reward_tables', []):
            loot_rolls.append(roll(service.catalog['loot_tables'], table_id, battle['battle_id'] + ':' + table_id))
        bundle = awarded((bundle, 1), *((row['rewards'], 1) for row in loot_rolls))
        points = integer(points + sum(row['counters'].get(battle['combat_rules'].get('contribution_counter_id'), 0) for row in loot_rolls))
        service._reward(player, bundle)
        raid.setdefault('completed', {}).setdefault(team, {})[node] = {'stars': stars, 'battle_id': battle['battle_id']}
        raid['contribution_points'] = integer(raid.get('contribution_points', 0) + points)
    raid['locks'].get(team, {}).pop(node, None)
    battle.update(status='complete', outcome=outcome, stars=stars)
    connection.execute('UPDATE battles SET data=? WHERE id=?', (canonical(battle), battle['battle_id']))
    return {'battle': battle, 'raid': project(player, rule, raid, now), 'rewards': bundle,
            'loot_rolls': loot_rolls, 'damage': damage,
            'contribution_points': points, 'server_time': now}


def reset(service, connection, player, parameters, raid, rule, team, room, now, canonical):
    nodes = rule['rooms'][room]['node_ids']
    if room not in accessible_rooms(rule, raid, team) or any(node in raid['completed'].get(team, {}) for node in nodes):
        fail('raid_reset_unavailable', 'Raid reset requires an accessible incomplete room', 409)
    for row in connection.execute('SELECT data FROM battles WHERE player_id=?', (player['player_id'],)):
        battle = json.loads(row[0])
        if battle['status'] == 'active' and battle.get('instance_id') == raid['instance_id'] and battle.get('team_index') == int(team):
            fail('raid_reset_active_battle', 'Active raid combat prevents room reset', 409)
    states = raid.get('node_states', {}).get(team, {})
    if not any(states.get(node, {}).get('settled_attempt') for node in nodes):
        fail('raid_reset_no_attempt', 'Raid room has no saved attempt', 409)
    for node in nodes:
        lock = raid['locks'].get(team, {}).get(node)
        if lock and lock['player_id'] != player['player_id'] and now < lock['expires_at']:
            fail('raid_node_locked', 'Raid room has a foreign selection lock', 409)
    damage = raid.setdefault('node_damage', {}).setdefault(team, {})
    removed = sum(damage.get(node, 0) for node in nodes)
    raid['damage'][team] = integer(raid['damage'].get(team, 0) - removed)
    for node in nodes:
        states.pop(node, None)
        damage[node] = 0
        raid['locks'].get(team, {}).pop(node, None)
    return {'raid': project(player, rule, raid, now), 'reset_node_ids': deepcopy(nodes), 'server_time': now}

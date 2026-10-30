"""Authoritative timed tower runs, progression, cooldowns and rewards.

All mutations participate in GameService's command transaction and receipts.
No client protocol, original content, private paths or sibling imports.
"""
from copy import deepcopy
import json
import secrets

from .catalog import fail, identifier, integer, object_fields


def definition(service, tower_id, now):
    tower = service.catalog.get('towers', {}).get(identifier(tower_id))
    if tower is None:
        fail('tower_not_found', 'Tower does not exist', 404)
    if not tower['starts_at'] <= now < tower['ends_at']:
        fail('tower_inactive', 'Tower is outside its availability window', 409)
    return tower


def state(player, tower_id):
    return player.setdefault('towers', {}).setdefault(tower_id, {
        'active_run': None, 'last_run': None, 'high_score': 0, 'highest_floor': 0,
        'cooldowns': {}, 'claimed_milestones': [], 'rewarded_floors': []})


def active_run(progress, run_id):
    run = progress['active_run']
    if not run or run['run_id'] != identifier(run_id) or run['status'] != 'active':
        fail('tower_run_inactive', 'Requested tower run is not active', 409)
    return run


def view(service, player, tower_id, now):
    tower = service.catalog.get('towers', {}).get(identifier(tower_id))
    if tower is None:
        fail('tower_not_found', 'Tower does not exist', 404)
    progress = deepcopy(state(player, tower_id))
    progress['cooldowns'] = {key: until for key, until in progress['cooldowns'].items() if until > now}
    return {'tower_id': tower_id, 'server_time': now,
            'available': tower['starts_at'] <= now < tower['ends_at'], 'tower': progress}


def finish_battle(service, connection, player, parameters, now, canonical, awarded):
    object_fields(parameters, ('battle_id', 'outcome', 'stars'))
    battle_id = identifier(parameters['battle_id'])
    outcome, stars = parameters['outcome'], integer(parameters['stars'], 0, 3)
    if outcome not in ('victory', 'defeat', 'retreat') or (outcome == 'victory') != (stars > 0):
        fail('invalid_outcome', 'Outcome and star count disagree')
    row = connection.execute('SELECT data FROM battles WHERE id=? AND player_id=?',
                             (battle_id, player['player_id'])).fetchone()
    if row is None:
        fail('battle_not_found', 'Battle does not belong to this player', 404)
    battle = json.loads(row[0])
    if battle.get('mode') != 'tower':
        fail('battle_mode_conflict', 'Battle is not a tower encounter', 409)
    if battle['status'] != 'active':
        fail('battle_finished', 'Battle was already completed', 409)
    # A battle issued inside the window can settle after its end. Fresh starts,
    # begins and claims still require an active event. Exact retries use receipts.
    tower = service.catalog['towers'][battle['tower_id']]
    progress = state(player, battle['tower_id'])
    run = active_run(progress, battle['run_id'])
    if run['battle_id'] != battle_id or run['next_floor'] != battle['floor_index']:
        fail('tower_battle_conflict', 'Battle does not match current tower progress', 409)
    floor = tower['difficulties'][run['difficulty_id']][battle['floor_index']]
    prizes = awarded()
    if outcome == 'victory':
        reward_key = run['difficulty_id'] + ':' + str(battle['floor_index'])
        if reward_key not in progress['rewarded_floors']:
            prizes = awarded((floor['rewards'], 1))
            service._reward(player, prizes)
            progress['rewarded_floors'].append(reward_key)
        run['score'] = integer(run['score'] + floor['score'])
        run['next_floor'] += 1
        progress['high_score'] = max(progress['high_score'], run['score'])
        progress['highest_floor'] = max(progress['highest_floor'], run['next_floor'])
        period = (now - tower['starts_at']) // tower['cooldown_seconds']
        until = tower['starts_at'] + (period + 1) * tower['cooldown_seconds']
        progress['cooldowns'] = {key: expiry for key, expiry in progress['cooldowns'].items() if expiry > now}
        progress['cooldowns'].update({key: until for key in battle['team']})
        if floor['reset_cooldowns']:
            progress['cooldowns'] = {}
        if run['next_floor'] == len(tower['difficulties'][run['difficulty_id']]):
            run['status'] = 'complete'
    run['battle_id'] = None
    battle.update(status='complete', outcome=outcome, stars=stars)
    connection.execute('UPDATE battles SET data=? WHERE id=?', (canonical(battle), battle_id))
    return {'tower_id': battle['tower_id'], 'tower': deepcopy(progress), 'battle': battle, 'rewards': prizes}


def apply(service, connection, player, command, parameters, now, canonical, awarded):
    if command == 'tower.complete':
        return finish_battle(service, connection, player, parameters, now, canonical, awarded)
    fields = {
        'tower.start': ('tower_id', 'difficulty_id'),
        'tower.end': ('tower_id', 'run_id'),
        'tower.begin': ('tower_id', 'run_id', 'floor_index', 'encounter_id', 'team'),
        'tower.claim': ('tower_id', 'milestone_id'),
    }
    if command not in fields:
        fail('unsupported_command', 'Command is not part of API v1', 404)
    object_fields(parameters, fields[command])
    tower_id = identifier(parameters['tower_id'])
    tower = definition(service, tower_id, now)
    progress = state(player, tower_id)
    if command == 'tower.start':
        difficulty = identifier(parameters['difficulty_id'])
        if difficulty not in tower['difficulties']:
            fail('tower_difficulty_not_found', 'Tower difficulty does not exist', 404)
        # Deliberately no player level check: all difficulties are accessible
        # at account level 1, subject to the authored roster requirements.
        run = progress['active_run']
        if run:
            if run['difficulty_id'] != difficulty:
                fail('tower_run_exists', 'End the current run before changing difficulty', 409)
        else:
            run = {'run_id': secrets.token_hex(16), 'difficulty_id': difficulty,
                   'next_floor': 0, 'score': 0, 'status': 'active', 'battle_id': None, 'started_at': now}
            progress['active_run'] = run
    elif command == 'tower.end':
        run = progress['active_run']
        if not run or run['run_id'] != identifier(parameters['run_id']):
            fail('tower_run_inactive', 'Requested tower run does not exist', 409)
        if run['battle_id'] is not None:
            fail('tower_battle_active', 'Settle the issued battle before ending the run', 409)
        progress['last_run'] = deepcopy(run)
        progress['last_run']['status'] = 'ended'
        progress['active_run'] = None
    elif command == 'tower.begin':
        run = active_run(progress, parameters['run_id'])
        index = integer(parameters['floor_index'], 0, 999)
        if index != run['next_floor']:
            fail('tower_floor_locked', 'Floor does not match current progression', 409)
        floor = tower['difficulties'][run['difficulty_id']][index]
        if identifier(parameters['encounter_id']) != floor['encounter_id']:
            fail('tower_encounter_conflict', 'Encounter does not match the selected difficulty and floor', 409)
        if now < tower['starts_at'] + floor['unlock_offset']:
            fail('tower_floor_locked', 'Floor is not yet available', 409)
        if run['battle_id'] is not None:
            fail('tower_battle_active', 'A battle is already active in this run', 409)
        team = parameters['team']
        if (not isinstance(team, list) or len(team) != tower['team_size'] or
                any(not isinstance(x, str) or x not in player['characters'] for x in team) or
                len(set(team)) != len(team)):
            fail('invalid_team', 'Team must contain the required number of distinct owned characters')
        if any(progress['cooldowns'].get(key, 0) > now for key in team):
            fail('tower_hero_cooldown', 'A selected character is on cooldown', 409)
        if 'eligible_characters' in floor and any(key not in floor['eligible_characters'] for key in team):
            fail('tower_team_restricted', 'Team does not meet floor eligibility', 409)
        service._spend(player, floor['cost'])
        battle = {'battle_id': secrets.token_hex(16), 'mode': 'tower', 'tower_id': tower_id,
                  'run_id': run['run_id'], 'difficulty_id': run['difficulty_id'],
                  'floor_index': index, 'encounter_id': floor['encounter_id'],
                  'team': deepcopy(team), 'status': 'active'}
        if 'alliance_economy' in service.catalog:
            battle['alliance_boosts'] = deepcopy(player.get('alliance_boosts', {}))
        run['battle_id'] = battle['battle_id']
        connection.execute('INSERT INTO battles VALUES (?,?,?)',
                           (battle['battle_id'], player['player_id'], canonical(battle)))
        return {'tower_id': tower_id, 'tower': deepcopy(progress), 'battle': battle}
    else:
        milestone_id = identifier(parameters['milestone_id'])
        milestone = next((m for m in tower['milestones'] if m['milestone_id'] == milestone_id), None)
        if milestone is None:
            fail('tower_milestone_not_found', 'Tower milestone does not exist', 404)
        if milestone_id in progress['claimed_milestones']:
            fail('tower_milestone_claimed', 'Tower milestone was already claimed', 409)
        if progress['high_score'] < milestone['points']:
            fail('tower_milestone_locked', 'Tower score has not reached this milestone', 409)
        prizes = awarded((milestone['rewards'], 1))
        service._reward(player, prizes)
        progress['claimed_milestones'].append(milestone_id)
        return {'tower_id': tower_id, 'tower': deepcopy(progress), 'rewards': prizes}
    return {'tower_id': tower_id, 'tower': deepcopy(progress)}

"""Server-owned local ranked Arena state, daily payouts and neutral candidates.

This is the retained local offline ladder policy, not publisher matchmaking.
Native hero builds, combat sessions and transcript validation are private and
must be integrated before arena battle commands can be enabled.
"""
from copy import deepcopy
import hashlib
import json
import secrets

from .catalog import fail, identifier, integer, object_fields, rewards


def validate(policy, catalog):
    object_fields(policy, ('required_level', 'maximum_rank', 'attacks_per_day', 'payout_ranges'))
    integer(policy['required_level'], 1, catalog['maximum_player_level'])
    integer(policy['maximum_rank'], 1, 100000)
    integer(policy['attacks_per_day'], 1, 1000)
    ranges = policy['payout_ranges']
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 100:
        fail('invalid_catalog', 'Ranked Arena requires bounded sourced payout ranges')
    expected = 1
    for row in ranges:
        object_fields(row, ('min_rank', 'max_rank', 'tier', 'rewards'))
        if integer(row['min_rank'], 1, policy['maximum_rank']) != expected:
            fail('invalid_catalog', 'Ranked Arena ranges must partition every rank exactly once')
        maximum = integer(row['max_rank'], expected, policy['maximum_rank'])
        integer(row['tier'], 0, 100000)
        rewards(row['rewards'])
        for currency in row['rewards'].get('wallet', {}):
            if currency not in catalog['starting_wallet'] and currency not in catalog.get('resource_rules', {}):
                fail('invalid_catalog', 'Ranked Arena payout uses an undefined currency')
        expected = maximum + 1
    if expected != policy['maximum_rank'] + 1:
        fail('invalid_catalog', 'Ranked Arena payout bands must cover the final rank')


def _new(player, policy, now):
    today = now // 86400
    roster = player['characters']
    return {'rank': policy['maximum_rank'], 'attacks': policy['attacks_per_day'],
            'day': today, 'last_payout_day': today,
            'defense': sorted(roster)[:5], 'victories': 0, 'defeats': 0,
            'opponents': [], 'nonce': 0, 'history': [], 'payout_history': []}


def _candidates(player, state, policy):
    names = sorted(player['characters'])
    if not names:
        fail('arena_empty_roster', 'Ranked Arena needs owned characters', 409)
    defense = [name for name in state['defense'] if name in player['characters']] or names[:5]
    base = sum(player['characters'][name]['power'] for name in defense)
    if base <= 0:
        fail('arena_empty_defense', 'Ranked Arena needs a powered defense', 409)
    result = []
    maximum = policy['maximum_rank']
    for index, part in enumerate((4, 10, 20)):
        target = max(1, state['rank'] - max(1, round(state['rank'] * part / 100)))
        if any(row['rank'] == target for row in result):
            target = max(1, target - index)
        seed = int(hashlib.sha256(f"{state['day']}:{state['nonce']}:{target}:{index}".encode()).hexdigest()[:13], 16)
        rotated = names[seed % len(names):] + names[:seed % len(names)]
        selected = rotated[:5]
        progression = (maximum - target) / max(1, maximum - 1)
        # Relative target power follows the retained local difficulty curve.
        ratio = 0.64 + 0.53 * progression + 0.09 * index
        result.append({'opponent_id': str(800000000 + seed % 100000000), 'rank': target,
                       'team': selected, 'target_power': max(1, round(base * ratio))})
    return result


def _daily(service, player, state, now):
    policy = service.catalog['ranked_arena']
    today = now // 86400
    paid = []
    if today <= state['day']:
        return paid
    if today > state['last_payout_day']:
        row = next((r for r in policy['payout_ranges'] if r['min_rank'] <= state['rank'] <= r['max_rank']), None)
        if row is None:
            fail('arena_payout_range_missing', 'Ranked Arena payout band is undefined')
        service._reward(player, row['rewards'])
        paid = [{'day': today, 'rank': state['rank'], 'tier': row['tier'],
                 'rewards': deepcopy(row['rewards'])}]
        state['payout_history'].extend(paid)
        state['payout_history'] = state['payout_history'][-40:]
        state['last_payout_day'] = today
    state.update(day=today, attacks=policy['attacks_per_day'], opponents=[], nonce=state['nonce'] + 1)
    return paid


def view(player, catalog, now):
    policy = catalog.get('ranked_arena')
    if policy is None:
        fail('feature_unavailable', 'Ranked Arena is not configured', 404)
    unlocked = player['level'] >= policy['required_level']
    state = player.get('ranked_arena') if unlocked else None
    return {'available': unlocked, 'needs_refresh': unlocked and
            (state is None or now // 86400 > state['day'] or not state['opponents']),
            'arena': deepcopy(state), 'day': now // 86400, 'server_time': now}


def apply(service, player, command, parameters, now, connection=None, canonical=None):
    policy = service.catalog.get('ranked_arena')
    if policy is None:
        fail('feature_unavailable', 'Ranked Arena is not configured', 404)
    if player['level'] < policy['required_level']:
        fail('arena_level_required', 'Ranked Arena level has not been earned', 409)
    if command == 'arena.refresh':
        object_fields(parameters, ())
    elif command == 'arena.set_defense':
        object_fields(parameters, ('team',))
        team = parameters['team']
        if not isinstance(team, list) or not 1 <= len(team) <= 5:
            fail('invalid_arena_defense', 'Arena defense needs 1–5 owned characters')
        for key in team:
            identifier(key)
        if len(set(team)) != len(team):
            fail('invalid_arena_defense', 'Arena defense characters must be unique')
        if not set(team) <= set(player['characters']):
            fail('arena_character_not_owned', 'Arena defense must use owned characters', 409)
    elif command == 'arena.begin':
        object_fields(parameters, ('team', 'opponent_id'))
        team = parameters['team']
        if not isinstance(team, list) or not 1 <= len(team) <= 5:
            fail('invalid_arena_team', 'Ranked Arena requires one to five owned characters')
        for key in team:
            identifier(key)
        if len(set(team)) != len(team) or not set(team) <= set(player['characters']):
            fail('arena_team_not_owned', 'Ranked Arena combat needs distinct owned heroes', 409)
        opponent_id = identifier(parameters['opponent_id'])
    elif command == 'arena.complete':
        object_fields(parameters, ('battle_id', 'outcome', 'stars', 'duration_seconds'))
        if parameters['outcome'] not in ('victory', 'defeat'):
            fail('invalid_outcome', 'Ranked Arena result must be adjudicated')
        stars = integer(parameters['stars'], 0, 3)
        if (parameters['outcome'] == 'victory' and stars == 0 or
                parameters['outcome'] == 'defeat' and stars != 0):
            fail('arena_stars_conflict', 'Ranked Arena stars conflict with victory', 409)
        integer(parameters['duration_seconds'], 0, 86400)
        if connection is None or canonical is None:
            fail('arena_session_unavailable', 'Arena combat needs an active application transaction', 409)
    else:
        fail('unsupported_command', 'Ranked Arena command is undefined', 404)
    state = player.setdefault('ranked_arena', _new(player, policy, now))
    payout = _daily(service, player, state, now)
    if command == 'arena.set_defense':
        state.update(defense=deepcopy(team), opponents=[], nonce=state['nonce'] + 1)
    if command == 'arena.begin':
        if connection is None or canonical is None:
            fail('arena_session_unavailable', 'Arena combat needs an active application transaction', 409)
        if state['attacks'] <= 0:
            fail('arena_attacks_exhausted', 'No ranked attacks remain today', 409)
        if not state['opponents']:
            fail('arena_matchup_missing', 'Ranked opponent selection must be refreshed first', 409)
        opponent = next((row for row in state['opponents'] if row['opponent_id'] == opponent_id), None)
        if opponent is None:
            fail('arena_opponent_unknown', 'Opponent was not issued by this account', 409)
        for row in connection.execute('SELECT data FROM battles WHERE player_id=?', (player['player_id'],)):
            if json.loads(row[0]).get('status') == 'active':
                fail('battle_already_active', 'Another game battle is still active', 409)
        battle = {'kind': 'ranked_arena', 'battle_id': secrets.token_hex(12),
                  'status': 'active', 'team': deepcopy(team),
                  'team_state': {key: deepcopy(player['characters'][key]) for key in team},
                  'opponent': deepcopy(opponent), 'started_at': now,
                  'rank_at_start': state['rank'], 'day_at_start': state['day']}
        connection.execute('INSERT INTO battles VALUES (?,?,?)',
                           (battle['battle_id'], player['player_id'], canonical(battle)))
        state['attacks'] -= 1
        state['history'].append({'battle_id': battle['battle_id'],
                                 'opponent_id': opponent_id, 'target_rank': opponent['rank'],
                                 'status': 'started'})
        state['history'] = state['history'][-100:]
        return {'battle': battle, 'arena': deepcopy(state),
                'payouts': payout, 'server_time': now}
    if command == 'arena.complete':
        row = connection.execute('SELECT data FROM battles WHERE id=? AND player_id=?',
                                 (identifier(parameters['battle_id']), player['player_id'])).fetchone()
        if row is None:
            fail('battle_not_found', 'Ranked Arena battle is not owned', 404)
        battle = json.loads(row[0])
        if battle.get('kind') != 'ranked_arena' or battle.get('status') != 'active':
            fail('arena_battle_not_active', 'Only active owned Ranked Arena battles can settle', 409)
        old_rank = state['rank']
        won = parameters['outcome'] == 'victory'
        if won:
            state['rank'] = min(old_rank, battle['opponent']['rank'])
            state['victories'] += 1
        else:
            state['defeats'] += 1
        for record in reversed(state['history']):
            if record['battle_id'] == battle['battle_id']:
                record.update(status='won' if won else 'lost',
                              rank_before=old_rank, rank_after=state['rank'])
                break
        state['nonce'] += 1
        state['opponents'] = []
        battle.update(status='complete', outcome=parameters['outcome'],
                      stars=stars, completed_at=now)
        connection.execute('UPDATE battles SET data=? WHERE id=?',
                           (canonical(battle), battle['battle_id']))
        return {'battle_id': battle['battle_id'], 'old_rank': old_rank,
                'new_rank': state['rank'], 'outcome': parameters['outcome'],
                'stars': stars, 'arena': deepcopy(state),
                'payouts': payout, 'server_time': now}
    if not state['opponents']:
        state['opponents'] = _candidates(player, state, policy)
    return {'arena': deepcopy(state), 'payouts': payout, 'server_time': now}

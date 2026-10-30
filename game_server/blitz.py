"""Server-owned local Blitz ladder, matchmaking identities and battle receipts.

Source-original native hero builds and combat transcript checks remain private.
This module only accepts normalized identifiers, trusted adjudicated results,
immutable application rules, and transactions in the public player ledger.
"""
from copy import deepcopy
import hashlib
import json
import secrets

from .catalog import fail, identifier, integer, object_fields, rewards


def window(event, now):
    if 'period_seconds' not in event:
        return event['starts_at'], event['ends_at']
    period, anchor = event['period_seconds'], event['anchor']
    start = anchor + ((now - anchor) // period) * period
    return start, start + period


def occurrence(event_id, event, now):
    """Current durable event instance, independent of changing catalog hashes."""
    start, _ = window(event, now)
    return event_id + ':' + str(start) if 'period_seconds' in event else event_id


def validate(policy, catalog):
    object_fields(policy, ('required_level', 'maximum_tier', 'matchup_expiry_seconds',
                           'recharge', 'events'))
    integer(policy['required_level'], 1, catalog['maximum_player_level'])
    integer(policy['maximum_tier'], 1, 1000)
    integer(policy['matchup_expiry_seconds'], 1, 86400)
    recharge = policy['recharge']
    object_fields(recharge, ('charges_currency_id', 'premium_currency_id', 'cost_per_character'))
    allowed = set(catalog['starting_wallet']) | set(catalog.get('resource_rules', {}))
    if (identifier(recharge['charges_currency_id']) not in allowed or
            identifier(recharge['premium_currency_id']) not in allowed or
            recharge['charges_currency_id'] == recharge['premium_currency_id']):
        fail('invalid_catalog', 'Blitz recharges require distinct configured currencies')
    integer(recharge['cost_per_character'], 1, 10000)
    events = policy['events']
    if not isinstance(events, dict) or not 1 <= len(events) <= 500:
        fail('invalid_catalog', 'Blitz requires bounded source-declared events')
    for key, event in events.items():
        identifier(key)
        object_fields(event, ('cooldown_seconds', 'win_points', 'lose_points',
                              'win_rewards', 'lose_rewards'),
                      ('starts_at', 'ends_at', 'period_seconds', 'anchor'))
        if 'period_seconds' in event:
            if 'starts_at' in event or 'ends_at' in event or 'anchor' not in event:
                fail('invalid_catalog', 'Recurring Blitz event needs only a period and anchor')
            integer(event['period_seconds'], 1, 31557600)
            integer(event['anchor'])
        elif ('starts_at' not in event or 'ends_at' not in event or
                'anchor' in event or integer(event['ends_at']) <= integer(event['starts_at'])):
            fail('invalid_catalog', 'Blitz event window is invalid')
        integer(event['cooldown_seconds'], 0, 86400)
        integer(event['win_points'])
        integer(event['lose_points'])
        for result in ('win_rewards', 'lose_rewards'):
            rewards(event[result])
            for currency in event[result].get('wallet', {}):
                if currency not in allowed:
                    fail('invalid_catalog', 'Blitz awards undefined currency')


def _require(service, player, key, now):
    policy = service.catalog.get('blitz')
    if policy is None:
        fail('feature_unavailable', 'Blitz mode is not configured', 404)
    if player['level'] < policy['required_level']:
        fail('blitz_level_required', 'Blitz commander level has not been earned', 409)
    event = policy['events'].get(identifier(key))
    if event is None:
        fail('blitz_event_not_found', 'Blitz event is unknown', 404)
    start, end = window(event, now)
    if not start <= now < end:
        fail('blitz_event_inactive', 'Blitz event is not active', 409)
    return policy, event, occurrence(key, event, now)


def _state(player):
    return player.setdefault('blitz', {'events': {}, 'matchups': {}, 'cooldowns': {}})


def _team(player, selected):
    if not isinstance(selected, list) or not 1 <= len(selected) <= 5:
        fail('invalid_blitz_team', 'Blitz requires one to five characters')
    for item in selected:
        identifier(item)
    if len(set(selected)) != len(selected) or any(item not in player['characters'] for item in selected):
        fail('blitz_team_not_owned', 'Blitz team must contain distinct owned characters', 409)
    return selected


def _tier(state, event_id):
    return state['events'].get(event_id, {}).get('tier', 1)


def _signature(player, state, event_id, team):
    snapshot = {'team': team, 'characters': [player['characters'][name] for name in team],
                'level': player['level'], 'tier': _tier(state, event_id)}
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _candidates(player, state, event_id, team):
    power = sum(integer(player['characters'][key]['power']) for key in team)
    if power == 0:
        fail('blitz_empty_power', 'Blitz team power must be positive', 409)
    # A neutral target-power curve. This is NOT a native build or a fake
    # source stat; the private renderer must synthesize a verified legal
    # opponent build before enabling the actual native combat route.
    tier = _tier(state, event_id)
    level = max(14, min(60, player['level']))
    result = []
    for index in range(3):
        ratio = 0.67 + 0.07 * index + 0.12 * (level - 14) / 46 + 0.38 * (tier - 1) / max(1, 7)
        key = f'{event_id}:{index}:{state.get("nonce", 0)}'
        uid = str(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16))
        result.append({'opponent_id': uid, 'team': deepcopy(team),
                       'target_power': max(1, round(power * ratio))})
    return result


def view(player, catalog, now):
    policy = catalog.get('blitz')
    if policy is None:
        fail('feature_unavailable', 'Blitz is not configured', 404)
    return {'available': player['level'] >= policy['required_level'],
            'events': {key: {'active': window(event, now)[0] <= now < window(event, now)[1],
                             'occurrence_id': occurrence(key, event, now),
                             'starts_at': window(event, now)[0],
                             'ends_at': window(event, now)[1]}
                       for key, event in policy['events'].items()},
            'state': deepcopy(player.get('blitz')), 'server_time': now}


def _battle(connection, player, battle_id):
    row = connection.execute('SELECT data FROM battles WHERE id=? AND player_id=?',
                             (identifier(battle_id), player['player_id'])).fetchone()
    if row is None:
        fail('battle_not_found', 'This Blitz battle is not owned', 404)
    record = json.loads(row[0])
    if record.get('kind') != 'blitz' or record.get('status') != 'active':
        fail('blitz_battle_not_active', 'Active Blitz battle required', 409)
    return record


def apply(service, connection, player, command, parameters, now, canonical):
    policy = service.catalog.get('blitz')
    if policy is None:
        fail('feature_unavailable', 'Blitz is not configured', 404)
    if player['level'] < policy['required_level']:
        fail('blitz_level_required', 'Blitz level has not been earned', 409)
    state = _state(player)
    if command == 'blitz.refresh':
        object_fields(parameters, ('event_id', 'team'), ('force',))
        event_id = identifier(parameters['event_id'])
        _, event, current = _require(service, player, event_id, now)
        team = _team(player, parameters['team'])
        if type(parameters.get('force', False)) is not bool:
            fail('invalid_blitz_refresh', 'Refresh switch must be boolean')
        previous = state['matchups'].get(current)
        signature = _signature(player, state, current, team)
        if (previous is None or now >= previous['expires_at'] or
                previous['signature'] != signature or parameters.get('force', False)):
            state['nonce'] = state.get('nonce', 0) + 1
            previous = {'team': deepcopy(team), 'signature': signature,
                        'expires_at': now + policy['matchup_expiry_seconds'],
                        'occurrence_id': current,
                        'opponents': _candidates(player, state, current, team)}
            state['matchups'][current] = previous
        return {'matchup': deepcopy(previous), 'blitz': deepcopy(state), 'server_time': now}

    if command == 'blitz.recharge':
        object_fields(parameters, ('characters',), ('expected_cost',))
        names = _team(player, parameters['characters'])
        cooling = [name for name in names if state['cooldowns'].get(name, 0) > now]
        if not cooling:
            fail('blitz_no_cooldown', 'No selected Blitz characters need recharge', 409)
        cost = len(cooling) * policy['recharge']['cost_per_character']
        if 'expected_cost' in parameters and integer(parameters['expected_cost']) != cost:
            fail('blitz_cost_conflict', 'Blitz charge cost differs from current cooldown', 409)
        charges, premium = (policy['recharge'][key] for key in
                            ('charges_currency_id', 'premium_currency_id'))
        paid_with = charges if player['wallet'].get(charges, 0) >= cost else premium
        service._spend(player, {paid_with: cost})
        for name in cooling:
            state['cooldowns'].pop(name, None)
        return {'recharged': cooling, 'cost': cost, 'currency_id': paid_with,
                'blitz': deepcopy(state), 'server_time': now}

    if command == 'blitz.begin':
        object_fields(parameters, ('event_id', 'team', 'opponent_id'))
        event_id = identifier(parameters['event_id'])
        _, event, current = _require(service, player, event_id, now)
        team = _team(player, parameters['team'])
        opponent_id = identifier(parameters['opponent_id'])
        match = state['matchups'].get(current)
        if (match is None or match['expires_at'] <= now or
                match['signature'] != _signature(player, state, current, team) or
                team != match['team']):
            fail('blitz_matchup_expired', 'A current saved Blitz matchup is required', 409)
        opponent = next((row for row in match['opponents'] if row['opponent_id'] == opponent_id), None)
        if opponent is None:
            fail('blitz_opponent_unknown', 'Opponent was not issued in this matchup', 409)
        if any(state['cooldowns'].get(name, 0) > now for name in team):
            fail('blitz_character_cooling', 'A selected character is cooling down', 409)
        for row in connection.execute('SELECT data FROM battles WHERE player_id=?', (player['player_id'],)):
            old = json.loads(row[0])
            if old.get('status') == 'active':
                fail('battle_already_active', 'Another battle is active', 409)
        battle = {'kind': 'blitz', 'battle_id': secrets.token_hex(12), 'status': 'active',
                  'event_id': event_id, 'occurrence_id': current,
                  'team': deepcopy(team), 'opponent': deepcopy(opponent),
                  'team_state': {key: deepcopy(player['characters'][key]) for key in team},
                  'tier': _tier(state, event_id), 'rules': deepcopy(event),
                  'started_at': now}
        connection.execute('INSERT INTO battles VALUES (?,?,?)',
                           (battle['battle_id'], player['player_id'], canonical(battle)))
        for name in team:
            state['cooldowns'][name] = now + event['cooldown_seconds']
        return {'battle': battle, 'blitz': deepcopy(state), 'server_time': now}

    if command == 'blitz.complete':
        object_fields(parameters, ('battle_id', 'outcome', 'stars', 'duration_seconds'))
        battle = _battle(connection, player, parameters['battle_id'])
        outcome = parameters['outcome']
        if outcome not in ('victory', 'defeat'):
            fail('invalid_outcome', 'Blitz battle outcome must be adjudicated')
        stars = integer(parameters['stars'], 0, 3)
        if (outcome == 'victory' and stars == 0) or (outcome == 'defeat' and stars != 0):
            fail('blitz_stars_conflict', 'Blitz stars must match adjudicated outcome', 409)
        integer(parameters['duration_seconds'], 0, 86400)
        event_id, event = battle['occurrence_id'], battle['rules']
        earned = event['win_rewards'] if outcome == 'victory' else event['lose_rewards']
        service._reward(player, earned)
        progress = state['events'].setdefault(event_id, {'points': 0, 'wins': 0,
                                                         'battles': 0, 'streak': 0, 'tier': 1})
        progress['battles'] += 1
        progress['wins'] += int(outcome == 'victory')
        progress['streak'] = progress['streak'] + 1 if outcome == 'victory' else 0
        tier = progress['tier']
        progress['tier'] = min(policy['maximum_tier'], tier + 1) if outcome == 'victory' else max(1, tier - 1)
        points = event['win_points'] * tier if outcome == 'victory' else event['lose_points']
        progress['points'] += points
        state['matchups'].pop(event_id, None)
        battle['status'] = 'complete'
        battle['outcome'] = outcome
        battle['stars'] = stars
        battle['completed_at'] = now
        connection.execute('UPDATE battles SET data=? WHERE id=?',
                           (canonical(battle), battle['battle_id']))
        return {'battle_id': battle['battle_id'], 'outcome': outcome, 'stars': stars,
                'points': points, 'tier': progress['tier'], 'streak': progress['streak'],
                'rewards': deepcopy(earned), 'blitz': deepcopy(state), 'server_time': now}

    fail('unknown_command', 'Blitz command is not defined', 404)

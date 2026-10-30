"""Period-bound pass points, automatic premium access and atomic prize claims."""
from copy import deepcopy

from .catalog import fail, identifier, integer, object_fields, rewards
from .objectives import period, period_for_key, view as objective_view


def validate(definitions, catalog):
    if not isinstance(definitions, dict) or 'objectives' not in catalog:
        fail('invalid_catalog', 'Passes require objective counters')
    for key, definition in definitions.items():
        identifier(key)
        object_fields(definition, ('counter_id', 'required_level', 'maximum_level', 'premium_required_level', 'rewards'), ('claim_grace_seconds',))
        counter = catalog['objectives']['counters'].get(identifier(definition['counter_id']))
        if counter is None or counter['source']['kind'] not in ('activity', 'signals'):
            fail('invalid_catalog', 'Pass points require transaction-derived or dated-signal counters')
        integer(definition['required_level'], 1, 10000)
        integer(definition['maximum_level'], definition['required_level'], 10000)
        integer(definition['premium_required_level'], definition['required_level'], 10000)
        integer(definition.get('claim_grace_seconds', 0), 0, 86400)
        rows = definition['rewards']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            fail('invalid_catalog', 'Pass rewards must be a bounded nonempty list')
        seen = set()
        for row in rows:
            object_fields(row, ('reward_id', 'threshold', 'track', 'rewards'))
            key = identifier(row['reward_id'])
            if key in seen or row['track'] not in ('free', 'premium'):
                fail('invalid_catalog', 'Pass reward IDs must be distinct and tracks defined')
            seen.add(key)
            integer(row['threshold'])
            rewards(row['rewards'])


def window_for_key(counter, player, now, key):
    return period_for_key(counter, player, now, key)


def read_one(player, catalog, key, now, period_key=None):
    definition = catalog['passes'][key]
    counter = catalog['objectives']['counters'][definition['counter_id']]
    current = period(counter, player, now)
    window = window_for_key(counter, player, now, period_key or current['key'])
    sources = player.get('pass_sources', {}).get(key, {})
    saved = sources.get(window['key'])
    level = definition['required_level'] <= player['level'] <= definition['maximum_level']
    began = window['key'] == 'lifetime' or int(window['key']) >= 0
    eligible = level and began and current['key'] == window['key']
    enrolled = saved is not None or (eligible and window['active'])
    premium = bool((saved or {}).get('premium') or (eligible and player['level'] >= definition['premium_required_level']))
    claim_until = (window['ends_at'] + definition.get('claim_grace_seconds', 0)) if window['ends_at'] is not None else None
    active = eligible and enrolled and (window['starts_at'] is None or window['starts_at'] <= now < claim_until)
    at = now if window['active'] else (window['starts_at'] if window['starts_at'] is not None else now)
    points = objective_view(player, catalog['objectives'], at)['counters'][definition['counter_id']]['value']
    claimed = player.get('pass_claims', {}).get(key, {}).get(window['key'], [])
    prizes = []
    for row in definition['rewards']:
        owned = row['reward_id'] in claimed
        prizes.append({'reward_id': row['reward_id'], 'threshold': row['threshold'], 'track': row['track'],
                       'claimed': owned, 'eligible': active and points >= row['threshold'] and (row['track'] == 'free' or premium),
                       'claimable': active and not owned and points >= row['threshold'] and (row['track'] == 'free' or premium)})
    selected_at = (saved or {}).get('selected_at', max(window['starts_at'] or player['created_at'],
        player.get('level_reached_at', {}).get(str(definition['required_level']), player['created_at'])))
    return {'pass_id': key, 'period': window, 'points': points, 'enrolled': enrolled, 'selected': eligible and window['active'],
            'premium': premium, 'claim_active': active, 'claim_until': claim_until, 'selected_at': selected_at, 'rewards': prizes}


def view(player, catalog, now):
    result = {}
    for key, definition in catalog.get('passes', {}).items():
        current = read_one(player, catalog, key, now)
        known = set(player.get('pass_sources', {}).get(key, {})) | set(player.get('pass_claims', {}).get(key, {}))
        history = [read_one(player, catalog, key, now, period_key) for period_key in sorted(known)
                   if period_key != current['period']['key']]
        result[key] = {'current': current, 'history': history}
    return {'passes': result, 'server_time': now}


def reconcile(player, catalog, now):
    for key in catalog.get('passes', {}):
        current = read_one(player, catalog, key, now)
        if current['selected']:
            sources = player.setdefault('pass_sources', {}).setdefault(key, {})
            source = sources.setdefault(current['period']['key'], {'selected_at': current['selected_at'], 'premium': False})
            source['premium'] = source['premium'] or current['premium']


def apply(service, player, command, parameters, awarded, now):
    if command not in ('pass.claim', 'pass.unlock'):
        fail('unknown_command', 'Pass command is undefined')
    if command == 'pass.unlock':
        object_fields(parameters, ('pass_id', 'period_key'))
    elif parameters.get('all') is True:
        object_fields(parameters, ('pass_id', 'period_key', 'all'))
    else:
        object_fields(parameters, ('pass_id', 'period_key', 'reward_ids'))
    key = identifier(parameters['pass_id'])
    if key not in service.catalog.get('passes', {}):
        fail('pass_not_found', 'Pass is undefined', 404)
    state = read_one(player, service.catalog, key, now)
    if parameters['period_key'] != state['period']['key'] or not state['claim_active']:
        fail('pass_not_active', 'Pass period is not active', 409)
    if command == 'pass.unlock':
        if not state['premium']:
            fail('pass_premium_required', 'Automatic premium level requirement is not met', 409)
        return {'pass_id': key}
    if parameters.get('all') is True:
        targets = [row['reward_id'] for row in state['rewards'] if row['claimable']]
    else:
        targets = parameters['reward_ids']
        if not isinstance(targets, list) or not 1 <= len(targets) <= 1000:
            fail('invalid_pass_rewards', 'Pass claim requires distinct reward IDs')
        for target in targets:
            identifier(target)
        if len(set(targets)) != len(targets):
            fail('invalid_pass_rewards', 'Pass reward IDs must be distinct')
        eligible = {row['reward_id'] for row in state['rewards'] if row['claimable']}
        if not set(targets) <= eligible:
            fail('pass_reward_not_earned', 'Pass reward is locked or already claimed', 409)
    definitions = {row['reward_id']: row for row in service.catalog['passes'][key]['rewards']}
    bundles = []
    for target in targets:
        reward = definitions[target]['rewards']
        service._reward(player, reward)
        bundles.append((reward, 1))
    claimed = player.setdefault('pass_claims', {}).setdefault(key, {}).setdefault(state['period']['key'], [])
    claimed.extend(targets)
    return {'pass_id': key, 'reward_ids': deepcopy(targets), 'rewards': awarded(*bundles)}

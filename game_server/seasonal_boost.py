"""Application-owned seasonal progression, minute accrual and paid boosts.

Definitions are neutral. The private gateway compiles original chapter/event
and virtual-mod presentation into these rules, never into the server runtime.
"""
from copy import deepcopy
import math

from .catalog import fail, identifier, integer, object_fields, amounts


def _wallet(value, catalog):
    amounts(value)
    supported = set(catalog['starting_wallet']) | set(catalog.get('resource_rules', {}))
    if set(value) - supported:
        fail('invalid_catalog', 'Seasonal boost refers to an unavailable wallet currency')


def validate(policy, catalog):
    object_fields(policy, ('starts_at', 'ends_at', 'maximum_accrual_minutes',
                          'claim_throttle_seconds', 'xp_currency_id', 'tier_thresholds',
                          'score_multipliers', 'character_tags', 'rates', 'boosts'), ('enabled',))
    if type(policy.get('enabled', True)) is not bool:
        fail('invalid_catalog', 'Seasonal chapter enablement must be boolean')
    start, end = integer(policy['starts_at']), integer(policy['ends_at'])
    if end <= start:
        fail('invalid_catalog', 'Seasonal chapter end must follow start')
    integer(policy['maximum_accrual_minutes'], 1, 525600)
    integer(policy['claim_throttle_seconds'], 0, 86400)
    if identifier(policy['xp_currency_id']) not in set(catalog['starting_wallet']) | set(catalog.get('resource_rules', {})):
        fail('invalid_catalog', 'Seasonal experience currency is unavailable')
    thresholds = policy['tier_thresholds']
    if not isinstance(thresholds, list) or len(thresholds) > 64:
        fail('invalid_catalog', 'Seasonal boost tiers must be bounded')
    for index, threshold in enumerate(thresholds):
        integer(threshold, 1)
        if index and threshold <= thresholds[index - 1]:
            fail('invalid_catalog', 'Seasonal tier thresholds must increase')
    multipliers = policy['score_multipliers']
    object_fields(multipliers, ('total', 'age', 'chapter'))
    for value in multipliers.values():
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1000:
            fail('invalid_catalog', 'Seasonal score multipliers must be finite and nonnegative')
    tags = policy['character_tags']
    if not isinstance(tags, dict) or set(tags) - set(catalog['characters']):
        fail('invalid_catalog', 'Seasonal tags must belong to known characters')
    for character, membership in tags.items():
        if not isinstance(membership, list) or not 0 <= len(membership) <= 5 or any(
                type(value) is not str or value not in ('age', 'chapter') for value in membership):
            fail('invalid_catalog', 'Seasonal collections must have normalized age/chapter tags')
        if len(set(membership)) != len(membership):
            fail('invalid_catalog', 'Seasonal collection tags must be distinct')
    rates = policy['rates']
    if not isinstance(rates, list) or not 1 <= len(rates) <= 256:
        fail('invalid_catalog', 'Seasonal rates must be defined')
    previous = -1
    for row in rates:
        object_fields(row, ('minimum_score', 'wallet_per_minute'))
        minimum = integer(row['minimum_score'])
        if minimum <= previous or (previous < 0 and minimum != 0):
            fail('invalid_catalog', 'Seasonal rates must start at zero and strictly increase')
        previous = minimum
        _wallet(row['wallet_per_minute'], catalog)
    boosts = policy['boosts']
    if not isinstance(boosts, dict) or not boosts:
        fail('invalid_catalog', 'Seasonal boost paths are required')
    for trait, stats in boosts.items():
        identifier(trait)
        if not isinstance(stats, dict) or not stats:
            fail('invalid_catalog', 'Seasonal trait requires supported stats')
        for stat, definition in stats.items():
            identifier(stat)
            object_fields(definition, ('tier_requirements', 'costs'))
            requirements, costs = definition['tier_requirements'], definition['costs']
            if not isinstance(requirements, list) or not 1 <= len(requirements) <= 100 or (
                    not isinstance(costs, list) or len(costs) != len(requirements)):
                fail('invalid_catalog', 'Seasonal levels require one tier and cost per level')
            for needed, cost in zip(requirements, costs):
                integer(needed, 1, len(thresholds) + 1)
                _wallet(cost, catalog)


def active(policy, now):
    return policy.get('enabled', True) and policy['starts_at'] <= now < policy['ends_at']


def score(player, policy):
    tcp = acp = ccp = 0
    for character_id, character in player['characters'].items():
        power = integer(character.get('power', 0))
        tcp += power
        membership = policy['character_tags'].get(character_id, ())
        if 'age' in membership:
            acp += power
        if 'chapter' in membership:
            ccp += power
    scales = policy['score_multipliers']
    tcs = int(tcp * scales['total'] + acp * scales['age'] + ccp * scales['chapter'])
    if max(tcp, acp, ccp, tcs) > 0xFFFFFFFF:
        fail('seasonal_score_overflow', 'Seasonal roster score exceeds the supported range', 409)
    return {'tcs': tcs, 'tcp': tcp, 'acp': acp, 'ccp': ccp}


def _rate(scores, policy):
    return next(row for row in reversed(policy['rates']) if row['minimum_score'] <= scores['tcs'])


def _new(now):
    return {'initialized_at': now, 'last_claimed_at': now,
            'last_unlocked_at': now, 'boosts': [], 'levels': {}}


def _pending(state, policy, now, rate):
    seconds = min(max(0, now - state['last_claimed_at']),
                  policy['maximum_accrual_minutes'] * 60,
                  max(0, policy['ends_at'] - state['last_claimed_at']))
    minutes = seconds // 60
    result = {key: integer(amount * minutes) for key, amount in rate['wallet_per_minute'].items()
              if amount * minutes > 0}
    return minutes, result


def view(player, catalog, now):
    policy = catalog.get('seasonal_boost')
    if policy is None:
        fail('feature_unavailable', 'Seasonal boosts are not configured', 404)
    state = player.get('seasonal_boost')
    scores = score(player, policy)
    rate = _rate(scores, policy)
    minutes, pending = _pending(state, policy, now, rate) if state else (0, {})
    return {'available': active(policy, now), 'scores': scores, 'accrual_rate': deepcopy(rate),
            'state': deepcopy(state), 'pending_minutes': minutes,
            'pending_wallet': pending, 'server_time': now}


def apply(service, player, command, parameters, now):
    policy = service.catalog.get('seasonal_boost')
    if policy is None:
        fail('feature_unavailable', 'Seasonal boosts are not configured', 404)
    if not active(policy, now):
        fail('seasonal_inactive', 'Seasonal chapter is not active', 409)
    if command in ('seasonal.refresh', 'seasonal.claim'):
        object_fields(parameters, ())
    elif command == 'seasonal.buy':
        object_fields(parameters, ('trait_id', 'stat_id', 'expected_level'))
        trait_id, stat_id = identifier(parameters['trait_id']), identifier(parameters['stat_id'])
        definition = policy['boosts'].get(trait_id, {}).get(stat_id)
        if definition is None:
            fail('seasonal_boost_missing', 'Seasonal boost is not offered', 404)
        level = integer(parameters['expected_level'])
    else:
        fail('unknown_command', 'Seasonal boost command is not defined')
    state = player.setdefault('seasonal_boost', _new(now))
    gained = {}
    purchased = None
    if command == 'seasonal.claim':
        if now - state['last_claimed_at'] < policy['claim_throttle_seconds']:
            fail('seasonal_claim_throttled', 'Seasonal accrual is not ready', 409)
        rate = _rate(score(player, policy), policy)
        minutes, gained = _pending(state, policy, now, rate)
        if not gained:
            fail('seasonal_nothing_to_claim', 'No seasonal accrual can be collected', 409)
        service._reward(player, {'wallet': gained})
        state['last_claimed_at'] = now
    elif command == 'seasonal.buy':
        key = trait_id + ':' + stat_id
        current = state['levels'].get(key, 0)
        if level != current:
            fail('seasonal_stale_level', 'Expected boost level does not match ownership', 409)
        if current >= len(definition['costs']):
            fail('seasonal_boost_cap', 'Seasonal boost is at its cap', 409)
        experience = player['wallet'].get(policy['xp_currency_id'], 0)
        tier = 1 + sum(experience >= threshold for threshold in policy['tier_thresholds'])
        if tier < definition['tier_requirements'][current]:
            fail('seasonal_tier_required', 'Seasonal experience tier has not been reached', 409)
        service._spend(player, definition['costs'][current])
        state['levels'][key] = current + 1
        purchased = {'trait_id': trait_id, 'stat_id': stat_id, 'level': current + 1, 'at': now}
        state['boosts'].append(purchased)
        state['last_unlocked_at'] = now
    result = view(player, service.catalog, now)
    result.update(claimed_wallet=gained, purchased=purchased)
    return result

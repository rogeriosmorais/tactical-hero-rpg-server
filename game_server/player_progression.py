"""Application XP thresholds and once-per-level catalog award receipts."""
from bisect import bisect_right
from copy import deepcopy

from .catalog import fail, identifier, integer, object_fields, rewards


def validate(catalog):
    thresholds = catalog.get('experience_thresholds')
    if thresholds is not None:
        if not isinstance(thresholds, list) or len(thresholds) != catalog['maximum_player_level'] or thresholds[0] != 0:
            fail('invalid_catalog', 'Player XP thresholds must start at zero and cover every level')
        previous = -1
        for threshold in thresholds:
            integer(threshold, previous + 1)
            previous = threshold
    policy = catalog.get('level_rewards')
    if policy is None:
        return
    object_fields(policy, ('policy_id', 'awards'))
    identifier(policy['policy_id'])
    if not isinstance(policy['awards'], dict):
        fail('invalid_catalog', 'Level awards must be an object')
    for key, bundle in policy['awards'].items():
        if not isinstance(key, str) or len(key) > 5 or not key.isascii() or not key.isdecimal() or str(int(key)) != key:
            fail('invalid_catalog', 'Level award keys must be canonical level numbers')
        integer(int(key), 2, catalog['maximum_player_level'])
        rewards(bundle)
        if bundle.get('experience', 0):
            fail('invalid_catalog', 'Level awards cannot grant experience')


def level_for_experience(catalog, experience):
    thresholds = catalog.get('experience_thresholds')
    return bisect_right(thresholds, experience) if thresholds is not None else min(
        catalog['maximum_player_level'], 1 + experience // catalog['experience_per_level'])


def view(player, catalog):
    level = player['level']
    thresholds = catalog.get('experience_thresholds')
    minimum = thresholds[level - 1] if thresholds is not None else (level - 1) * catalog['experience_per_level']
    next_level = (thresholds[level] if thresholds is not None else level * catalog['experience_per_level']) if level < catalog['maximum_player_level'] else None
    return {'level': level, 'experience': player['experience'], 'minimum_experience': minimum,
            'next_level_experience': next_level, 'maximum_level': catalog['maximum_player_level'],
            'awarded_levels': deepcopy(player.get('level_reward_claims', {}))}


def award(service, player, previous_level):
    policy = service.catalog.get('level_rewards')
    result = []
    if policy is None:
        return result
    for level in range(previous_level + 1, player['level'] + 1):
        key = str(level)
        bundle = policy['awards'].get(key)
        if bundle is None or key in player.get('level_reward_claims', {}):
            continue
        receipt = {'policy_id': policy['policy_id'], 'at': service._command_time(player)}
        player.setdefault('level_reward_claims', {})[key] = receipt
        service._reward(player, bundle)
        result.append({'level': level, 'rewards': deepcopy(bundle), **receipt})
    return result

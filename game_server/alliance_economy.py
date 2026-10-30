"""Profile-owned donation limits, earned alliance XP and permanent combat boosts."""
from copy import deepcopy
import math

from .alliances import current
from .catalog import amounts, fail, identifier, integer, object_fields, rewards


def validate(rules, catalog):
    object_fields(rules, ('donations', 'boosts'))
    if 'alliances' not in catalog or any(not isinstance(value, dict) for value in rules.values()):
        fail('invalid_catalog', 'Alliance economy requires alliance policy and defined collections')
    for key, steps in rules['donations'].items():
        identifier(key)
        if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
            fail('invalid_catalog', 'Donation tiers require bounded ordinal steps')
        for step in steps:
            object_fields(step, ('cost', 'rewards', 'maxed_rewards', 'experience'))
            amounts(step['cost'])
            rewards(step['rewards'])
            rewards(step['maxed_rewards'])
            integer(step['experience'])
    for category, entries in rules['boosts'].items():
        identifier(category)
        if not isinstance(entries, dict) or not entries or len(entries) > 100:
            fail('invalid_catalog', 'Boost categories require bounded nonempty definitions')
        for key, steps in entries.items():
            identifier(key)
            if not isinstance(steps, list) or not 1 <= len(steps) <= 1000:
                fail('invalid_catalog', 'Boosts require bounded nonempty levels')
            previous = 0
            for step in steps:
                object_fields(step, ('required_level', 'cost', 'percent'))
                integer(step['required_level'], 1, catalog['maximum_player_level'])
                amounts(step['cost'])
                percent = step['percent']
                if type(percent) not in (int, float) or not previous <= percent <= 10000 or not math.isfinite(percent):
                    fail('invalid_catalog', 'Boost percentages must be finite, nonnegative and nondecreasing')
                previous = percent


def maxed(player, rules):
    return bool(rules['boosts']) and all(player.get('alliance_boosts', {}).get(category, {}).get(key, 0) == len(steps)
        for category, entries in rules['boosts'].items() for key, steps in entries.items())


def view(player, catalog, now):
    rules = catalog.get('alliance_economy', {'donations': {}, 'boosts': {}})
    donations = player.get('alliance_donations', {})
    day = now // 86400
    count = donations.get('days', {}).get(str(day), 0)
    boosts = {}
    for category, entries in rules['boosts'].items():
        boosts[category] = {}
        for key, steps in entries.items():
            level = player.get('alliance_boosts', {}).get(category, {}).get(key, 0)
            boosts[category][key] = {'level': level, 'percent': steps[level - 1]['percent'] if level else 0,
                'maximum_level': len(steps), 'next_step': deepcopy(steps[level]) if level < len(steps) else None}
    return {'donations': {'day': day, 'count': count, 'last_at': donations.get('last_at', 0),
                'lifetime_count': donations.get('lifetime_count', 0), 'experience': donations.get('experience', 0),
                'cooldown_until': player.get('alliance_donation_cooldown_until', 0)},
            'boosts': boosts, 'boosts_maxed': maxed(player, rules)}


def apply(service, player, command, parameters, awarded, now):
    rules = service.catalog.get('alliance_economy')
    alliance = current(player)
    if rules is None:
        fail('feature_unavailable', 'Alliance economy is not configured', 404)
    if player['level'] < service.catalog['alliances']['required_level']:
        fail('alliance_level_required', 'Alliance level requirement is not met', 409)
    if alliance is None:
        fail('alliance_required', 'Current alliance membership is required', 409)
    if command == 'alliance.donate':
        object_fields(parameters, ('tier_id',))
        tier = identifier(parameters['tier_id'])
        if now < player.get('alliance_donation_cooldown_until', 0):
            fail('alliance_donation_cooldown', 'Joining cooldown prevents donation', 409)
        steps = rules['donations'].get(tier)
        if steps is None:
            fail('donation_tier_not_found', 'Donation tier is undefined', 404)
        donations = player.setdefault('alliance_donations', {})
        day = str(now // 86400)
        ordinal = donations.get('days', {}).get(day, 0)
        if ordinal >= len(steps):
            fail('donation_limit', 'Daily donation limit is reached', 409)
        step = steps[ordinal]
        service._spend(player, step['cost'])
        bundle = step['maxed_rewards'] if maxed(player, rules) else step['rewards']
        service._reward(player, bundle)
        donations.setdefault('days', {})[day] = ordinal + 1
        donations['last_at'] = now
        donations['lifetime_count'] = integer(donations.get('lifetime_count', 0) + 1)
        donations['experience'] = integer(donations.get('experience', 0) + step['experience'])
        alliance['experience'] = integer(alliance['experience'] + step['experience'])
        policy = service.catalog['alliances']
        alliance['level'] = min(policy['maximum_level'], 1 + alliance['experience'] // policy['experience_per_level'])
        return {'donation_tier_id': tier, 'donation_count': ordinal + 1, 'alliance_experience': step['experience'],
                'payment': deepcopy(step['cost']), 'rewards': awarded((bundle, 1))}
    if command not in ('alliance.upgrade_boost', 'alliance.upgrade_category'):
        fail('unsupported_command', 'Alliance economy action is undefined', 404)
    object_fields(parameters, ('category_id', 'boost_id') if command == 'alliance.upgrade_boost' else ('category_id',))
    category = identifier(parameters['category_id'])
    entries = rules['boosts'].get(category)
    if entries is None:
        fail('boost_category_not_found', 'Boost category is undefined', 404)
    targets = [identifier(parameters['boost_id'])] if command == 'alliance.upgrade_boost' else list(entries)
    payment, purchased = {}, []
    levels = player.get('alliance_boosts', {}).get(category, {})
    for key in targets:
        steps = entries.get(key)
        if steps is None:
            fail('boost_not_found', 'Boost is undefined', 404)
        level = levels.get(key, 0)
        if level == len(steps):
            fail('boost_maximum', 'Boost is already at its maximum', 409)
        step = steps[level]
        if player['level'] < step['required_level']:
            fail('boost_level_required', 'Boost level requirement is not met', 409)
        for currency, amount in step['cost'].items():
            payment[currency] = integer(payment.get(currency, 0) + amount)
        purchased.append({'category_id': category, 'boost_id': key, 'level': level + 1, 'percent': step['percent']})
    service._spend(player, payment)
    levels = player.setdefault('alliance_boosts', {}).setdefault(category, {})
    for row in purchased:
        levels[row['boost_id']] = row['level']
    return {'purchased': purchased, 'quantity': len(purchased), 'payment': payment}

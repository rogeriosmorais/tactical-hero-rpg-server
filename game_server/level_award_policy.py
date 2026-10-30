"""Explicitly estimated local level-award policy, expressed only in neutral IDs.

This preserves a local experimental schedule. It is not publisher parity or
independently recovered game data, and is never enabled by the demo catalog.
"""
from .catalog import identifier, integer, object_fields


def estimated_policy(currencies, fragment_item, maximum_level=110):
    object_fields(currencies, ('credits', 'premium', 'energy', 'shop', 'charges', 'donations'))
    for key in currencies.values():
        identifier(key)
    identifier(fragment_item)
    integer(maximum_level, 1, 110)
    awards = {}
    for level in range(4, maximum_level + 1):
        if level == 21:
            amounts = {'energy': 70, 'premium': 20, 'shop': 9000, 'charges': 200, 'donations': 50}
            inventory = {fragment_item: 2000}
        else:
            if level <= 20:
                credits, premium = 1000 * level, 10
            elif level <= 40:
                credits, premium = 1500 * level, 15
            elif level <= 60:
                credits, premium = 3000 * level, 20
            elif level <= 80:
                credits, premium = 500000 if level >= 76 else 6000 * level, 25
            elif level <= 90:
                credits, premium = 2000000 if level in (85, 90) else 1500000, 100
            else:
                credits, premium = 4000000 if level >= 105 else 2000000, 100
            amounts = {'credits': credits, 'premium': premium, 'energy': 100}
            if level >= 20:
                amounts['donations'] = 50
            inventory = {}
        wallet = {}
        for role, amount in amounts.items():
            key = currencies[role]
            wallet[key] = wallet.get(key, 0) + amount
        awards[str(level)] = {'wallet': wallet, 'inventory': inventory}
    return {'policy_id': 'estimated-level-awards-v1', 'awards': awards}

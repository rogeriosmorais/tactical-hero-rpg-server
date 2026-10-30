"""Catalog resource policies; unlimited balances and level-based soft capacities."""
from .catalog import fail, identifier, integer, object_fields


def validate(definitions, catalog):
    if not isinstance(definitions, dict):
        fail('invalid_catalog', 'Resource rules must be an object')
    for key, rule in definitions.items():
        identifier(key)
        object_fields(rule, ('mode',), ('display_balance', 'capacity', 'level_capacities'))
        if rule['mode'] not in ('finite', 'unlimited'):
            fail('invalid_catalog', 'Resource mode is undefined')
        if rule['mode'] == 'unlimited':
            integer(rule.get('display_balance'), 1)
        elif 'display_balance' in rule:
            fail('invalid_catalog', 'Only unlimited resources have a fixed display balance')
        if 'capacity' in rule:
            integer(rule['capacity'])
        levels = rule.get('level_capacities', {})
        if not isinstance(levels, dict) or (levels and 'capacity' not in rule):
            fail('invalid_catalog', 'Level capacities require an initial capacity')
        previous = rule.get('capacity', 0)
        ordered = []
        for level, value in levels.items():
            if not isinstance(level, str) or len(level) > 5 or not level.isascii() or not level.isdecimal() or str(int(level)) != level:
                fail('invalid_catalog', 'Capacity levels must be canonical level numbers')
            integer(int(level), 1, catalog['maximum_player_level'])
            integer(value)
            ordered.append((int(level), value))
        for level, value in sorted(ordered):
            if value < previous:
                fail('invalid_catalog', 'Earned resource capacities cannot decrease')
            previous = value


def unlimited(catalog, key):
    return catalog.get('resource_rules', {}).get(key, {}).get('mode') == 'unlimited'


def initialize(player, catalog):
    for key, rule in catalog.get('resource_rules', {}).items():
        if rule['mode'] == 'unlimited':
            player['wallet'][key] = rule['display_balance']


def view(player, catalog):
    result = {}
    for key, rule in catalog.get('resource_rules', {}).items():
        row = {'balance': player['wallet'].get(key, 0), 'mode': rule['mode']}
        if 'capacity' in rule:
            row['capacity'] = max([rule['capacity']] + [value for level, value in rule.get('level_capacities', {}).items()
                                                       if int(level) <= player['level']])
        result[key] = row
    return result

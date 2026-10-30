"""Ordered weighted reward groups; rolls are issued inside owning transactions."""
from copy import deepcopy
import random

from .catalog import amounts, fail, identifier, integer, object_fields, rewards


def validate(tables):
    if not isinstance(tables, dict):
        fail('invalid_catalog', 'Loot tables must be an object')
    for key, table in tables.items():
        identifier(key)
        object_fields(table, ('groups',))
        groups = table['groups']
        # Original one-of-each Dark Dimension completion boxes can contain
        # 178 independent reward groups. Keep a finite safety cap, but do
        # not reject valid source-authored groups solely because the former
        # small catalog limit was 100.
        if not isinstance(groups, list) or len(groups) > 256:
            fail('invalid_catalog', 'Loot tables require 0–256 ordered groups')
        for choices in groups:
            weighted(choices)
            for choice in choices:
                object_fields(choice, ('weight', 'multiplier', 'items'))
                integer(choice['multiplier'], 1)
                weighted(choice['items'])
                for item in choice['items']:
                    object_fields(item, ('weight', 'minimum', 'maximum', 'multiplier', 'rewards'), ('counters',))
                    amounts(item.get('counters', {}))
                    if item['rewards'] is None:
                        if item.get('counters') or any(type(item[field]) is not int or item[field] != 0 for field in ('minimum', 'maximum', 'multiplier')):
                            fail('invalid_catalog', 'Empty loot outcomes cannot have a quantity')
                        continue
                    integer(item['minimum'], 1)
                    integer(item['maximum'], item['minimum'])
                    integer(item['multiplier'], 1)
                    rewards(item['rewards'])
                    maximum = integer(item['maximum'] * item['multiplier'] * choice['multiplier'])
                    for category in ('wallet', 'inventory'):
                        for value in item['rewards'].get(category, {}).values():
                            integer(value * maximum)
                    integer(item['rewards'].get('experience', 0) * maximum)
                    for signal in item['rewards'].get('signals', []):
                        integer(signal['quantity'] * maximum)
                    for value in item.get('counters', {}).values():
                        integer(value * maximum)


def weighted(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        fail('invalid_catalog', 'Weighted loot choices require a bounded nonempty list')
    total = 0
    for row in rows:
        if not isinstance(row, dict) or 'weight' not in row:
            fail('invalid_catalog', 'Loot choice weight is required')
        total = integer(total + integer(row['weight']))
    if total == 0:
        fail('invalid_catalog', 'Loot choices require positive total weight')


def choose(rows, rng):
    pick = rng.randrange(sum(row['weight'] for row in rows))
    for index, row in enumerate(rows):
        pick -= row['weight']
        if pick < 0:
            return index, row
    raise AssertionError('Unreachable weighted loot selection')


def roll(tables, table_id, seed):
    table = tables.get(identifier(table_id))
    if table is None:
        fail('loot_table_not_found', 'Reward table is not configured', 404)
    from .service import awarded
    rng = random.Random(seed)
    bundles, selections, counters = [], [], {}
    for group_index, choices in enumerate(table['groups']):
        choice_index, choice = choose(choices, rng)
        item_index, item = choose(choice['items'], rng)
        quantity = 0
        if item['rewards'] is not None:
            quantity = integer(rng.randint(item['minimum'], item['maximum']) * item['multiplier'] * choice['multiplier'])
            bundles.append((item['rewards'], quantity))
            for key, value in item.get('counters', {}).items():
                counters[key] = integer(counters.get(key, 0) + value * quantity)
        selections.append({'group_index': group_index, 'choice_index': choice_index, 'item_index': item_index,
                           'quantity': quantity})
    return {'table_id': table_id, 'selections': deepcopy(selections), 'rewards': awarded(*bundles), 'counters': counters}

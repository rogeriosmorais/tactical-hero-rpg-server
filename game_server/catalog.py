"""Server-owned, versioned gameplay definitions; no client file formats."""
from copy import deepcopy
import re

IDENTIFIER = re.compile(r'[A-Za-z0-9_.-]{1,64}\Z')
MAX_AMOUNT = 9_000_000_000_000_000


class DomainError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def fail(code, message, status=400):
    raise DomainError(code, message, status)


def object_fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        fail('invalid_fields', 'Object fields do not match the API schema')


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail('invalid_identifier', 'Identifier must contain 1–64 letters, digits, dots, underscores or hyphens')
    return value


def integer(value, minimum=0, maximum=MAX_AMOUNT):
    if type(value) is not int or not minimum <= value <= maximum:
        fail('invalid_amount', 'Integer is outside the allowed range')
    return value


def amounts(value):
    if not isinstance(value, dict):
        fail('invalid_amounts', 'Amounts must be an object')
    for key, amount in value.items():
        identifier(key)
        integer(amount)


def rewards(value):
    object_fields(value, (), ('wallet', 'inventory', 'experience'))
    amounts(value.get('wallet', {}))
    amounts(value.get('inventory', {}))
    integer(value.get('experience', 0))


def validate_catalog(value):
    object_fields(value, ('schema_version', 'starting_wallet', 'starting_characters', 'characters',
                          'missions', 'offers', 'experience_per_level', 'maximum_player_level'))
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        fail('unsupported_catalog', 'Catalog schema version must be 1')
    amounts(value['starting_wallet'])
    integer(value['experience_per_level'], 1)
    integer(value['maximum_player_level'], 1, 10000)
    for category in ('characters', 'missions', 'offers'):
        if not isinstance(value[category], dict):
            fail('invalid_catalog', 'Catalog collections must be objects')
        for key in value[category]:
            identifier(key)
    for definition in value['characters'].values():
        object_fields(definition, ('maximum_level', 'training_cost', 'power_per_level'))
        integer(definition['maximum_level'], 1, 10000)
        amounts(definition['training_cost'])
        integer(definition['power_per_level'], 1)
    amounts(value['starting_characters'])
    for key, level in value['starting_characters'].items():
        if key not in value['characters']:
            fail('invalid_catalog', 'Starting character is undefined')
        integer(level, 1, value['characters'][key]['maximum_level'])
    for key, definition in value['missions'].items():
        object_fields(definition, ('required_level', 'cost', 'rewards'), ('prerequisite', 'first_clear_rewards'))
        integer(definition['required_level'], 1)
        amounts(definition['cost'])
        rewards(definition['rewards'])
        rewards(definition.get('first_clear_rewards', {}))
        prerequisite = definition.get('prerequisite')
        if prerequisite is not None and (prerequisite not in value['missions'] or prerequisite == key):
            fail('invalid_catalog', 'Mission prerequisite is invalid')
    for key in value['missions']:
        seen = set()
        current = key
        while current is not None:
            if current in seen:
                fail('invalid_catalog', 'Mission prerequisites contain a cycle')
            seen.add(current)
            current = value['missions'][current].get('prerequisite')
    for definition in value['offers'].values():
        object_fields(definition, ('cost', 'rewards', 'purchase_limit'))
        amounts(definition['cost'])
        rewards(definition['rewards'])
        integer(definition['purchase_limit'], 1)
    return deepcopy(value)


# Authored demonstration content: these identifiers and values are fixtures for
# this API, not imports or reconstructions of any game's files or responses.
DEMO_CATALOG = {
    'schema_version': 1, 'experience_per_level': 100, 'maximum_player_level': 100,
    'starting_wallet': {'credits': 100, 'energy': 20},
    'starting_characters': {'scout': 1, 'guardian': 1},
    'characters': {
        'scout': {'maximum_level': 10, 'training_cost': {'credits': 10}, 'power_per_level': 12},
        'guardian': {'maximum_level': 10, 'training_cost': {'credits': 15}, 'power_per_level': 15},
    },
    'missions': {
        'training.1': {'required_level': 1, 'cost': {'energy': 2},
                       'rewards': {'wallet': {'credits': 5}, 'experience': 10},
                       'first_clear_rewards': {'inventory': {'badge': 1}}},
        'training.2': {'required_level': 1, 'cost': {'energy': 3}, 'prerequisite': 'training.1',
                       'rewards': {'wallet': {'credits': 8}, 'experience': 15}},
    },
    'offers': {'training-kit': {'cost': {'credits': 20}, 'rewards': {'inventory': {'training-kit': 1}},
                              'purchase_limit': 2}},
}

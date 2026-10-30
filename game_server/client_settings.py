"""Normalized per-player client settings; original DS wire fields stay private.

Replaces the legacy ProfileStore.save_ds authoritative profile/queue merge.
Settings have no gameplay, currency or entitlement side effects.
"""
from copy import deepcopy

from .catalog import fail, identifier, integer, object_fields


KINDS = frozenset(('boolean', 'integer', 'string'))


def validate(rules):
    if not isinstance(rules, dict) or not 1 <= len(rules) <= 128:
        fail('invalid_catalog', 'Client settings require 1–128 typed definitions')
    for key, definition in rules.items():
        identifier(key)
        object_fields(definition, ('kind', 'default'))
        kind, default = definition['kind'], definition['default']
        if kind not in KINDS:
            fail('invalid_catalog', 'Client setting kind is undefined')
        check(default, kind)


def check(value, kind):
    if kind == 'boolean':
        if type(value) is not bool:
            fail('invalid_setting', 'Boolean setting requires true or false')
    elif kind == 'integer':
        integer(value, -2147483648, 2147483647)
    elif kind == 'string':
        if not isinstance(value, str) or len(value) > 256:
            fail('invalid_setting', 'Text setting must be at most 256 characters')
    else:
        fail('invalid_setting', 'Setting kind is unsupported')


def view(player, catalog):
    definitions = catalog.get('client_settings')
    if not definitions:
        fail('feature_unavailable', 'Client settings are not configured', 404)
    stored = player.get('client_settings', {})
    return {'values': {key: deepcopy(stored.get(key, rule['default']))
                       for key, rule in definitions.items()}}


def apply(player, catalog, parameters):
    object_fields(parameters, ('values',))
    definitions = catalog.get('client_settings')
    if definitions is None:
        fail('feature_unavailable', 'Client settings are not configured', 404)
    values = parameters['values']
    if not isinstance(values, dict) or not 1 <= len(values) <= 128:
        fail('invalid_settings', 'Setting patch must contain 1–128 values')
    for key, value in values.items():
        if not isinstance(key, str) or key not in definitions:
            fail('setting_not_found', 'Setting identifier is not configured', 404)
        check(value, definitions[key]['kind'])
    player.setdefault('client_settings', {}).update(deepcopy(values))
    return view(player, catalog)

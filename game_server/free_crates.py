"""Server-authoritative timed free-crate progression.

Adapted from the retired offline schedule: the server owns refill time,
rotation, claims and reward selection; clients supply no prizes or clock.
Only neutral, authored catalog definitions enter this module.
"""
from copy import deepcopy

from .catalog import fail, integer, object_fields


def validate(definition):
    object_fields(definition, ('capacity', 'refill_seconds', 'rotation', 'rewards'))
    integer(definition['capacity'], 1, 100)
    integer(definition['refill_seconds'], 1, 86400)
    rotation = definition['rotation']
    if not isinstance(rotation, list) or len(rotation) < definition['capacity'] or not all(
            isinstance(value, str) and value in definition['rewards'] for value in rotation):
        fail('invalid_catalog', 'Free-crate rotation must use authored rewards')
    if not isinstance(definition['rewards'], dict) or not definition['rewards']:
        fail('invalid_catalog', 'Free-crate rewards must be a nonempty mapping')
    from .catalog import identifier, rewards
    for key, value in definition['rewards'].items():
        identifier(key)
        rewards(value)


def advance(player, definition, now):
    """Update only this player's persisted schedule; never award here."""
    state = player.setdefault('free_crates', {
        'next_refill': now + definition['refill_seconds'],
        'next_index': 0,
        'available': list(definition['rotation'][:definition['capacity']]),
    })
    cycle = definition['rotation']
    if (not isinstance(state, dict) or type(state.get('next_refill')) is not int or
            type(state.get('next_index')) is not int or
            not 0 <= state['next_index'] < len(cycle) or
            not isinstance(state.get('available'), list) or
            len(state['available']) > definition['capacity'] or
            any(item not in definition['rewards'] for item in state['available'])):
        fail('invalid_crate_state', 'Saved crate schedule is invalid', 409)
    if now >= state['next_refill']:
        periods = (now - state['next_refill']) // definition['refill_seconds'] + 1
        missing = definition['capacity'] - len(state['available'])
        for _ in range(min(periods, missing)):
            state['available'].append(cycle[state['next_index']])
            state['next_index'] = (state['next_index'] + 1) % len(cycle)
        state['next_refill'] += periods * definition['refill_seconds']
    return deepcopy(state)


def claim(player, definition, now):
    advance(player, definition, now)
    state = player['free_crates']
    if not state['available']:
        fail('no_free_crates', 'No free crate is available', 409)
    crate = state['available'].pop(0)
    return crate, deepcopy(definition['rewards'][crate]), deepcopy(state)

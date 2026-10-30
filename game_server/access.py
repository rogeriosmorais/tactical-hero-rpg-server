"""Persistent feature access earned from application level and mission progress."""
from .catalog import fail, identifier, integer, object_fields


def validate(definitions, catalog):
    if not isinstance(definitions, dict):
        fail('invalid_catalog', 'Access rules must be an object')
    items = set()
    for key, definition in definitions.items():
        identifier(key)
        object_fields(definition, ('required_level', 'mission_ids', 'item_id'))
        integer(definition['required_level'], 1, catalog['maximum_player_level'])
        item = identifier(definition['item_id'])
        if item in items:
            fail('invalid_catalog', 'Access grants must use distinct items')
        items.add(item)
        missions = definition['mission_ids']
        if not isinstance(missions, list) or len(missions) > 1000:
            fail('invalid_catalog', 'Access mission requirements must be a bounded list')
        for mission in missions:
            if identifier(mission) not in catalog['missions']:
                fail('invalid_catalog', 'Access requirement references an undefined mission')
        if len(set(missions)) != len(missions):
            fail('invalid_catalog', 'Access mission requirements must be distinct')


def eligible(player, definition):
    return player['level'] >= definition['required_level'] and all(
        player['missions'].get(key, {}).get('best_stars', 0) > 0 for key in definition['mission_ids'])


def view(player, definitions):
    return {key: {'unlocked': key in player.get('access_grants', {}),
                  'granted_at': player.get('access_grants', {}).get(key),
                  'eligible': eligible(player, definition)} for key, definition in definitions.items()}


def reconcile(service, player):
    definitions = service.catalog.get('access_rules', {})
    newly = []
    for key, definition in definitions.items():
        if key in player.get('access_grants', {}):
            continue
        item_id = definition['item_id']
        # Legacy entitlement rewards unlocked a feature by granting its
        # inventory token. That award is an authoritative public transaction.
        # Record matching application access exactly once, without granting a
        # second copy of an already earned token.
        from_reward = player['inventory'].get(item_id, 0) > 0
        if from_reward or eligible(player, definition):
            player.setdefault('access_grants', {})[key] = service._command_time(player)
            if not from_reward:
                service._reward(player, {'inventory': {item_id: 1}})
            newly.append(key)
    return newly

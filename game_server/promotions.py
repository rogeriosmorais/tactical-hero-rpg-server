"""Nonconsuming promotion entitlements and duplicate-item recycling."""
from copy import deepcopy

from .catalog import fail, identifier, integer, object_fields, rewards


def import_historic_advanced_items(service, player_id, balances):
    """Trusted offline cutover of prior earned advanced promotion items.

    Migrates the legacy redstar_rewards.migrate_diamonds monotone import rule.
    The caller MUST supply neutral IDs from a private, verified legacy source.
    Not a public game command or HTTP route: ordinary clients cannot mint
    historical items. Historic unknown/invalid items stay in the original
    archive instead of being fabricated in the new inventory.

    This imports only *previously unmirrored* event-asset balances. If an
    operator separately copies the old inventory, that operation must preserve
    the old already-imported baseline to prevent duplicate grants.
    """
    if not isinstance(balances, dict) or len(balances) > 100000:
        fail('invalid_legacy_import', 'Historic promotion balances must be a bounded map')
    definitions = service.catalog.get('promotions')
    if definitions is None:
        fail('feature_unavailable', 'Promotions are not configured', 404)
    allowed = {item_id for definition in definitions['characters'].values()
               for item_id in definition['advanced_items']}
    # Validate the whole input before writing anything. An archived unknown
    # source item is ignored, just as the retained legacy import does.
    normalized = {}
    for item_id, quantity in balances.items():
        if not isinstance(item_id, str) or not item_id or len(item_id) > 256:
            continue
        if item_id not in allowed or type(quantity) is not int or quantity < 0:
            continue
        normalized[item_id] = integer(quantity)
    from .service import canonical
    with service.transaction() as connection:
        player = service._player(connection, player_id)
        history = player.setdefault('historic_promotion_imports', {})
        changes = {}
        for item_id, amount in normalized.items():
            previous = history.get(item_id, 0)
            if type(previous) is not int or previous < 0:
                fail('invalid_legacy_import', 'Historic import journal is invalid')
            if amount <= previous:
                continue
            difference = amount - previous
            player['inventory'][item_id] = integer(player['inventory'].get(item_id, 0) + difference)
            history[item_id] = amount
            changes[item_id] = difference
        if changes:
            player['version'] += 1
            connection.execute('UPDATE players SET data=? WHERE id=?',
                               (canonical(player), player_id))
        return {'imported': changes, 'historic_imports': deepcopy(history)}


def validate(rules, characters):
    object_fields(rules, ('required_level', 'maximum_base_rank', 'characters'))
    integer(rules['required_level'], 1, 10000)
    integer(rules['maximum_base_rank'], 1, 100)
    if not isinstance(rules['characters'], dict):
        fail('invalid_catalog', 'Promotion characters must be an object')
    all_items = set()
    for key, definition in rules['characters'].items():
        identifier(key)
        if key not in characters:
            fail('invalid_catalog', 'Promotion character is undefined')
        object_fields(definition, ('items', 'advanced_items'))
        for category in ('items', 'advanced_items'):
            if not isinstance(definition[category], dict):
                fail('invalid_catalog', 'Promotion items must be objects')
            ranks = set()
            for item_id, item in definition[category].items():
                identifier(item_id)
                object_fields(item, ('rank', 'recycle_rewards') if category == 'items' else ('rank',))
                integer(item['rank'], 1, rules['maximum_base_rank'] if category == 'items' else 100)
                if item_id in all_items or item['rank'] in ranks:
                    fail('invalid_catalog', 'Promotion item IDs and per-category ranks must be distinct')
                all_items.add(item_id)
                ranks.add(item['rank'])
                if category == 'items':
                    rewards(item['recycle_rewards'])


def apply(service, player, command, parameters, awarded):
    rules = service.catalog.get('promotions')
    if rules is None:
        fail('feature_unavailable', 'Promotions are not configured', 404)
    object_fields(parameters, ('character_id',))
    key = identifier(parameters['character_id'])
    definition = rules['characters'].get(key)
    if definition is None:
        fail('promotion_character_not_found', 'Promotion character is undefined', 404)
    owned = [(item_id, row, player['inventory'].get(item_id, 0)) for item_id, row in definition['items'].items()
             if player['inventory'].get(item_id, 0) > 0]
    if command == 'promotion.apply':
        character = player['characters'].get(key)
        if character is None:
            fail('character_not_owned', 'Character must be owned before applying promotions', 409)
        if player['level'] < rules['required_level']:
            fail('promotion_locked', 'Promotion account level requirement is not met', 409)
        maximum = rules['maximum_base_rank']
        rank = character.get('rank', 1)
        current = character.get('promotion_rank', 0)
        advanced = [row['rank'] for item, row in definition['advanced_items'].items() if player['inventory'].get(item, 0) > 0]
        advanced_allowed = rank == maximum and current >= maximum
        if not owned and not (advanced_allowed and advanced):
            fail('promotion_not_owned', 'No applicable promotion item is owned', 409)
        target = (maximum + max(advanced) if advanced_allowed and advanced else
                  min(max(row['rank'] for _, row, _ in owned), rank))
        character['promotion_rank'] = max(current, target)
        return {'character_id': key, 'character': deepcopy(character)}
    if command != 'promotion.recycle':
        fail('unknown_command', 'Promotion command is undefined')
    highest = max((row['rank'] for _, row, _ in owned), default=0)
    bundles = []
    for item_id, row, quantity in owned:
        retained = 1 if row['rank'] == highest else 0
        removed = quantity - retained
        if removed:
            player['inventory'][item_id] = retained
            service._reward(player, row['recycle_rewards'], removed)
            bundles.append((row['recycle_rewards'], removed))
    return {'rewards': awarded(*bundles)}

"""Paid inventory-token openings with server-generated randomness and receipts."""
import secrets

from .catalog import fail, identifier, integer, object_fields
from .loot import roll


def validate(products, catalog):
    if not isinstance(products, dict):
        fail('invalid_catalog', 'Loot products must be an object')
    for key, product in products.items():
        identifier(key)
        object_fields(product, ('revision', 'table_id', 'payment_item_id', 'unit_cost', 'experience',
                               'required_level', 'batch_required_level', 'maximum_quantity'))
        identifier(product['revision'])
        identifier(product['payment_item_id'])
        table = catalog.get('loot_tables', {}).get(identifier(product['table_id']))
        if table is None:
            fail('invalid_catalog', 'Paid opening requires a configured loot table')
        integer(product['unit_cost'], 1)
        integer(product['experience'])
        integer(product['maximum_quantity'], 1, 10000)
        integer(product['unit_cost'] * product['maximum_quantity'])
        integer(product['experience'] * product['maximum_quantity'])
        for field in ('required_level', 'batch_required_level'):
            integer(product[field], 0, catalog['maximum_player_level'])
        for choices in table['groups']:
            for choice in choices:
                for item in choice['items']:
                    if choice['weight'] and item['weight'] and item.get('counters'):
                        fail('invalid_catalog', 'Paid opening cannot grant unowned contextual counters')


def apply(service, player, parameters, awarded):
    object_fields(parameters, ('product_id', 'revision', 'quantity'), ('request_fingerprint',))
    if 'request_fingerprint' in parameters:
        fingerprint = parameters['request_fingerprint']
        if (not isinstance(fingerprint, str) or len(fingerprint) != 64 or
                any(c not in '0123456789abcdef' for c in fingerprint)):
            fail('invalid_request_fingerprint', 'Request fingerprint must be a SHA-256 hex digest')
    key = identifier(parameters['product_id'])
    product = service.catalog.get('loot_products', {}).get(key)
    if product is None:
        fail('loot_product_not_found', 'Paid loot product is unavailable', 404)
    if identifier(parameters['revision']) != product['revision']:
        fail('loot_revision_conflict', 'Paid loot product revision changed', 409)
    count = integer(parameters['quantity'], 1, product['maximum_quantity'])
    if player['level'] < product['required_level']:
        fail('loot_level_required', 'Paid opening account level is not met', 409)
    if count > 1 and player['level'] < product['batch_required_level']:
        fail('loot_batch_level_required', 'Paid batch opening account level is not met', 409)
    item, cost = product['payment_item_id'], integer(product['unit_cost'] * count)
    previous = player['inventory'].get(item, 0)
    if previous < cost:
        fail('insufficient_items', 'Paid opening tokens are insufficient', 409)
    seed = secrets.token_hex(32)
    rolls = [roll(service.catalog['loot_tables'], product['table_id'], seed + ':' + str(index)) for index in range(count)]
    bundle = awarded(*((result['rewards'], 1) for result in rolls))
    if not (any(bundle['wallet'].values()) or any(bundle['inventory'].values()) or bundle['experience'] or bundle.get('signals')):
        fail('loot_empty_result', 'Paid opening produced no prizes', 409)
    xp = integer(product['experience'] * count)
    bundle = awarded((bundle, 1), ({'experience': xp}, 1))
    player['inventory'][item] = previous - cost
    from .objectives import track
    track(player, 'inventory_spent', item, cost)
    service._reward(player, bundle)
    return {'product_id': key, 'revision': product['revision'], 'quantity': count, 'opening_experience': xp,
            'server_time': service._command_time(player),
            'payment': {'item_id': item, 'quantity': cost, 'previous_balance': previous, 'balance': player['inventory'][item]},
            'rewards': bundle, 'roll_seed': seed, 'loot_rolls': rolls}

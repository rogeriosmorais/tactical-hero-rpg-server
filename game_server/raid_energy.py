"""Finite raid energy purchases, source-configured prices and UTC-day receipts."""
import math

from .catalog import fail, identifier, integer, object_fields
from .resources import view as resource_view, unlimited


def validate(rule, catalog):
    object_fields(rule, ('required_level', 'currency_id', 'payment_currency_id', 'prices', 'quantities', 'conversion'))
    if 'alliances' not in catalog:
        fail('invalid_catalog', 'Raid energy requires alliance membership policy')
    integer(rule['required_level'], catalog['alliances']['required_level'], catalog['maximum_player_level'])
    for field in ('currency_id', 'payment_currency_id'):
        identifier(rule[field])
        if unlimited(catalog, rule[field]):
            fail('invalid_catalog', 'Raid energy and its payment currency must be finite')
    capacity = catalog.get('resource_rules', {}).get(rule['currency_id'], {}).get('capacity')
    if type(capacity) is not int or capacity <= 0 or rule['currency_id'] == rule['payment_currency_id']:
        fail('invalid_catalog', 'Raid energy requires a positive soft capacity and distinct payment currency')
    for field in ('prices', 'quantities'):
        values = rule[field]
        if not isinstance(values, list) or not 1 <= len(values) <= 10000:
            fail('invalid_catalog', 'Raid refill schedules must be bounded nonempty lists')
        for value in values:
            integer(value, 1)
    conversion = rule['conversion']
    if conversion is not None:
        object_fields(conversion, ('currency_id', 'rate'))
        identifier(conversion['currency_id'])
        rate = conversion['rate']
        if (conversion['currency_id'] in (rule['currency_id'], rule['payment_currency_id']) or
                unlimited(catalog, conversion['currency_id']) or type(rate) not in (int, float) or
                not math.isfinite(rate) or rate <= 0):
            fail('invalid_catalog', 'Raid refill conversion must use a distinct finite currency and positive rate')
        for price in rule['prices']:
            if not math.isfinite(price * rate):
                fail('invalid_catalog', 'Raid refill converted price is not finite')
            integer(math.ceil(price * rate), 1)


def view(player, catalog, now):
    rule = catalog.get('raid_energy')
    if rule is None:
        fail('raid_energy_unavailable', 'Raid energy refills are not configured', 404)
    from .alliances import current
    count = player.get('raid_energy_refills', {}).get(str(now // 86400), 0)
    return {'server_time': now, 'count': count, 'remaining': max(0, len(rule['prices']) - count),
            'available': player['level'] >= rule['required_level'] and current(player) is not None}


def apply(service, player, parameters, now):
    object_fields(parameters, ('expected_cost',), ('request_fingerprint',))
    if 'request_fingerprint' in parameters:
        fingerprint = parameters['request_fingerprint']
        if (not isinstance(fingerprint, str) or len(fingerprint) != 64 or
                any(char not in '0123456789abcdef' for char in fingerprint)):
            fail('invalid_request_fingerprint', 'Request fingerprint must be a SHA-256 hex digest')
    state = view(player, service.catalog, now)
    rule = service.catalog['raid_energy']
    if not state['available']:
        fail('raid_access_required', 'Raid energy requires earned level and current alliance membership', 409)
    target = rule['currency_id']
    if player['wallet'].get(target, 0) >= resource_view(player, service.catalog)[target]['capacity']:
        fail('raid_energy_full', 'Raid energy has reached its soft capacity', 409)
    count = state['count']
    if count >= len(rule['prices']):
        fail('raid_refill_limit', 'Raid refill daily schedule is exhausted', 409)
    price = rule['prices'][count]
    if type(parameters['expected_cost']) is not int or parameters['expected_cost'] != price:
        fail('raid_refill_price_mismatch', 'Displayed raid refill price changed', 409)
    currency, cost = rule['payment_currency_id'], price
    if player['wallet'].get(currency, 0) < price:
        conversion = rule['conversion']
        if conversion is None:
            fail('insufficient_funds', 'Raid refill funds are insufficient', 409)
        currency, cost = conversion['currency_id'], math.ceil(price * conversion['rate'])
    # Convert the whole price, retaining a partial primary balance unchanged.
    service._spend(player, {currency: cost})
    quantity = rule['quantities'][min(count, len(rule['quantities']) - 1)]
    service._reward(player, {'wallet': {target: quantity}})
    player.setdefault('raid_energy_refills', {})[str(now // 86400)] = count + 1
    return {'server_time': now, 'payment': {'currency_id': currency, 'quantity': cost},
            'energy': {'currency_id': target, 'quantity': quantity},
            'raid_energy': view(player, service.catalog, now)}

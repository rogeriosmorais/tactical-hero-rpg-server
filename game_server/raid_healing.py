"""Atomic raid recovery, authored prices and per-character action ordinals."""
from copy import deepcopy
import json
import math

from .catalog import fail, identifier, integer, object_fields
from .raids import instance, project
from .resources import unlimited


def validate(rule, catalog):
    object_fields(rule, ('limit', 'heal', 'revive'))
    integer(rule['limit'], -1)
    for action in ('heal', 'revive'):
        price = rule[action]
        object_fields(price, ('currency_id', 'prices', 'conversion'))
        identifier(price['currency_id'])
        if unlimited(catalog, price['currency_id']):
            fail('invalid_catalog', 'Raid recovery requires finite payment')
        if not isinstance(price['prices'], list) or not 1 <= len(price['prices']) <= 10000:
            fail('invalid_catalog', 'Raid recovery prices must be a bounded nonempty schedule')
        for cost in price['prices']:
            integer(cost, 1)
        conversion = price['conversion']
        if conversion is not None:
            object_fields(conversion, ('currency_id', 'rate'))
            identifier(conversion['currency_id'])
            rate = conversion['rate']
            if (conversion['currency_id'] == price['currency_id'] or unlimited(catalog, conversion['currency_id']) or
                    type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0):
                fail('invalid_catalog', 'Raid recovery conversion requires a distinct finite currency and positive rate')
            for cost in price['prices']:
                if not math.isfinite(cost * rate):
                    fail('invalid_catalog', 'Raid recovery converted price is not finite')
                integer(math.ceil(cost * rate), 1)


def apply(service, connection, player, parameters, now):
    object_fields(parameters, ('raid_id', 'instance_id', 'node_id', 'characters', 'request_fingerprint', 'projection_fingerprint'))
    for field in ('request_fingerprint', 'projection_fingerprint'):
        value = parameters[field]
        if (not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value)):
            fail('invalid_request_fingerprint', 'Recovery fingerprints must be SHA-256 hex digests')
    raid = instance(player, parameters['instance_id'], now)
    if raid['raid_id'] != identifier(parameters['raid_id']) or raid['team_index'] is None:
        fail('raid_team_required', 'Joined matching raid is required', 409)
    rule = service.catalog['raids'][raid['raid_id']]
    if identifier(parameters['node_id']) not in rule['nodes']:
        fail('raid_node_not_found', 'Raid recovery context must identify a known node', 404)
    policy = rule.get('healing')
    if policy is None:
        fail('raid_healing_unavailable', 'Raid recovery is not configured', 404)
    for row in connection.execute('SELECT data FROM battles WHERE player_id=?', (player['player_id'],)):
        if json.loads(row[0])['status'] == 'active':
            fail('battle_already_active', 'Recovery cannot run during combat', 409)
    heroes = parameters['characters']
    if not isinstance(heroes, list) or not 1 <= len(heroes) <= 5:
        fail('invalid_raid_heal_request', 'Recovery requires one to five distinct characters')
    count = raid.get('heal_count', 0)
    if policy['limit'] >= 0 and count + len(heroes) > policy['limit']:
        fail('raid_heal_limit', 'Raid recovery limit is exhausted', 409)
    changed, payments, seen = {}, [], set()
    for item in heroes:
        object_fields(item, ('character_id', 'revive', 'maximum_health'), ('expected_cost',))
        key = identifier(item['character_id'])
        if key in seen or key not in raid['characters'] or key not in player['characters']:
            fail('invalid_raid_heal_character', 'Recovery requires distinct owned joined characters')
        seen.add(key)
        if type(item['revive']) is not bool:
            fail('invalid_raid_heal_request', 'Revival selection must be boolean')
        maximum = integer(item['maximum_health'], 1)
        state = raid.setdefault('character_states', {}).get(key)
        health = state['health'] if state else maximum
        dead = health == 0
        if item['revive'] != dead:
            fail('raid_heal_state_mismatch', 'Recovery action does not match character state', 409)
        if not dead and health >= maximum:
            fail('raid_character_healthy', 'Character is already healthy', 409)
        action = 'revive' if dead else 'heal'
        price = policy[action]
        counts = raid.setdefault('heal_counts', {}).setdefault(key, {})
        ordinal = counts.get(action, 0)
        cost = price['prices'][min(ordinal, len(price['prices']) - 1)]
        if 'expected_cost' in item and (type(item['expected_cost']) is not int or item['expected_cost'] != cost):
            fail('raid_heal_price_mismatch', 'Displayed recovery price changed', 409)
        currency = price['currency_id']
        if currency not in player['wallet']:
            fail('raid_heal_currency_unavailable', 'Recovery currency is unavailable', 409)
        if player['wallet'][currency] < cost:
            conversion = price['conversion']
            if conversion is None:
                fail('insufficient_funds', 'Recovery funds are insufficient', 409)
            currency, cost = conversion['currency_id'], math.ceil(cost * conversion['rate'])
        previous = player['wallet'].get(currency, 0)
        service._spend(player, {currency: cost})
        payments.append({'currency_id': currency, 'quantity': cost, 'previous_balance': previous,
                         'balance': player['wallet'][currency]})
        # Maximum HP comes from trusted private stat interpretation of the frozen
        # joined roster, independently of combat boosts and client result fields.
        state.update(health=maximum, maximum_health=maximum)
        state.setdefault('energy', [0, -1, -1])
        changed[key] = deepcopy(state)
        counts[action] = ordinal + 1
    raid['heal_count'] = count + len(heroes)
    return {'raid': project(player, rule, raid, now), 'characters': changed, 'payments': payments,
            'server_time': now, 'request_fingerprint': parameters['request_fingerprint'],
            'projection_fingerprint': parameters['projection_fingerprint'], 'receipt_command': 'raid.heal'}

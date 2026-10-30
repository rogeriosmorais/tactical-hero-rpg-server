"""Deferred grants and read/delete state; no native message templates."""
from copy import deepcopy

from .catalog import fail, identifier, integer, object_fields


def validate(definitions):
    if not isinstance(definitions, dict) or len(definitions) > 10000:
        fail('invalid_catalog', 'Authored mail references must be bounded')
    for key, row in definitions.items():
        identifier(key)
        object_fields(row, ('created_at', 'expires_at'))
        integer(row['created_at'])
        integer(row['expires_at'], row['created_at'] + 1)


def all_messages(player, catalog):
    rows = {key: {'message_id': key, 'gift_id': None, 'kind': 'archive', 'created_at': row['created_at'],
        'expires_at': row['expires_at'], 'rewards': {}, 'contribution_points': 0,
        'read_at': None, 'claimed_at': None, 'deleted': False} for key, row in catalog.get('mail_messages', {}).items()}
    rows.update(deepcopy(player.get('mail', {})))
    return rows


def view(player, catalog, now):
    return {'messages': [deepcopy(row) for row in all_messages(player, catalog).values()
                         if not row['deleted'] and now < row['expires_at']], 'server_time': now}


def apply(service, player, command, parameters, now, awarded):
    from .raid_settlement import refresh
    closed = refresh(service, player, {}, now, awarded)['closed_raids']
    if command == 'mail.claim' and 'gift_id' in parameters:
        object_fields(parameters, ('gift_id',), ('request_fingerprint', 'projection_fingerprint'))
        gift_id = identifier(parameters['gift_id'])
        matches = [row for row in all_messages(player, service.catalog).values() if row['gift_id'] == gift_id]
        if len(matches) != 1:
            fail('mail_gift_not_found', 'Mail gift is unavailable', 404)
        parameters = {key: value for key, value in parameters.items() if key != 'gift_id'}
        parameters.update(message_ids=[matches[0]['message_id']], gift_ids=[gift_id])
    optional = ('gift_ids',) if command in ('mail.claim', 'mail.read_claim') else ()
    object_fields(parameters, ('message_ids',), optional + ('request_fingerprint', 'projection_fingerprint'))
    if 'request_fingerprint' in parameters or 'projection_fingerprint' in parameters:
        for field in ('request_fingerprint', 'projection_fingerprint'):
            value = parameters.get(field)
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                fail('invalid_request_fingerprint', 'Mail fingerprints must be SHA-256 hex digests')
    ids = parameters['message_ids']
    if not isinstance(ids, list) or not 1 <= len(ids) <= 8:
        fail('invalid_mail_ids', 'Mail operations require one to eight messages')
    for key in ids:
        identifier(key)
    if len(set(ids)) != len(ids):
        fail('invalid_mail_ids', 'Mail message IDs must be distinct')
    claiming = command in ('mail.claim', 'mail.read_claim')
    gifts = parameters.get('gift_ids')
    if claiming and (not isinstance(gifts, list) or len(gifts) != len(ids)):
        fail('mail_gift_context_mismatch', 'Each claimed message requires its gift ID')
    bundles, messages = [], []
    available = all_messages(player, service.catalog)
    for index, key in enumerate(ids):
        row = available.get(key)
        if row is None or row['deleted'] or now >= row['expires_at']:
            fail('mail_unavailable', 'Mail is expired, deleted or unavailable', 409)
        player.setdefault('mail', {})[key] = row
        if claiming:
            if row['gift_id'] is None or gifts[index] != row['gift_id']:
                fail('mail_gift_context_mismatch', 'Mail gift does not match this message', 409)
            if row['claimed_at'] is not None:
                fail('mail_already_claimed', 'Mail gift was already claimed', 409)
            service._reward(player, row['rewards'])
            if row['contribution_points']:
                raid = player['raids'][row['instance_id']]
                raid['contribution_points'] = integer(raid.get('contribution_points', 0) + row['contribution_points'])
            bundles.append(row['rewards'])
            row['claimed_at'] = now
        if command in ('mail.read', 'mail.read_claim') and row['read_at'] is None:
            row['read_at'] = now
        if command == 'mail.delete':
            if row['gift_id'] is not None and row['claimed_at'] is None:
                fail('mail_unclaimed_gift', 'Claim the gift before deleting mail', 409)
            row['deleted'] = True
        messages.append(deepcopy(row))
    result = {'messages': messages, 'rewards': awarded(*((bundle, 1) for bundle in bundles)),
              'closed_raids': closed, 'server_time': now}
    if 'request_fingerprint' in parameters:
        result.update(receipt_command=command, request_fingerprint=parameters['request_fingerprint'],
                      projection_fingerprint=parameters['projection_fingerprint'])
    return result

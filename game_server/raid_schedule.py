"""Original-shaped UTC daily raid scheduling on the independent player ledger.

The private gateway supplies only neutral, selected launch definitions. The
server owns the slot clock, alliance cursor and atomic multi-raid purchase.
"""
from copy import deepcopy

from .alliances import current
from .catalog import DomainError, fail, identifier, integer, object_fields


def validate(policy, catalog):
    object_fields(policy, ('enabled', 'hour_utc', 'minutes', 'entries'))
    if type(policy['enabled']) is not bool:
        fail('invalid_catalog', 'Raid scheduling enabled must be boolean')
    integer(policy['hour_utc'], 0, 23)
    integer(policy['minutes'], 0, 59)
    entries = policy['entries']
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 32:
        fail('invalid_catalog', 'Raid schedule must select one to 32 launch entries')
    seen = set()
    for key, entry in entries.items():
        identifier(key)
        object_fields(entry, ('raid_id', 'difficulty'))
        raid_id = identifier(entry['raid_id'])
        if raid_id in seen or raid_id not in catalog.get('raids', {}):
            fail('invalid_catalog', 'Scheduled raids must be distinct and configured')
        seen.add(raid_id)
        integer(entry['difficulty'], 0, len(catalog['raids'][raid_id]['difficulties']) - 1)


def seed(policy, now):
    return {'enabled': policy['enabled'], 'hour_utc': policy['hour_utc'],
            'minutes': policy['minutes'], 'entries': deepcopy(policy['entries']),
            'last_modified_at': now, 'last_attempt_at': 0}


def due_slot(schedule, now):
    offset = schedule['hour_utc'] * 3600 + schedule['minutes'] * 60
    candidate = now // 86400 * 86400 + offset
    return candidate if candidate <= now else candidate - 86400


def view(player, catalog, now):
    schedule = current(player).get('raid_schedule') if current(player) else None
    if not schedule or 'raid_schedule' not in catalog:
        return {'available': False, 'due': False, 'slot': None, 'schedule': None, 'server_time': now}
    slot = due_slot(schedule, now)
    eligible = (player['level'] >= catalog['alliances']['required_level'] and
                all(player['level'] >= catalog['raids'][entry['raid_id']]['required_level']
                    for entry in schedule['entries'].values()))
    due = (schedule['enabled'] and eligible
           and slot > schedule['last_attempt_at'] and slot >= schedule['last_modified_at'])
    return {'available': True, 'due': due, 'slot': slot, 'schedule': deepcopy(schedule), 'server_time': now}


def apply(service, player, parameters, now):
    object_fields(parameters, (), ('skip_if_unfunded',))
    skip_if_unfunded = parameters.get('skip_if_unfunded', False)
    if type(skip_if_unfunded) is not bool:
        fail('invalid_request', 'Scheduled raid affordability policy must be boolean')
    if 'raid_schedule' not in service.catalog:
        fail('feature_unavailable', 'Raid scheduling is not configured', 404)
    alliance = current(player)
    if alliance is None or player['level'] < service.catalog['alliances']['required_level']:
        fail('raid_access_required', 'Raid scheduling requires an earned alliance', 409)
    status = view(player, service.catalog, now)
    launched = []
    if status['due']:
        from .raids import apply as raid_apply
        # The native gateway formerly caught the public insufficient-funds
        # error and silently skipped this scheduled attempt. Own that
        # no-op/rollback decision here, alongside the atomic debit logic.
        before = deepcopy(player) if skip_if_unfunded else None
        for entry in alliance['raid_schedule']['entries'].values():
            try:
                issued = raid_apply(service, player, 'raid.launch',
                                    {'raid_id': entry['raid_id'], 'difficulty': entry['difficulty']}, now)
            except DomainError as error:
                # Only an already-active raid is exempt. Other launch failures
                # abort the entire public transaction, including earlier debits.
                if error.code == 'insufficient_funds' and skip_if_unfunded:
                    player.clear()
                    player.update(before)
                    return {'launched_raids': [], 'slot': status['slot'],
                            'skipped': 'insufficient_funds', 'server_time': now}
                if error.code != 'raid_already_active':
                    raise
            else:
                launched.append(issued['raid'])
        alliance['raid_schedule']['last_attempt_at'] = status['slot']
    return {'launched_raids': launched, 'slot': status['slot'], 'server_time': now}

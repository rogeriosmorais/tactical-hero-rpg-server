"""Profile-scoped local teams with one actual member and persistent membership."""
from copy import deepcopy

from .catalog import fail, identifier, integer, object_fields


def validate(rules, catalog):
    object_fields(rules, ('required_level', 'name_limits', 'tag_limits', 'message_limits', 'visibility',
        'default_visibility', 'maximum_members', 'leave_on_create', 'rejoin_cooldown_seconds',
        'ranks', 'inactive_days', 'experience_per_level', 'maximum_level'), ('bootstrap',))
    integer(rules['required_level'], 1, catalog['maximum_player_level'])
    for field in ('name_limits', 'tag_limits', 'message_limits'):
        limits = rules[field]
        if not isinstance(limits, list) or len(limits) != 2:
            fail('invalid_catalog', 'Alliance text limits require minimum and maximum lengths')
        integer(limits[0], 0 if field == 'message_limits' else 1, 4096)
        integer(limits[1], limits[0], 4096)
    if not isinstance(rules['visibility'], dict) or not rules['visibility']:
        fail('invalid_catalog', 'Alliance visibility types must be defined')
    for key, joinable in rules['visibility'].items():
        identifier(key)
        if type(joinable) is not bool:
            fail('invalid_catalog', 'Alliance visibility joinability must be boolean')
    if (not isinstance(rules['default_visibility'], str) or rules['default_visibility'] not in rules['visibility'] or
            type(rules['leave_on_create']) is not bool):
        fail('invalid_catalog', 'Alliance default visibility or creation policy is invalid')
    integer(rules['maximum_members'], 1, 10000)
    integer(rules['rejoin_cooldown_seconds'], 0, 31536000)
    integer(rules['experience_per_level'], 1)
    integer(rules['maximum_level'], 1, 10000)
    for field in ('ranks', 'inactive_days'):
        values = rules[field]
        if not isinstance(values, list) or not values:
            fail('invalid_catalog', 'Alliance settings require nonempty option lists')
        for value in values:
            identifier(value) if field == 'ranks' else integer(value, 0, 3650)
        if len(set(values)) != len(values):
            fail('invalid_catalog', 'Alliance settings options must be distinct')
    if not {'owner', 'member'} <= set(rules['ranks']):
        fail('invalid_catalog', 'Local alliances require owner and member ranks')
    if 'bootstrap' in rules:
        validate_details(rules, rules['bootstrap'])


def validate_details(rules, details):
    object_fields(details, ('name', 'tag', 'message', 'visibility_id', 'settings'))
    for field, limits in (('name', 'name_limits'), ('tag', 'tag_limits'), ('message', 'message_limits')):
        value = details[field]
        if (not isinstance(value, str) or not rules[limits][0] <= len(value) <= rules[limits][1] or
                any(ord(char) < 32 for char in value) or
                (field != 'message' and any(not char.isalnum() and char not in " .'-_" for char in value))):
            fail('invalid_alliance_text', 'Alliance ' + field + ' is invalid')
    if not isinstance(details['visibility_id'], str) or details['visibility_id'] not in rules['visibility']:
        fail('invalid_alliance_visibility', 'Alliance visibility is undefined')
    settings = details['settings']
    object_fields(settings, (), ('emblem', 'raid_rank', 'churn_days'))
    integer(settings.get('emblem', 1), 1, 1000)
    if settings.get('raid_rank', 'member') not in rules['ranks']:
        fail('invalid_alliance_rank', 'Alliance raid rank is undefined')
    churn = settings.get('churn_days', {})
    if not isinstance(churn, dict):
        fail('invalid_alliance_settings', 'Alliance churn settings must be an object')
    for rank, days in churn.items():
        if rank not in rules['ranks'] or type(days) is not int or days not in rules['inactive_days']:
            fail('invalid_alliance_settings', 'Alliance inactivity setting is undefined')


def current(player):
    return player.get('alliances', {}).get(player.get('alliance_id'))


def create(player, details, now, schedule_policy=None):
    if any(row['name'].casefold() == details['name'].casefold() for row in player.get('alliances', {}).values()):
        fail('alliance_name_taken', 'Alliance name is already used', 409)
    sequence = player.get('alliance_sequence', 1)
    key = str(integer(sequence, 1))
    player['alliance_sequence'] = integer(sequence + 1)
    row = {'alliance_id': key, **deepcopy(details), 'created_at': now, 'joined_at': now,
           'leadership_seen': True, 'experience': 0, 'level': 1, 'emblem': details['settings'].get('emblem', 1)}
    if schedule_policy is not None:
        from .raid_schedule import seed
        row['raid_schedule'] = seed(schedule_policy, now)
    player.setdefault('alliances', {})[key] = row
    player.update(alliance_id=key, alliance_rank='owner')
    return row


def reconcile(service, player):
    rules = service.catalog.get('alliances')
    if rules is None or player['level'] < rules['required_level']:
        return False
    now = service._command_time(player)
    player.setdefault('alliance_unlocked_at', now)
    if not player.get('alliance_bootstrapped'):
        if current(player) is None and 'bootstrap' in rules:
            create(player, rules['bootstrap'], now, service.catalog.get('raid_schedule'))
        player['alliance_bootstrapped'] = True
        return True
    return False


def team_view(player, row, rules):
    members = []
    if player.get('alliance_id') == row['alliance_id']:
        members.append({'player_id': player['player_id'], 'display_name': player['display_name'],
                        'level': player['level'], 'rank': player['alliance_rank'], 'joined_at': row['joined_at']})
    result = {**deepcopy(row), 'members': members, 'member_count': len(members),
            'minimum_experience': (row['level'] - 1) * rules['experience_per_level'],
            'next_level_experience': row['level'] * rules['experience_per_level']
                if row['level'] < rules['maximum_level'] else None}
    result['settings'].setdefault('raid_rank', 'member')
    result['settings'].setdefault('churn_days', {})
    result['settings']['emblem'] = row['emblem']
    return result


def view(player, rules, now):
    if rules is None:
        return {'available': False, 'current': None, 'alliances': [], 'server_time': now}
    rows = [team_view(player, row, rules) for row in sorted(player.get('alliances', {}).values(),
                                                          key=lambda row: int(row['alliance_id']))]
    return {'available': player['level'] >= rules['required_level'],
            'unlocked_at': player.get('alliance_unlocked_at'),
            'current': next((row for row in rows if row['alliance_id'] == player.get('alliance_id')), None),
            'alliances': rows, 'donation_cooldown_until': player.get('alliance_donation_cooldown_until', 0),
            'server_time': now}


def search(player, rules, parameters, now):
    object_fields(parameters, (), ('name', 'tag', 'visibility_id', 'exact', 'exclude_full', 'limit'))
    for field in ('name', 'tag', 'visibility_id'):
        if not isinstance(parameters.get(field, ''), str):
            fail('invalid_alliance_search', 'Alliance search text must be strings')
    for field in ('exact', 'exclude_full'):
        if type(parameters.get(field, False)) is not bool:
            fail('invalid_alliance_search', 'Alliance search flags must be boolean')
    limit = integer(parameters.get('limit', 10), 1, 100)
    visibility = parameters.get('visibility_id', '')
    if visibility and (rules is None or visibility not in rules['visibility']):
        fail('invalid_alliance_visibility', 'Alliance visibility is undefined')
    selected = []
    for row in view(player, rules, now)['alliances']:
        if visibility and row['visibility_id'] != visibility:
            continue
        if parameters.get('exclude_full') and row['member_count'] >= rules['maximum_members']:
            continue
        matches = True
        for field in ('name', 'tag'):
            wanted, actual = parameters.get(field, '').casefold(), row[field].casefold()
            if wanted and (actual != wanted if parameters.get('exact') else wanted not in actual):
                matches = False
        if matches:
            selected.append(row)
            if len(selected) == limit:
                break
    return {'alliances': selected, 'server_time': now}


def apply(service, player, command, parameters, now):
    rules = service.catalog.get('alliances')
    if rules is None or player['level'] < rules['required_level']:
        fail('alliance_level_required', 'Alliance access is unavailable', 409)
    fields = {'alliance.create': ('name', 'tag'),
              'alliance.join': ('alliance_id', 'allow_switch'), 'alliance.leave': ('alliance_id',),
              'alliance.update': ('alliance_id', 'name', 'tag'),
              'alliance.acknowledge': ()}
    if command not in fields:
        fail('unsupported_command', 'Alliance action is undefined', 404)
    object_fields(parameters, fields[command], ('message', 'visibility_id', 'settings')
                  if command in ('alliance.create', 'alliance.update') else ())
    row = current(player)
    target = parameters.get('alliance_id')
    if target is not None:
        identifier(target)
    switching = command in ('alliance.create', 'alliance.leave') or (command == 'alliance.join' and
                 (row is None or target != row['alliance_id']))
    if row and switching and any(raid.get('status') == 'active' and
            raid.get('alliance_id', row['alliance_id']) == row['alliance_id'] and raid.get('expires_at', 2**53) > now
            for raid in player.get('raids', {}).values()):
        fail('alliance_active_raid', 'An active raid prevents changing membership', 409)
    if command == 'alliance.create':
        if row is not None and not rules['leave_on_create']:
            fail('already_in_alliance', 'Leave the current alliance before creating another', 409)
        details = {'message': '', 'visibility_id': rules['default_visibility'], 'settings': {}, **parameters}
        validate_details(rules, details)
        create(player, details, now, service.catalog.get('raid_schedule'))
    elif command == 'alliance.join':
        if type(parameters['allow_switch']) is not bool:
            fail('invalid_alliance_switch', 'Alliance switch flag must be boolean')
        destination = player.get('alliances', {}).get(target)
        if destination is None:
            fail('alliance_not_found', 'Alliance is not in this profile', 404)
        if row != destination:
            if row is not None and not parameters['allow_switch']:
                fail('already_in_alliance', 'Alliance switch was not requested', 409)
            if not rules['visibility'][destination['visibility_id']]:
                fail('alliance_private', 'Alliance does not accept joining', 409)
            player.update(alliance_id=target, alliance_rank='owner')
            destination['joined_at'] = now
            player['alliance_donation_cooldown_until'] = now + rules['rejoin_cooldown_seconds']
    elif command in ('alliance.leave', 'alliance.update'):
        if row is None or row['alliance_id'] != target:
            fail('alliance_membership_mismatch', 'Alliance membership does not match', 409)
        if command == 'alliance.leave':
            player['alliance_id'] = None
            player.pop('alliance_rank', None)
        else:
            if player.get('alliance_rank') != 'owner':
                fail('alliance_owner_required', 'Alliance settings require ownership', 409)
            details = {key: deepcopy(row[key]) for key in ('name', 'tag', 'message', 'visibility_id', 'settings')}
            details.update({key: value for key, value in parameters.items() if key != 'alliance_id'})
            validate_details(rules, details)
            if any(other['alliance_id'] != target and other['name'].casefold() == details['name'].casefold()
                   for other in player['alliances'].values()):
                fail('alliance_name_taken', 'Alliance name is already used', 409)
            row.update(deepcopy(details))
            row['emblem'] = details['settings'].get('emblem', row['emblem'])
    else:
        if row is None:
            fail('alliance_required', 'Alliance membership is required', 409)
        row['leadership_seen'] = True
    return {'server_time': now}

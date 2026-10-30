"""UTC attendance boards, daily reward sequencing and durable award receipts."""
import calendar
from copy import deepcopy
from datetime import datetime, timezone

from .catalog import fail, identifier, integer, object_fields, rewards


def validate(definitions, catalog):
    if not isinstance(definitions, dict):
        fail('invalid_catalog', 'Calendars must be an object')
    orders = set()
    for key, definition in definitions.items():
        identifier(key)
        object_fields(definition, ('mode', 'required_level', 'order', 'rewards'),
                      ('ends_at', 'required_item_id', 'requires_alliance'))
        if definition['mode'] not in ('finite', 'repeat', 'consecutive', 'monthly'):
            fail('invalid_catalog', 'Calendar sequence mode is undefined')
        integer(definition['required_level'], 1, catalog['maximum_player_level'])
        order = integer(definition['order'], 0, 1000000)
        if order in orders:
            fail('invalid_catalog', 'Calendar processing order must be explicit and distinct')
        orders.add(order)
        if 'ends_at' in definition:
            integer(definition['ends_at'])
        if 'required_item_id' in definition:
            identifier(definition['required_item_id'])
        if 'requires_alliance' in definition and type(definition['requires_alliance']) is not bool:
            fail('invalid_catalog', 'Calendar membership requirement must be boolean')
        if not isinstance(definition['rewards'], list) or not 1 <= len(definition['rewards']) <= 4096:
            fail('invalid_catalog', 'Calendar rewards must be a bounded nonempty list')
        for bundle in definition['rewards']:
            rewards(bundle)


def clock(player, now):
    day = max([now // 86400] + [row['last_day'] for row in player.get('calendar_progress', {}).values()])
    date = datetime.fromtimestamp(day * 86400, timezone.utc)
    return day, date, calendar.monthrange(date.year, date.month)[1]


def eligible(player, definition):
    return (player['level'] >= definition['required_level'] and
            (not definition.get('required_item_id') or player['inventory'].get(definition['required_item_id'], 0) > 0) and
            (not definition.get('requires_alliance') or bool(player.get('alliance_id'))))


def board_view(player, key, definition, day, date, month_days):
    saved = player.get('calendar_progress', {}).get(key, {})
    mode = definition['mode']
    period = date.strftime('%Y-%m') if mode == 'monthly' else 'lifetime'
    step = saved.get('step', 0)
    reset = mode == 'consecutive' and saved.get('last_day') is not None and day - saved['last_day'] > 1
    if (mode == 'monthly' and saved.get('period') != period) or reset:
        step = 0
    index = step % len(definition['rewards'])
    exhausted = (mode == 'finite' and step >= len(definition['rewards'])) or (mode == 'monthly' and step >= month_days)
    available = eligible(player, definition) and day * 86400 < definition.get('ends_at', 2**53 - 1)
    return {'step': step, 'period': period, 'last_day': saved.get('last_day'), 'available': available,
            'claimable': available and not exhausted and saved.get('last_day', -1) < day,
            'next_index': None if exhausted else index, 'reset_consecutive': reset, 'mode': mode}


def view(player, definitions, now):
    day, date, month_days = clock(player, now)
    return {'boards': {key: board_view(player, key, definition, day, date, month_days)
                       for key, definition in definitions.items()},
            'day': day, 'server_time': now, 'month': date.month - 1, 'days_in_month': month_days,
            'end_of_day': (day + 1) * 86400,
            'end_of_month': (day + 1 + month_days - date.day) * 86400,
            'last_award_day': max((row['last_day'] for row in player.get('calendar_progress', {}).values()), default=None)}


def advance(service, player, parameters, awarded, now):
    object_fields(parameters, ())
    definitions = service.catalog.get('calendars')
    if definitions is None:
        fail('feature_unavailable', 'Login calendars are not configured', 404)
    day, date, month_days = clock(player, now)
    grants, bundles = [], []
    if day > now // 86400:
        return {'calendar_awards': [], 'quantity': 0, 'rewards': awarded()}
    for key, definition in sorted(definitions.items(), key=lambda item: item[1]['order']):
        state = board_view(player, key, definition, day, date, month_days)
        receipt_key = state['period'] + ':' + str(day)
        if not state['claimable'] or receipt_key in player.get('calendar_receipts', {}).get(key, {}):
            continue
        bundle = definition['rewards'][state['next_index']]
        service._reward(player, bundle)
        step = state['next_index'] + 1 if definition['mode'] == 'consecutive' else state['step'] + 1
        receipt = {'day': day, 'period': state['period'], 'step': step, 'rewards': deepcopy(bundle)}
        player.setdefault('calendar_receipts', {}).setdefault(key, {})[receipt_key] = deepcopy(receipt)
        player.setdefault('calendar_progress', {})[key] = {'step': step, 'period': state['period'], 'last_day': day}
        grants.append({'calendar_id': key, **receipt})
        bundles.append((bundle, 1))
    return {'calendar_awards': grants, 'quantity': len(grants), 'rewards': awarded(*bundles)}


def import_historic_attendance(service, player_id, archive):
    """Trusted one-time legacy calendar receipt cutover; never awards a prize.

    Call only after importing the associated original inventory/wallet balance.
    This is deliberately not a player command or HTTP endpoint. The private
    side must translate archived source board names and ISO dates before use.
    An existing public attendance record must match exactly or import fails;
    it is never silently replaced by a snapshot from another state epoch.
    """
    from .service import canonical
    if not isinstance(archive, dict) or set(archive) != {'boards'} or not isinstance(archive['boards'], dict):
        fail('invalid_legacy_calendar', 'Trusted calendar import requires board state')
    definitions = service.catalog.get('calendars', {})
    if len(archive['boards']) > len(definitions):
        fail('invalid_legacy_calendar', 'Legacy board count exceeds declared calendars')
    normalized = {}
    for key, source in archive['boards'].items():
        if identifier(key) not in definitions:
            fail('invalid_legacy_calendar', 'Legacy board has no public policy')
        object_fields(source, ('step', 'period', 'last_day', 'receipts'))
        step = integer(source['step'], 0, 1000000)
        last_day = integer(source['last_day'], 0, 2500000)
        period = source['period']
        if not isinstance(period, str) or not period or len(period) > 20:
            fail('invalid_legacy_calendar', 'Legacy period is invalid')
        if definitions[key]['mode'] == 'monthly':
            expected_period = datetime.fromtimestamp(last_day * 86400, timezone.utc).strftime('%Y-%m')
            if period != expected_period:
                fail('invalid_legacy_calendar', 'Monthly legacy board period differs from its last day')
        elif period != 'lifetime':
            fail('invalid_legacy_calendar', 'Nonmonthly legacy period must be lifetime')
        rows = source['receipts']
        if not isinstance(rows, list) or len(rows) > 100000:
            fail('invalid_legacy_calendar', 'Legacy daily receipts must be bounded')
        records, newest = {}, -1
        for receipt in rows:
            object_fields(receipt, ('day', 'period', 'step'))
            day = integer(receipt['day'], 0, 2500000)
            count = integer(receipt['step'], 1, 1000000)
            observed_period = receipt['period']
            if not isinstance(observed_period, str) or len(observed_period) > 20:
                fail('invalid_legacy_calendar', 'Legacy receipt period is invalid')
            if definitions[key]['mode'] == 'monthly':
                expected = datetime.fromtimestamp(day * 86400, timezone.utc).strftime('%Y-%m')
                if observed_period != expected:
                    fail('invalid_legacy_calendar', 'Legacy monthly receipt does not match UTC day')
            elif observed_period != 'lifetime':
                fail('invalid_legacy_calendar', 'Legacy attendance receipt period is invalid')
            receipt_key = observed_period + ':' + str(day)
            if receipt_key in records or day > last_day:
                fail('invalid_legacy_calendar', 'Legacy attendance has duplicate or future receipts')
            records[receipt_key] = {'day': day, 'period': observed_period,
                                   'step': count, 'imported_legacy': True}
            newest = max(newest, day)
        if newest != last_day or not rows:
            fail('invalid_legacy_calendar', 'Legacy attendance day lacks a source receipt')
        if definitions[key]['mode'] != 'monthly':
            latest = max(rows, key=lambda row: row['day'])
            if latest['step'] != step:
                fail('invalid_legacy_calendar', 'Legacy sequence step and latest receipt disagree')
        elif max((row['step'] for row in rows if row['period'] == period), default=-1) != step:
            fail('invalid_legacy_calendar', 'Legacy monthly sequence step is inconsistent')
        normalized[key] = ({'step': step, 'period': period, 'last_day': last_day}, records)
    with service.transaction() as connection:
        player = service._player(connection, player_id)
        old_progress = player.get('calendar_progress', {})
        old_receipts = player.get('calendar_receipts', {})
        changes = {}
        for key, (progress, receipts) in normalized.items():
            if key in old_progress or key in old_receipts:
                if old_progress.get(key) != progress or old_receipts.get(key) != receipts:
                    fail('legacy_calendar_conflict', 'Public attendance history conflicts with legacy record', 409)
                continue
            changes[key] = len(receipts)
        if changes:
            for key in changes:
                progress, receipts = normalized[key]
                player.setdefault('calendar_progress', {})[key] = progress
                player.setdefault('calendar_receipts', {})[key] = receipts
            player['version'] += 1
            connection.execute('UPDATE players SET data=? WHERE id=?', (canonical(player), player_id))
        return {'imported_receipts': changes, 'boards': sorted(normalized)}

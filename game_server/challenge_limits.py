"""Server-owned UTC section attempt limits migrated from legacy campaign state.

The private original-client reader supplies only neutral section/mission IDs
and authored caps. The server owns consumed attempts, UTC reset and receipts.
"""
from .catalog import fail, identifier, integer


def validate(catalog):
    limits = catalog.get('challenge_limits', {})
    if not isinstance(limits, dict) or len(limits) > 128:
        fail('invalid_catalog', 'Challenge attempt sections must be bounded')
    for section, limit in limits.items():
        identifier(section)
        integer(limit, 1, 100)
    used = set()
    for mission in catalog['missions'].values():
        section = mission.get('challenge_section')
        if section is not None:
            if identifier(section) not in limits:
                fail('invalid_catalog', 'Mission challenge section is undefined')
            used.add(section)
    if set(limits) != used:
        fail('invalid_catalog', 'Challenge limits must correspond to configured missions')


def view(player, catalog, now):
    day_start = (now // 86400) * 86400
    recorded = player.get('challenge_attempts', {})
    spent = recorded.get('used', {}) if recorded.get('day_start') == day_start else {}
    return {'day_start': day_start, 'next_reset': day_start + 86400,
            'sections': {section: {'daily_limit': limit,
                                   'used': spent.get(section, 0),
                                   'remaining': limit - spent.get(section, 0)}
                         for section, limit in catalog.get('challenge_limits', {}).items()}}


def require(player, catalog, mission, count, now):
    section = mission.get('challenge_section')
    if section is None:
        return
    remaining = view(player, catalog, now)['sections'][section]['remaining']
    if remaining < count:
        fail('challenge_daily_limit_reached', 'Challenge section attempt limit has been reached', 409)


def consume(player, catalog, mission, count, now):
    section = mission.get('challenge_section')
    if section is None:
        return
    require(player, catalog, mission, count, now)
    day_start = now // 86400 * 86400
    record = player.setdefault('challenge_attempts', {'day_start': day_start, 'used': {}})
    if record['day_start'] != day_start:
        record.update(day_start=day_start, used={})
    record['used'][section] = integer(record['used'].get(section, 0) + count, 1,
                                      catalog['challenge_limits'][section])

"""Authoritative campaign/challenge squad restrictions, independent of native rules.

The original content reader translates source-specific traits/filters to
neutral character identifiers. The public service alone evaluates owned
character progression and authorizes a mission team before spending energy.
"""

from datetime import datetime

from .catalog import fail, identifier, integer, object_fields


FILTER_FIELDS = ('character_ids', 'excluded_character_ids', 'minimum_level',
                 'minimum_rank', 'minimum_tier', 'minimum_promotion_rank')


def validate_filter(definition, characters):
    object_fields(definition, (), FILTER_FIELDS)
    for field in ('character_ids', 'excluded_character_ids'):
        if field not in definition:
            continue
        value = definition[field]
        if not isinstance(value, list) or len(value) > len(characters):
            fail('invalid_catalog', 'Mission character filter must be a bounded list')
        for character in value:
            if identifier(character) not in characters:
                fail('invalid_catalog', 'Mission filter references an undefined character')
        if len(set(value)) != len(value):
            fail('invalid_catalog', 'Mission filter characters must be distinct')
    if set(definition.get('character_ids', ())) & set(definition.get('excluded_character_ids', ())):
        fail('invalid_catalog', 'Mission filter cannot allow and exclude the same character')
    for field in ('minimum_level', 'minimum_rank', 'minimum_tier', 'minimum_promotion_rank'):
        if field in definition:
            integer(definition[field], 1 if field != 'minimum_promotion_rank' else 0, 10000)


def validate(definition, catalog):
    object_fields(definition, (), ('minimum_mission_stars', 'must_include',
                                   'each_character', 'any_character', 'minimum_matching',
                                   'minimum_commander_level', 'local_weekdays',
                                   'one_time', 'first_entry_minimum_mission_stars',
                                   'scopes', 'event_access'))
    missions, characters = catalog['missions'], catalog['characters']
    if 'event_access' in definition:
        access = definition['event_access']
        object_fields(access, ('section_mission_ids',), ('permanent_minimum_level', 'windows'))
        if ('permanent_minimum_level' in access) == ('windows' in access):
            fail('invalid_catalog', 'Event access must be permanent or window-bound')
        section = access['section_mission_ids']
        if not isinstance(section, list) or not 1 <= len(section) <= len(missions):
            fail('invalid_catalog', 'Event section must list its complete authored missions')
        for mission_id in section:
            if identifier(mission_id) not in missions:
                fail('invalid_catalog', 'Event section mission is undefined')
        if len(set(section)) != len(section):
            fail('invalid_catalog', 'Event section missions must be distinct')
        if 'permanent_minimum_level' in access:
            integer(access['permanent_minimum_level'], 1, catalog['maximum_player_level'])
        else:
            windows = access['windows']
            if not isinstance(windows, list) or len(windows) > 1000:
                fail('invalid_catalog', 'Event windows must be an authored bounded list')
            for window in windows:
                object_fields(window, ('starts_at', 'ends_at', 'required_level'))
                start = integer(window['starts_at'])
                end = integer(window['ends_at'])
                if end <= start:
                    fail('invalid_catalog', 'Event window end must follow start')
                integer(window['required_level'], 1, catalog['maximum_player_level'])
    if 'minimum_commander_level' in definition:
        integer(definition['minimum_commander_level'], 1, catalog['maximum_player_level'])
    if 'one_time' in definition and type(definition['one_time']) is not bool:
        fail('invalid_catalog', 'One-time challenge eligibility must be a boolean')
    if 'local_weekdays' in definition:
        weekdays = definition['local_weekdays']
        if (not isinstance(weekdays, list) or not 1 <= len(weekdays) <= 7 or
                any(type(day) is not int or not 0 <= day <= 6 for day in weekdays) or
                len(set(weekdays)) != len(weekdays)):
            fail('invalid_catalog', 'Challenge local weekdays must be distinct Sunday=0 through Saturday=6')
    if 'scopes' in definition:
        scopes = definition['scopes']
        if not isinstance(scopes, list) or not 1 <= len(scopes) <= 3:
            fail('invalid_catalog', 'Mission scope filters must be bounded to section, chapter, node')
        for scope in scopes:
            if not isinstance(scope, dict) or 'scopes' in scope:
                fail('invalid_catalog', 'Nested mission scope filters are unsupported')
            validate(scope, catalog)
    for field, collection, lower, upper in (
            ('minimum_mission_stars', missions, 1, 3),
            ('first_entry_minimum_mission_stars', missions, 1, 3),
            ('must_include', characters, None, None)):
        if field not in definition:
            continue
        value = definition[field]
        if field in ('minimum_mission_stars', 'first_entry_minimum_mission_stars'):
            if not isinstance(value, dict) or len(value) > len(missions):
                fail('invalid_catalog', 'Mission prerequisites must be a bounded object')
            for key, stars in value.items():
                if identifier(key) not in collection:
                    fail('invalid_catalog', 'Mission prerequisite refers to an undefined mission')
                integer(stars, lower, upper)
        else:
            if not isinstance(value, list) or len(value) > 5:
                fail('invalid_catalog', 'Required squad heroes must be a bounded list')
            for key in value:
                if identifier(key) not in collection:
                    fail('invalid_catalog', 'Required squad hero is undefined')
            if len(set(value)) != len(value):
                fail('invalid_catalog', 'Required squad heroes must be distinct')
    if 'each_character' in definition:
        validate_filter(definition['each_character'], characters)
    if 'any_character' in definition:
        alternatives = definition['any_character']
        if not isinstance(alternatives, list) or not 1 <= len(alternatives) <= 32:
            fail('invalid_catalog', 'Mission alternatives require a bounded list')
        for option in alternatives:
            validate_filter(option, characters)
    if 'minimum_matching' in definition:
        integer(definition['minimum_matching'], 1, 5)
        if 'any_character' not in definition:
            fail('invalid_catalog', 'Minimum matching count needs a squad filter')


def matches(character_id, state, definition):
    if ('character_ids' in definition and character_id not in definition['character_ids'] or
            character_id in definition.get('excluded_character_ids', ())):
        return False
    checks = (('minimum_level', 'level'), ('minimum_rank', 'rank'),
              ('minimum_tier', 'tier'), ('minimum_promotion_rank', 'promotion_rank'))
    return all(state.get(attribute, 0 if attribute == 'promotion_rank' else 1) >= definition[field]
               for field, attribute in checks if field in definition)


def require(player, definition, selected, now, mission_id):
    """Authorize a battle squad against the selected authored mission rules."""
    rule = definition.get('eligibility')
    if rule is None:
        return
    _require_rule(player, rule, selected, now, mission_id)


def require_sweep(player, definition, now, mission_id):
    """Recheck the original replay gates without requiring a new combat squad.

    An earned three-star battle is the squad proof for a sweep. The original
    registry still rechecked the current commander/day/mission prerequisites,
    but allowed empty squads for ordinary challenge and campaign replays.
    """
    rule = definition.get('eligibility')
    if rule is not None:
        _require_rule(player, rule, None, now, mission_id)


def require_event_access(player, definition, now):
    """Authorize a source-backed event section independently of UI visibility."""
    access = definition.get('eligibility', {}).get('event_access')
    if access is None:
        return
    if all(player['missions'].get(key, {}).get('best_stars', 0) >= 1
           for key in access['section_mission_ids']):
        fail('event_challenge_section_complete', 'All event challenge nodes were already cleared', 409)
    if 'permanent_minimum_level' in access:
        active = player['level'] >= access['permanent_minimum_level']
    else:
        active = any(row['starts_at'] <= now < row['ends_at'] and
                     player['level'] >= row['required_level']
                     for row in access['windows'])
    if not active:
        fail('event_challenge_inactive', 'Original event availability gate is closed', 409)


def _require_rule(player, rule, selected, now, mission_id):
    # Original CampaignRegistry.eligible_challenge checks an event's saved
    # clear state before authorizing another attempt; this decision is
    # authoritative server state, not native event UI configuration.
    if 'event_access' in rule:
        require_event_access(player, {'eligibility': {'event_access': rule['event_access']}}, now)
    if rule.get('one_time') and player['missions'].get(mission_id, {}).get('best_stars', 0) > 0:
        fail('event_challenge_already_completed', 'One-time event challenge has already been cleared', 409)
    if 'minimum_commander_level' in rule and player['level'] < rule['minimum_commander_level']:
        fail('mission_commander_level', 'Required commander level has not been earned', 409)
    if 'local_weekdays' in rule:
        # Legacy campaign.CampaignRegistry.eligible_challenge used process-local
        # datetime.fromtimestamp(now).isoweekday() % 7, not UTC weekday.
        if datetime.fromtimestamp(now).isoweekday() % 7 not in rule['local_weekdays']:
            fail('challenge_not_active_today', 'Challenge is not available on this local weekday', 409)
    for previous_id, stars in rule.get('minimum_mission_stars', {}).items():
        if player['missions'].get(previous_id, {}).get('best_stars', 0) < stars:
            fail('mission_prerequisite_required', 'Required mission stars have not been earned', 409)
    # The original campaign sequential gate only applies until this mission
    # first has a saved record. Earned replay rights are not revoked if an
    # old imported record lacks the usual preceding node.
    if mission_id not in player['missions']:
        for previous_id, stars in rule.get('first_entry_minimum_mission_stars', {}).items():
            if player['missions'].get(previous_id, {}).get('best_stars', 0) < stars:
                fail('previous_mission_required', 'Previous campaign mission must be cleared first', 409)
    if selected is not None:
        if not set(rule.get('must_include', ())) <= set(selected):
            fail('mission_required_character', 'A required mission character is missing', 409)
        roster = player['characters']
        for key in selected:
            if 'each_character' in rule and not matches(key, roster[key], rule['each_character']):
                fail('mission_squad_restriction', 'A selected character violates mission eligibility', 409)
        if 'any_character' in rule:
            matching = sum(any(matches(key, roster[key], option) for option in rule['any_character'])
                           for key in selected)
            if matching < rule.get('minimum_matching', len(selected)):
                fail('mission_squad_restriction', 'Not enough characters satisfy the mission filter', 409)
    for scope in rule.get('scopes', []):
        _require_rule(player, scope, selected, now, mission_id)

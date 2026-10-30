"""Server-owned, versioned gameplay definitions; no client file formats."""
from copy import deepcopy
import re

IDENTIFIER = re.compile(r'[A-Za-z0-9_.-]{1,64}\Z')
MAX_AMOUNT = 9_000_000_000_000_000


class DomainError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def fail(code, message, status=400):
    raise DomainError(code, message, status)


def object_fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        fail('invalid_fields', 'Object fields do not match the API schema')


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail('invalid_identifier', 'Identifier must contain 1–64 letters, digits, dots, underscores or hyphens')
    return value


def integer(value, minimum=0, maximum=MAX_AMOUNT):
    if type(value) is not int or not minimum <= value <= maximum:
        fail('invalid_amount', 'Integer is outside the allowed range')
    return value


def amounts(value):
    if not isinstance(value, dict):
        fail('invalid_amounts', 'Amounts must be an object')
    for key, amount in value.items():
        identifier(key)
        integer(amount)


def rewards(value):
    object_fields(value, (), ('wallet', 'inventory', 'experience', 'signals'))
    amounts(value.get('wallet', {}))
    amounts(value.get('inventory', {}))
    integer(value.get('experience', 0))
    if 'signals' in value:
        signals = value['signals']
        if not isinstance(signals, list) or len(signals) > 100:
            fail('invalid_rewards', 'Reward signals must be a bounded list')
        for signal in signals:
            object_fields(signal, ('labels', 'quantity'))
            labels = signal['labels']
            if not isinstance(labels, list) or not 1 <= len(labels) <= 64:
                fail('invalid_rewards', 'Signals require 1–64 distinct labels')
            for label in labels:
                identifier(label)
            if len(set(labels)) != len(labels):
                fail('invalid_rewards', 'Signal labels must be distinct')
            integer(signal['quantity'], 1)


def validate_catalog(value):
    object_fields(value, ('schema_version', 'starting_wallet', 'starting_characters', 'characters',
                          'missions', 'offers', 'experience_per_level', 'maximum_player_level'),
                  ('towers', 'roguelite', 'free_crates', 'squad_groups', 'appearance', 'guides', 'development', 'enhancements', 'promotions', 'objectives', 'passes', 'access_rules', 'experience_thresholds', 'level_rewards', 'resource_rules', 'calendars', 'milestones', 'alliances', 'alliance_economy', 'raids', 'raid_schedule', 'ranked_arena', 'seasonal_boost', 'blitz', 'dark_dimension', 'loot_tables', 'loot_products', 'raid_energy', 'mail_messages', 'challenge_limits', 'client_settings'))
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        fail('unsupported_catalog', 'Catalog schema version must be 1')
    amounts(value['starting_wallet'])
    integer(value['experience_per_level'], 1)
    integer(value['maximum_player_level'], 1, 10000)
    from .player_progression import validate as validate_player_progression
    validate_player_progression(value)
    if 'resource_rules' in value:
        from .resources import validate as validate_resources
        validate_resources(value['resource_rules'], value)
    if 'calendars' in value:
        from .calendars import validate as validate_calendars
        validate_calendars(value['calendars'], value)
    for category in ('characters', 'missions', 'offers'):
        if not isinstance(value[category], dict):
            fail('invalid_catalog', 'Catalog collections must be objects')
        for key in value[category]:
            identifier(key)
    for definition in value['characters'].values():
        object_fields(definition, ('maximum_level', 'training_cost', 'power_per_level'))
        integer(definition['maximum_level'], 1, 10000)
        amounts(definition['training_cost'])
        integer(definition['power_per_level'], 1)
    amounts(value['starting_characters'])
    for key, level in value['starting_characters'].items():
        if key not in value['characters']:
            fail('invalid_catalog', 'Starting character is undefined')
        integer(level, 1, value['characters'][key]['maximum_level'])
    for key, definition in value['missions'].items():
        object_fields(definition, ('required_level', 'cost', 'rewards'), ('prerequisite', 'first_clear_rewards', 'challenge_section', 'eligibility'))
        integer(definition['required_level'], 1)
        amounts(definition['cost'])
        rewards(definition['rewards'])
        rewards(definition.get('first_clear_rewards', {}))
        prerequisite = definition.get('prerequisite')
        if prerequisite is not None and (prerequisite not in value['missions'] or prerequisite == key):
            fail('invalid_catalog', 'Mission prerequisite is invalid')
        if 'eligibility' in definition:
            from .mission_eligibility import validate as validate_mission_eligibility
            validate_mission_eligibility(definition['eligibility'], value)
    for key in value['missions']:
        seen = set()
        current = key
        while current is not None:
            if current in seen:
                fail('invalid_catalog', 'Mission prerequisites contain a cycle')
            seen.add(current)
            current = value['missions'][current].get('prerequisite')
    if 'challenge_limits' in value or any('challenge_section' in row for row in value['missions'].values()):
        from .challenge_limits import validate as validate_challenge_limits
        validate_challenge_limits(value)
    if 'client_settings' in value:
        from .client_settings import validate as validate_client_settings
        validate_client_settings(value['client_settings'])
    for definition in value['offers'].values():
        object_fields(definition, ('cost', 'rewards', 'purchase_limit'), ('reset_policy',))
        amounts(definition['cost'])
        rewards(definition['rewards'])
        integer(definition['purchase_limit'], 1)
        if 'reset_policy' in definition and definition['reset_policy'] != 'utc_day_once':
            fail('invalid_catalog', 'Offer reset policy is unsupported')
    if 'free_crates' in value:
        from .free_crates import validate as validate_free_crates
        validate_free_crates(value['free_crates'])
    validate_towers(value.get('towers', {}), value['characters'])
    from .squads import validate as validate_squads
    validate_squads(value.get('squad_groups', {}))
    from .player_preferences import validate as validate_preferences
    validate_preferences(value)
    if 'development' in value:
        from .development import validate as validate_development
        validate_development(value['development'], value['characters'])
    if 'enhancements' in value:
        from .enhancements import validate as validate_enhancements
        validate_enhancements(value['enhancements'], value['characters'])
    if 'roguelite' in value:
        # Shards reference the validated recipient's recruitment payment.
        from .roguelite import validate as validate_roguelite
        validate_roguelite(value['roguelite'], value)
    if 'promotions' in value:
        from .promotions import validate as validate_promotions
        validate_promotions(value['promotions'], value['characters'])
    if 'objectives' in value:
        from .objectives import validate as validate_objectives
        validate_objectives(value['objectives'], value)
    if 'passes' in value:
        from .passes import validate as validate_passes
        validate_passes(value['passes'], value)
    if 'milestones' in value:
        from .milestones import validate as validate_milestones
        validate_milestones(value['milestones'], value)
    if 'alliances' in value:
        from .alliances import validate as validate_alliances
        validate_alliances(value['alliances'], value)
    if 'mail_messages' in value:
        from .mail import validate as validate_mail
        validate_mail(value['mail_messages'])
    if 'raid_schedule' in value:
        from .raid_schedule import validate as validate_raid_schedule
        validate_raid_schedule(value['raid_schedule'], value)
    if 'ranked_arena' in value:
        from .ranked_arena import validate as validate_arena
        validate_arena(value['ranked_arena'], value)
    if 'seasonal_boost' in value:
        from .seasonal_boost import validate as validate_boost
        validate_boost(value['seasonal_boost'], value)
    if 'blitz' in value:
        from .blitz import validate as validate_blitz
        validate_blitz(value['blitz'], value)
    if 'dark_dimension' in value:
        from .dark_dimension import validate as validate_dark_dimension
        validate_dark_dimension(value['dark_dimension'], value)
    if 'raid_energy' in value:
        from .raid_energy import validate as validate_raid_energy
        validate_raid_energy(value['raid_energy'], value)
    if 'alliance_economy' in value:
        from .alliance_economy import validate as validate_alliance_economy
        validate_alliance_economy(value['alliance_economy'], value)
    if 'loot_tables' in value:
        from .loot import validate as validate_loot
        validate_loot(value['loot_tables'])
    if 'loot_products' in value:
        from .loot_products import validate as validate_loot_products
        validate_loot_products(value['loot_products'], value)
    if 'raids' in value:
        from .raids import validate as validate_raids
        validate_raids(value['raids'], value)
    if 'access_rules' in value:
        from .access import validate as validate_access
        validate_access(value['access_rules'], value)
    return deepcopy(value)


def validate_towers(towers, characters):
    """Application definitions only; encounter rendering belongs to an adapter."""
    if not isinstance(towers, dict):
        fail('invalid_catalog', 'Towers must be an object')
    for tower_id, tower in towers.items():
        identifier(tower_id)
        object_fields(tower, ('starts_at', 'ends_at', 'cooldown_seconds', 'team_size',
                              'difficulties', 'milestones'))
        integer(tower['starts_at'])
        integer(tower['ends_at'], tower['starts_at'] + 1)
        integer(tower['cooldown_seconds'], 1)
        integer(tower['team_size'], 1, 5)
        if not isinstance(tower['difficulties'], dict) or not tower['difficulties']:
            fail('invalid_catalog', 'Tower difficulties must be a nonempty object')
        for difficulty_id, floors in tower['difficulties'].items():
            identifier(difficulty_id)
            if not isinstance(floors, list) or not 1 <= len(floors) <= 1000:
                fail('invalid_catalog', 'Tower difficulty must define 1–1000 floors')
            previous_offset = 0
            for floor in floors:
                object_fields(floor, ('encounter_id', 'unlock_offset', 'reset_cooldowns',
                                     'score', 'cost', 'rewards'), ('eligible_characters',))
                identifier(floor['encounter_id'])
                previous_offset = integer(floor['unlock_offset'], previous_offset,
                                          tower['ends_at'] - tower['starts_at'] - 1)
                if type(floor['reset_cooldowns']) is not bool:
                    fail('invalid_catalog', 'Cooldown reset must be a boolean')
                integer(floor['score'])
                amounts(floor['cost'])
                rewards(floor['rewards'])
                allowed = floor.get('eligible_characters')
                if allowed is not None and (not isinstance(allowed, list) or
                        any(not isinstance(x, str) or x not in characters for x in allowed) or
                        len(allowed) != len(set(allowed))):
                    fail('invalid_catalog', 'Floor eligibility must contain distinct defined characters')
        if not isinstance(tower['milestones'], list):
            fail('invalid_catalog', 'Tower milestones must be a list')
        seen, points = set(), 0
        for milestone in tower['milestones']:
            object_fields(milestone, ('milestone_id', 'points', 'rewards'))
            key = identifier(milestone['milestone_id'])
            if key in seen:
                fail('invalid_catalog', 'Tower milestone IDs must be unique')
            seen.add(key)
            points = integer(milestone['points'], points + 1)
            rewards(milestone['rewards'])


# Authored demonstration content: these identifiers and values are fixtures for
# this API, not imports or reconstructions of any game's files or responses.
DEMO_CATALOG = {
    'schema_version': 1, 'experience_per_level': 100, 'maximum_player_level': 100,
    'starting_wallet': {'credits': 100, 'energy': 20},
    'starting_characters': {'scout': 1, 'guardian': 1},
    'characters': {
        'scout': {'maximum_level': 10, 'training_cost': {'credits': 10}, 'power_per_level': 12},
        'guardian': {'maximum_level': 10, 'training_cost': {'credits': 15}, 'power_per_level': 15},
    },
    'missions': {
        'training.1': {'required_level': 1, 'cost': {'energy': 2},
                       'rewards': {'wallet': {'credits': 5}, 'experience': 10},
                       'first_clear_rewards': {'inventory': {'badge': 1}}},
        'training.2': {'required_level': 1, 'cost': {'energy': 3}, 'prerequisite': 'training.1',
                       'rewards': {'wallet': {'credits': 8}, 'experience': 15}},
    },
    'offers': {'training-kit': {'cost': {'credits': 20}, 'rewards': {'inventory': {'training-kit': 1}},
                              'purchase_limit': 2}},
}

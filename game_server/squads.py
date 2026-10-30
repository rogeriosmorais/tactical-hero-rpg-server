"""Application-owned saved teams, earned capacity and atomic validation."""
from copy import deepcopy

from .catalog import fail, identifier, integer, object_fields


def validate(groups):
    if not isinstance(groups, dict):
        fail('invalid_catalog', 'Squad groups must be an object')
    for group_id, definition in groups.items():
        identifier(group_id)
        object_fields(definition, ('required_level', 'capacity', 'team_size'), ('capacity_item',))
        integer(definition['required_level'], 1, 10000)
        integer(definition['capacity'], 1, 1000)
        integer(definition['team_size'], 1, 5)
        if 'capacity_item' in definition:
            identifier(definition['capacity_item'])


def capacity(player, definition):
    maximum = definition['capacity']
    if 'capacity_item' in definition:
        return min(maximum, player['inventory'].get(definition['capacity_item'], 0))
    return maximum


def view(player, definitions):
    return {group_id: {'teams': deepcopy(player.get('squads', {}).get(group_id, [])),
                       'available': player['level'] >= definition['required_level'],
                       'capacity': capacity(player, definition),
                       'team_size': definition['team_size']}
            for group_id, definition in definitions.items()}


def save(player, definitions, parameters, now):
    object_fields(parameters, ('groups',))
    groups = parameters['groups']
    if not isinstance(groups, dict) or not 1 <= len(groups) <= 100:
        fail('invalid_squads', 'Save requires 1–100 group replacements')
    # Validate every replacement before applying any of them. The enclosing
    # player/receipt transaction also protects concurrent requests and retries.
    for group_id, teams in groups.items():
        identifier(group_id)
        definition = definitions.get(group_id)
        if definition is None:
            fail('squad_group_not_found', 'Squad group does not exist', 404)
        if player['level'] < definition['required_level']:
            fail('squad_group_locked', 'Squad group level requirement is not met', 409)
        if not isinstance(teams, list) or len(teams) > capacity(player, definition):
            fail('squad_capacity', 'Squad group capacity exceeded', 409)
        for team in teams:
            if not isinstance(team, list) or len(team) > definition['team_size']:
                fail('invalid_team', 'Saved team exceeds its group size')
            selected = []
            for character in team:
                # Null preserves an empty formation slot without a wire alias.
                if character is not None:
                    identifier(character)
                    if character not in player['characters']:
                        fail('character_not_owned', 'Saved team character is not owned', 409)
                    selected.append(character)
            if len(selected) != len(set(selected)):
                fail('invalid_team', 'Saved team contains duplicate characters')
    player.setdefault('squads', {}).update(deepcopy(groups))
    player['squads_updated_at'] = now
    return {'squads': view(player, definitions), 'updated_at': now}

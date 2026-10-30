"""Neutral replayable runs. All writes share GameService's receipt transaction."""
from copy import deepcopy
import hashlib
import json
import random
import secrets

from .catalog import fail, identifier, integer, object_fields, rewards


def validate(rules, catalog):
    object_fields(rules, ('minimum_team_size', 'maximum_team_size', 'enemy_teams',
        'enemy_base_level', 'enemy_level_step', 'victory_rewards', 'defeat_rewards',
        'shard_pool', 'choice_count'), ('opening_enemy_team', 'reward_bands'))
    integer(rules['minimum_team_size'], 1, 1)
    integer(rules['maximum_team_size'], 5, 5)
    integer(rules['enemy_base_level'], 1)
    integer(rules['enemy_level_step'], 0)
    if not isinstance(rules['enemy_teams'], dict) or not 1 <= len(rules['enemy_teams']) <= 64:
        fail('invalid_catalog', 'Roguelite requires 1–64 enemy templates')
    for key, team in rules['enemy_teams'].items():
        identifier(key)
        if (not isinstance(team, list) or len(team) != 5 or
                any(not isinstance(hero, str) or hero not in catalog['characters'] for hero in team)):
            fail('invalid_catalog', 'Each enemy template requires five declared actors')
        if any(rules['enemy_base_level'] > catalog['characters'][hero]['maximum_level'] for hero in team):
            fail('invalid_catalog', 'Initial enemy level exceeds a character maximum')
    for field in ('victory_rewards', 'defeat_rewards'):
        rewards(rules[field])
    if 'opening_enemy_team' in rules:
        team = rules['opening_enemy_team']
        if (not isinstance(team, list) or not 1 <= len(team) <= 5 or
                any(not isinstance(hero, str) or hero not in catalog['characters'] for hero in team)):
            fail('invalid_catalog', 'Opening encounter requires 1–5 declared actors')
        if any(rules['enemy_base_level'] > catalog['characters'][hero]['maximum_level'] for hero in team):
            fail('invalid_catalog', 'Initial enemy level exceeds a character maximum')
    pool = rules['shard_pool']
    if not isinstance(pool, list) or not 1 <= len(pool) <= 500:
        fail('invalid_catalog', 'Shard pool requires 1–500 recipients')
    integer(rules['choice_count'], 1, min(3, len(pool)))
    recipients, items = set(), set()
    development = catalog.get('development', {}).get('characters', {})
    for offer in pool:
        object_fields(offer, ('character_id', 'item_id', 'quantity'),
                      ('weight', 'era', 'affinities'))
        hero, item = identifier(offer['character_id']), identifier(offer['item_id'])
        integer(offer['quantity'], 1)
        integer(offer.get('weight', 100), 1, 1000)
        if 'era' in offer:
            integer(offer['era'], 0, 100)
        affinities = offer.get('affinities', [])
        if (not isinstance(affinities, list) or len(affinities) > 20 or
                any(not isinstance(key, str) or key not in catalog['characters'] for key in affinities)
                or len(set(affinities)) != len(affinities)):
            fail('invalid_catalog', 'Shard affinity must use distinct known characters')
        if hero not in catalog['characters'] or hero in recipients or item in items:
            fail('invalid_catalog', 'Shard recipients and their items must be distinct declared characters')
        payment = development.get(hero, {}).get('recruitment', {}).get('inventory', {})
        if payment.get(item, 0) <= 0 or any(
                other != hero and item in definition['recruitment'].get('inventory', {})
                for other, definition in development.items()):
            fail('invalid_catalog', 'A shard item must recruit only its stated recipient')
        recipients.add(hero)
        items.add(item)
    if 'reward_bands' in rules:
        bands = rules['reward_bands']
        if not isinstance(bands, list) or not 1 <= len(bands) <= 500:
            fail('invalid_catalog', 'Reward bands require 1–500 ordered entries')
        previous = 0
        for band in bands:
            object_fields(band, ('minimum_enemy_level', 'victory_rewards', 'shard_multiplier'))
            level = integer(band['minimum_enemy_level'], previous + 1)
            if previous == 0 and level != 1:
                fail('invalid_catalog', 'Reward bands must begin at enemy level one')
            previous = level
            rewards(band['victory_rewards'])
            multiplier = integer(band['shard_multiplier'], 1)
            for offer in pool:
                integer(offer['quantity'] * multiplier)
    if len(catalog['starting_characters']) < rules['minimum_team_size']:
        fail('invalid_catalog', 'Starting roster cannot field a legal Roguelite squad')


def definition(service):
    rules = service.catalog.get('roguelite')
    if rules is None:
        fail('roguelite_unavailable', 'No Roguelite is configured', 404)
    return rules


def progress(player):
    return player.setdefault('roguelite', {'active_run': None, 'last_run': None})


def view(service, player):
    definition(service)
    return {'available': True, 'roguelite': deepcopy(player.get('roguelite',
        {'active_run': None, 'last_run': None}))}


def run_for(player, run_id):
    run = player.get('roguelite', {}).get('active_run')
    if run is None or run['run_id'] != identifier(run_id):
        fail('roguelite_run_inactive', 'Requested run is not active', 409)
    return run


def rng(seed, purpose, identity):
    return random.Random(hashlib.sha256(f'{seed}:{purpose}:{identity}'.encode()).hexdigest())


def upgrade_strength(character):
    """Neutral reward affinity score from earned character progression.

    The base power field follows level in the public model; recruitment rank,
    equipped gear and upgraded abilities need to influence affinity odds even
    before a downstream native stat rebuild changes displayed combat power.
    This score only weights offers. It never changes combat actor stats.
    """
    base = integer(character['power'], 1)
    rank = integer(character.get('rank', 1), 1, 100)
    tier = integer(character.get('tier', 1), 1, 100)
    abilities = character.get('abilities', [])
    if not isinstance(abilities, list):
        fail('invalid_character', 'Character ability progression must be a list')
    upgraded = sum(integer(level, 0, 100) - 1 for level in abilities if level >= 1)
    equipment = character.get('equipment', {})
    if not isinstance(equipment, dict):
        fail('invalid_character', 'Character equipment progression must be an object')
    factor = 100 + 25 * (rank - 1) + 15 * (tier - 1) + 8 * upgraded + 5 * len(equipment)
    return base * factor


def weighted_shard_options(player, rules, catalog, seed, identity):
    """Draw distinct permanent rewards with no hardcoded option categories.

    Every eligible shard character always retains positive probability.
    Optional neutral source-authored era/affinity hints vary probabilities
    with actual player level and upgraded roster, not with a fixed 3-slot
    partition or a per-character percentage assumed to be large.
    """
    roster = player['characters']
    strengths = {hero: upgrade_strength(state) for hero, state in roster.items()}
    maximum_power = max(strengths.values(), default=1)
    level_position = 100 * (player['level'] - 1) // max(1, catalog['maximum_player_level'] - 1)
    options = list(rules['shard_pool'])
    weights = []
    for offer in options:
        weight = offer.get('weight', 100)
        closeness = 50 if 'era' not in offer else 100 - abs(offer['era'] - level_position)
        weight *= 100 + 5 * closeness  # Unspecified era is neutral, not penalized
        affinity_power = max((strengths[hero] for hero in offer.get('affinities', [])
                              if hero in strengths), default=0)
        weight *= 100 + 400 * affinity_power // max(1, maximum_power)
        if offer['character_id'] in roster:
            weight *= 2  # Upgrading an owned character is possible, not forced
        weights.append(weight)
    draw = rng(seed, 'choice', identity)
    issued = []
    for _ in range(rules['choice_count']):
        total = sum(weights)
        ticket = draw.randrange(total)
        running = 0
        for index, weight in enumerate(weights):
            running += weight
            if ticket < running:
                issued.append(deepcopy(options.pop(index)))
                weights.pop(index)
                break
    return issued


def encounter(service, rules, run):
    template = rng(run['seed'], 'encounter', run['floor']).choice(sorted(rules['enemy_teams']))
    team = rules['enemy_teams'][template]
    if run['floor'] == 1 and 'opening_enemy_team' in rules:
        template, team = 'opening', rules['opening_enemy_team']
    enemies = []
    for hero in team:
        character = service.catalog['characters'][hero]
        level = min(character['maximum_level'], rules['enemy_base_level'] +
                    (run['floor'] - 1) * rules['enemy_level_step'])
        state = {'level': level, 'power': integer(level * character['power_per_level'])}
        development = service.catalog.get('development', {}).get('characters', {}).get(hero)
        if development:
            from .development import initialize
            initialize(state, development)
        enemies.append({'character_id': hero, 'state': state})
    identity = hashlib.sha256(f"{run['run_id']}:{run['floor']}".encode()).hexdigest()[:32]
    result = {'encounter_id': identity, 'template_id': template, 'floor': run['floor'], 'enemies': enemies}
    result.update(reward_snapshot(rules, enemies))
    return result


def reward_snapshot(rules, enemies):
    """Freeze authored rewards from opposition, never from the earning account."""
    level = min(enemy['state']['level'] for enemy in enemies)
    band = {'minimum_enemy_level': 1, 'victory_rewards': rules['victory_rewards'], 'shard_multiplier': 1}
    for candidate in rules.get('reward_bands', []):
        if candidate['minimum_enemy_level'] > level:
            break
        band = candidate
    return {'victory_rewards': deepcopy(band['victory_rewards']),
            'shard_multiplier': band['shard_multiplier'], 'reward_enemy_level': level}


def battle_for(connection, player, battle_id):
    row = connection.execute('SELECT data FROM battles WHERE id=? AND player_id=?',
        (identifier(battle_id), player['player_id'])).fetchone()
    if row is None:
        fail('battle_not_found', 'Battle does not belong to this player', 404)
    battle = json.loads(row[0])
    if battle.get('mode') != 'roguelite':
        fail('battle_mode_conflict', 'Battle is not a Roguelite encounter', 409)
    if battle['status'] != 'active':
        fail('roguelite_battle_finished', 'Battle is already terminal', 409)
    run = run_for(player, battle['run_id'])
    if run['battle_id'] != battle['battle_id'] or run['encounter']['encounter_id'] != battle['encounter_id']:
        fail('roguelite_battle_conflict', 'Battle does not match the current encounter', 409)
    return battle, run


def save_battle(connection, battle, canonical):
    connection.execute('UPDATE battles SET data=? WHERE id=?', (canonical(battle), battle['battle_id']))


def resolve_choice(service, player, run, option_id, automatic, awarded):
    choice = run['pending_choice']
    option = next((item for item in choice['options'] if item['option_id'] == option_id), None)
    if option is None:
        fail('roguelite_option_invalid', 'Option does not belong to the issued choice', 409)
    prizes = awarded(({'inventory': {option['item_id']: option['quantity']}}, 1))
    service._reward(player, prizes)
    resolved = {'choice_id': choice['choice_id'], 'option': deepcopy(option), 'automatic': automatic}
    run['last_choice'] = resolved
    run['pending_choice'] = None
    return resolved, prizes


def apply(service, connection, player, command, parameters, now, canonical, awarded):
    rules = definition(service)
    fields = {'roguelite.start': (), 'roguelite.begin': ('run_id', 'encounter_id', 'team'),
        'roguelite.complete': ('battle_id', 'outcome', 'stars'),
        'roguelite.choose': ('run_id', 'choice_id', 'option_id'),
        'roguelite.abandon': ('battle_id',), 'roguelite.end': ('run_id',)}
    if command not in fields:
        fail('unsupported_command', 'Command is not part of API v1', 404)
    optional = ('formation',) if command == 'roguelite.begin' else (
        ('request_fingerprint',) if command == 'roguelite.choose' else ())
    object_fields(parameters, fields[command], optional)
    fingerprint = parameters.get('request_fingerprint')
    if 'request_fingerprint' in parameters and (not isinstance(fingerprint, str) or len(fingerprint) != 64 or
            any(character not in '0123456789abcdef' for character in fingerprint)):
        fail('invalid_request_fingerprint', 'Request fingerprint must be a SHA-256 hex digest')
    state = progress(player)
    result = {'rewards': awarded(), 'server_time': now}
    if command == 'roguelite.start':
        if state['active_run'] is None:
            run = {'run_id': secrets.token_hex(16), 'seed': secrets.token_hex(16),
                'floor': 1, 'score': 0, 'used_squads': [], 'battle_id': None,
                'pending_choice': None, 'last_choice': None, 'started_at': now}
            run['encounter'] = encounter(service, rules, run)
            state['active_run'] = run
    elif command == 'roguelite.begin':
        run = run_for(player, parameters['run_id'])
        if run['battle_id'] is not None:
            fail('roguelite_battle_active', 'Recover or abandon the outstanding battle first', 409)
        if run['pending_choice'] is not None:
            fail('roguelite_choice_pending', 'Choose a reward or retire before the next fight', 409)
        if identifier(parameters['encounter_id']) != run['encounter']['encounter_id']:
            fail('roguelite_encounter_conflict', 'Encounter is not the issued current floor', 409)
        team = parameters['team']
        if (not isinstance(team, list) or not 1 <= len(team) <= 5 or
                any(not isinstance(hero, str) or hero not in player['characters'] for hero in team) or
                len(set(team)) != len(team)):
            fail('invalid_team', 'Select one to five distinct owned characters')
        signature = sorted(team)
        if signature in run['used_squads']:
            fail('roguelite_squad_used', 'This exact combination was already used in this run', 409)
        formation = parameters.get('formation', list(range(len(team))))
        if (not isinstance(formation, list) or len(formation) != len(team) or
                any(type(slot) is not int or slot not in range(10) for slot in formation) or
                len(set(formation)) != len(formation)):
            fail('invalid_formation', 'Supply one distinct board slot per selected character')
        battle = {'battle_id': secrets.token_hex(16), 'mode': 'roguelite', 'run_id': run['run_id'],
            'encounter_id': run['encounter']['encounter_id'], 'floor': run['floor'],
            'team': deepcopy(team), 'formation': deepcopy(formation),
            'team_state': {hero: deepcopy(player['characters'][hero]) for hero in team},
            'enemies': deepcopy(run['encounter']['enemies']), 'status': 'active'}
        # Old saved encounters predate reward snapshots. Their catalog is still
        # digest-bound; freeze its rewards once when that battle is issued.
        snapshot = run['encounter'] if 'victory_rewards' in run['encounter'] else reward_snapshot(rules, battle['enemies'])
        battle.update({key: deepcopy(snapshot[key]) for key in
            ('victory_rewards', 'shard_multiplier', 'reward_enemy_level')})
        run['used_squads'].append(signature)
        run['battle_id'] = battle['battle_id']
        connection.execute('INSERT INTO battles VALUES (?,?,?)',
            (battle['battle_id'], player['player_id'], canonical(battle)))
        result['battle'] = battle
    elif command in ('roguelite.complete', 'roguelite.abandon'):
        battle, run = battle_for(connection, player, parameters['battle_id'])
        if command == 'roguelite.abandon':
            battle.update(status='abandoned', outcome='abandoned', stars=0)
        else:
            stars = integer(parameters['stars'], 0, 3)
            outcome = parameters['outcome']
            if outcome not in ('victory', 'defeat') or (outcome == 'victory') != (stars > 0):
                fail('invalid_outcome', 'Victory requires stars and defeat requires zero')
            battle.update(status='complete', outcome=outcome, stars=stars)
            if outcome == 'victory':
                result['rewards'] = awarded((battle.get('victory_rewards', rules['victory_rewards']), 1))
                service._reward(player, result['rewards'])
                options = weighted_shard_options(
                    player, rules, service.catalog, run['seed'], battle['battle_id'])
                for option in options:
                    option['quantity'] = integer(option['quantity'] * battle.get('shard_multiplier', 1))
                run['pending_choice'] = {'choice_id': secrets.token_hex(16), 'options': [
                    dict(deepcopy(option), option_id=option['character_id']) for option in options]}
                run['score'] = integer(run['score'] + 1)
                run['floor'] = integer(run['floor'] + 1)
                run['encounter'] = encounter(service, rules, run)
        run['battle_id'] = None
        save_battle(connection, battle, canonical)
        result['battle'] = battle
    elif command == 'roguelite.choose':
        run = run_for(player, parameters['run_id'])
        choice = run['pending_choice']
        if choice is None or choice['choice_id'] != identifier(parameters['choice_id']):
            fail('roguelite_choice_inactive', 'Requested choice is not pending', 409)
        option_id = identifier(parameters['option_id'])
        result['choice'], result['rewards'] = resolve_choice(service, player, run, option_id, False, awarded)
    else:
        run = run_for(player, parameters['run_id'])
        if run['pending_choice']:
            result['choice'], result['rewards'] = resolve_choice(service, player, run,
                run['pending_choice']['options'][0]['option_id'], True, awarded)
        if run['battle_id']:
            battle, _ = battle_for(connection, player, run['battle_id'])
            battle.update(status='abandoned', outcome='abandoned', stars=0)
            save_battle(connection, battle, canonical)
            result['battle'] = battle
        state['last_run'] = {'run_id': run['run_id'], 'status': 'ended', 'floor': run['floor'],
            'score': run['score'], 'used_squads': deepcopy(run['used_squads']),
            'last_choice': deepcopy(run['last_choice']), 'ended_at': now}
        state['active_run'] = None
    result.update(view(service, player))
    if fingerprint is not None:
        result.update(receipt_command=command, request_fingerprint=fingerprint)
    return result

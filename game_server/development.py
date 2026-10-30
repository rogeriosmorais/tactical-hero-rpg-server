"""Catalog-defined character development and recursive equipment transactions."""
from copy import deepcopy

from .catalog import amounts, fail, identifier, integer, object_fields


def payment(value):
    object_fields(value, ('wallet', 'inventory'))
    amounts(value['wallet'])
    amounts(value['inventory'])


def validate(value, characters):
    object_fields(value, ('characters', 'training_items', 'recipes'))
    for collection in value.values():
        if not isinstance(collection, dict):
            fail('invalid_catalog', 'Development collections must be objects')
        for key in collection:
            identifier(key)
    for definition in value['training_items'].values():
        object_fields(definition, ('experience', 'cost'))
        integer(definition['experience'], 1)
        amounts(definition['cost'])
    for definition in value['recipes'].values():
        object_fields(definition, ('ingredients', 'cost'))
        amounts(definition['ingredients'])
        amounts(definition['cost'])
        if not definition['ingredients'] or any(count == 0 for count in definition['ingredients'].values()):
            fail('invalid_catalog', 'Crafting requires positive ingredient quantities')
    depths = {}

    def visit(key, path):
        if key in path:
            fail('invalid_catalog', 'Crafting recipes must be acyclic and at most 64 levels deep')
        if key not in value['recipes']:
            return 0
        if key not in depths:
            if len(path) >= 64:
                fail('invalid_catalog', 'Crafting recipes must be acyclic and at most 64 levels deep')
            depths[key] = 1 + max(visit(ingredient, path + (key,))
                                  for ingredient in value['recipes'][key]['ingredients'])
            if depths[key] > 64:
                fail('invalid_catalog', 'Crafting recipes must be acyclic and at most 64 levels deep')
        return depths[key]
    for key in value['recipes']:
        visit(key, ())
    for key, definition in value['characters'].items():
        if key not in characters:
            fail('invalid_catalog', 'Development character is undefined')
        object_fields(definition, ('experience_thresholds', 'recruitment', 'initial_rank',
                                  'rank_upgrades', 'abilities', 'initial_abilities', 'tiers'))
        thresholds = definition['experience_thresholds']
        if not isinstance(thresholds, list) or len(thresholds) != characters[key]['maximum_level']:
            fail('invalid_catalog', 'Experience thresholds must cover every character level')
        previous = -1
        for threshold in thresholds:
            integer(threshold, previous + 1)
            previous = threshold
        if thresholds[0] != 0:
            fail('invalid_catalog', 'Level one must require zero experience')
        payment(definition['recruitment'])
        ranks = definition['rank_upgrades']
        if not isinstance(ranks, list) or len(ranks) > 100:
            fail('invalid_catalog', 'Rank upgrades must be a bounded list')
        for cost in ranks:
            payment(cost)
        integer(definition['initial_rank'], 1, len(ranks) + 1)
        abilities, initial = definition['abilities'], definition['initial_abilities']
        if (not isinstance(abilities, list) or not isinstance(initial, list) or
                len(abilities) != len(initial) or len(abilities) > 16):
            fail('invalid_catalog', 'Ability definitions and initial levels must match')
        tiers = definition['tiers']
        if not isinstance(tiers, list) or not 1 <= len(tiers) <= 100:
            fail('invalid_catalog', 'Equipment tiers must be a bounded nonempty list')
        for index, steps in enumerate(abilities):
            if not isinstance(steps, list) or len(steps) > 100:
                fail('invalid_catalog', 'Ability upgrades must be a bounded list')
            if steps:
                integer(initial[index], 0, len(steps))
            elif initial[index] is not None:
                fail('invalid_catalog', 'Undefined abilities require a null initial level')
            for step in steps:
                object_fields(step, ('required_level', 'required_tier', 'payment'))
                integer(step['required_level'], 1, characters[key]['maximum_level'])
                integer(step['required_tier'], 1, len(tiers))
                payment(step['payment'])
        for tier in tiers:
            if not isinstance(tier, dict) or len(tier) > 6:
                fail('invalid_catalog', 'Equipment tier requires at most six slots')
            for slot, entry in tier.items():
                identifier(slot)
                object_fields(entry, ('item_id', 'required_level'))
                identifier(entry['item_id'])
                integer(entry['required_level'], 1, characters[key]['maximum_level'])


def initialize(character, definition):
    character.update(experience=definition['experience_thresholds'][character['level'] - 1],
                     rank=definition['initial_rank'], abilities=deepcopy(definition['initial_abilities']),
                     tier=1, equipment={})


def spend(service, player, cost):
    service._spend(player, cost['wallet'])
    for item, count in cost['inventory'].items():
        if player['inventory'].get(item, 0) < count:
            fail('insufficient_items', 'Required development item is not owned', 409)
        player['inventory'][item] -= count
        from .objectives import track
        track(player, 'inventory_spent', item, count)


def craft(service, player, rules, item_id, quantity):
    if item_id not in rules['recipes']:
        fail('item_not_craftable', 'Missing item has no crafting recipe', 409)
    # Expand shared dependencies once in parent-before-ingredient order. This
    # retains existing intermediate stock before crafting the missing amount,
    # without exponential recursion through a diamond-shaped recipe graph.
    visited, order = set(), []

    def visit(item):
        if item in visited:
            return
        visited.add(item)
        for ingredient in rules['recipes'].get(item, {}).get('ingredients', {}):
            visit(ingredient)
        order.append(item)
    visit(item_id)
    demand = {item_id: quantity}
    for item in reversed(order):
        required = demand.get(item, 0)
        if item != item_id:
            available = player['inventory'].get(item, 0)
            consumed = min(available, required)
            player['inventory'][item] = available - consumed
            from .objectives import track
            track(player, 'inventory_spent', item, consumed)
            required -= consumed
        if not required:
            continue
        definition = rules['recipes'].get(item)
        if definition is None:
            fail('item_not_craftable', 'Missing item has no crafting recipe', 409)
        service._spend(player, definition['cost'], required)
        for ingredient, count in definition['ingredients'].items():
            demand[ingredient] = integer(demand.get(ingredient, 0) + count * required)
    player['inventory'][item_id] = integer(player['inventory'].get(item_id, 0) + quantity)


def equip(service, player, rules, character, definition, entries, craft_missing):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 6 or type(craft_missing) is not bool:
        fail('invalid_equipment', 'Equipment batch requires 1–6 slots and a boolean crafting flag')
    seen = set()
    for entry in entries:
        object_fields(entry, ('slot_id', 'item_id'))
        slot, item = identifier(entry['slot_id']), identifier(entry['item_id'])
        if slot in seen:
            fail('invalid_equipment', 'Equipment batch has duplicate slots')
        seen.add(slot)
        expected = definition['tiers'][character['tier'] - 1].get(slot)
        if expected is None or expected['item_id'] != item:
            fail('equipment_mismatch', 'Equipment does not match the current tier slot', 409)
        if slot in character['equipment']:
            fail('equipment_already_equipped', 'Equipment slot is already filled', 409)
        if character['level'] < expected['required_level']:
            fail('equipment_level', 'Character level does not meet slot requirements', 409)
        if player['inventory'].get(item, 0) < 1 and craft_missing:
            craft(service, player, rules, item, 1)
        spend(service, player, {'wallet': {}, 'inventory': {item: 1}})
        character['equipment'][slot] = item


def promote(character, definition, target):
    integer(target, 2, len(definition['tiers']))
    if target != character['tier'] + 1:
        fail('equipment_tier_conflict', 'Equipment promotion must advance one tier', 409)
    slots = definition['tiers'][character['tier'] - 1]
    if any(slot not in character['equipment'] for slot in slots):
        fail('equipment_slots_required', 'All current-tier slots must be equipped', 409)
    if any(character['level'] < entry['required_level'] for entry in slots.values()):
        fail('equipment_level', 'Character level does not meet current tier requirements', 409)
    character.update(tier=target, equipment={})


def apply(service, player, command, parameters):
    rules = service.catalog.get('development')
    if rules is None:
        fail('feature_unavailable', 'Character development is not configured', 404)
    if command == 'equipment.craft':
        object_fields(parameters, ('item_id', 'quantity'), ('character_id',))
        character_id = parameters.get('character_id')
        if 'character_id' in parameters and identifier(character_id) not in player['characters']:
            fail('character_not_owned', 'Character is not owned', 409)
        item = identifier(parameters['item_id'])
        craft(service, player, rules, item, integer(parameters['quantity'], 1, 1000))
        result = {'item_id': item}
        if character_id is not None:
            result.update(character_id=character_id, character=deepcopy(player['characters'][character_id]))
        return result
    fields = {
        'character.recruit': (), 'character.rank_up': (),
        'character.train_items': ('expected_level', 'items'),
        'character.ability_upgrade': ('ability_index', 'unlock'),
        'equipment.equip': ('slots', 'craft_missing'), 'equipment.tier_up': ('target_tier',),
        'equipment.bulk_tier_up': ('target_tier', 'plan')}
    if command not in fields:
        fail('unknown_command', 'Development command is not defined')
    object_fields(parameters, ('character_id',) + fields[command],
                  ('allowed_levels',) if command == 'character.ability_upgrade' else ())
    key = identifier(parameters['character_id'])
    definition = rules['characters'].get(key)
    if definition is None:
        fail('development_not_found', 'Character has no development definition', 404)
    if command == 'character.recruit':
        if key in player['characters']:
            fail('character_already_owned', 'Character is already recruited', 409)
        spend(service, player, definition['recruitment'])
        character = {'level': 1, 'power': service.catalog['characters'][key]['power_per_level']}
        initialize(character, definition)
        player['characters'][key] = character
    else:
        character = player['characters'].get(key)
        if character is None:
            fail('character_not_owned', 'Character is not owned', 409)
    if command == 'character.rank_up':
        if character['rank'] > len(definition['rank_upgrades']):
            fail('maximum_rank', 'Character has reached its maximum rank', 409)
        spend(service, player, definition['rank_upgrades'][character['rank'] - 1])
        character['rank'] += 1
    elif command == 'character.train_items':
        integer(parameters['expected_level'], 1)
        if parameters['expected_level'] != character['level']:
            fail('character_level_conflict', 'Training request uses a stale level', 409)
        items = parameters['items']
        amounts(items)
        if not 1 <= len(items) <= 100 or any(count == 0 for count in items.values()):
            fail('invalid_training', 'Training requires positive item quantities')
        experience = character['experience']
        for item, count in items.items():
            training = rules['training_items'].get(item)
            if training is None:
                fail('invalid_training', 'Item is not a configured training material')
            spend(service, player, {'wallet': {}, 'inventory': {item: count}})
            service._spend(player, training['cost'], count)
            experience = integer(experience + training['experience'] * count)
        level = sum(threshold <= experience for threshold in definition['experience_thresholds'])
        if level > player['level']:
            fail('commander_level_required', 'Training exceeds the account level', 409)
        character.update(experience=experience, level=level)
    elif command == 'character.ability_upgrade':
        index = integer(parameters['ability_index'], 0, len(character['abilities']) - 1)
        unlock = parameters['unlock']
        current = character['abilities'][index]
        if type(unlock) is not bool or current is None or (unlock and current != 0) or (not unlock and current < 1):
            fail('ability_state_conflict', 'Ability unlock/upgrade does not match saved state', 409)
        steps = definition['abilities'][index]
        if current >= len(steps):
            fail('maximum_ability_level', 'Ability has reached its maximum level', 409)
        if 'allowed_levels' in parameters:
            allowed = parameters['allowed_levels']
            if not isinstance(allowed, list) or not 1 <= len(allowed) <= len(steps):
                fail('invalid_ability_levels', 'Allowed target levels must be a bounded list')
            for level in allowed:
                integer(level, 1, len(steps))
            if len(set(allowed)) != len(allowed) or current + 1 not in allowed:
                fail('ability_state_conflict', 'Ability target level does not match the request', 409)
        step = steps[current]
        if character['level'] < step['required_level'] or character['tier'] < step['required_tier']:
            fail('ability_requirement', 'Ability level/tier requirements are not met', 409)
        spend(service, player, step['payment'])
        character['abilities'][index] += 1
    elif command == 'equipment.equip':
        equip(service, player, rules, character, definition, parameters['slots'], parameters['craft_missing'])
    elif command == 'equipment.tier_up':
        promote(character, definition, parameters['target_tier'])
    elif command == 'equipment.bulk_tier_up':
        target = integer(parameters['target_tier'], character['tier'] + 1, len(definition['tiers']))
        plan = parameters['plan']
        if not isinstance(plan, list) or len(plan) > 600:
            fail('invalid_equipment', 'Bulk tier plan must be a bounded list')
        expected = []
        for tier in range(character['tier'], target):
            for slot, entry in definition['tiers'][tier - 1].items():
                if tier != character['tier'] or slot not in character['equipment']:
                    expected.append({'tier': tier, 'slot_id': slot, 'item_id': entry['item_id']})
        for entry in plan:
            object_fields(entry, ('tier', 'slot_id', 'item_id'))
            integer(entry['tier'], 1)
            identifier(entry['slot_id'])
            identifier(entry['item_id'])
        normalize = lambda values: sorted((entry['tier'], entry['slot_id'], entry['item_id']) for entry in values)
        if normalize(plan) != normalize(expected):
            fail('equipment_mismatch', 'Bulk tier plan does not match required missing slots', 409)
        for tier in range(character['tier'], target):
            slots = [{key: value for key, value in entry.items() if key != 'tier'}
                     for entry in expected if entry['tier'] == tier]
            if slots:
                equip(service, player, rules, character, definition, slots, True)
            promote(character, definition, tier + 1)
    character['power'] = integer(character['level'] * service.catalog['characters'][key]['power_per_level'])
    return {'character_id': key, 'character': deepcopy(character)}

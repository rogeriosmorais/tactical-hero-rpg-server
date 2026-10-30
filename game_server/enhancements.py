"""Owned enhancement equipment, fusion, class history and atomic bulk upgrades."""
from copy import deepcopy

from .catalog import amounts, fail, identifier, integer, object_fields
from .development import craft


def validate(rules, characters):
    object_fields(rules, ('required_level', 'slots', 'items', 'tiers', 'classes', 'characters', 'swap_seconds'), ('required_item',))
    integer(rules['required_level'], 1, 10000)
    integer(rules['swap_seconds'], 0)
    if 'required_item' in rules:
        identifier(rules['required_item'])
    slots = rules['slots']
    if not isinstance(slots, list) or not 1 <= len(slots) <= 8:
        fail('invalid_catalog', 'Enhancements require 1–8 distinct slots')
    for slot in slots:
        identifier(slot)
    if len(set(slots)) != len(slots):
        fail('invalid_catalog', 'Enhancement slots must be distinct')
    tiers = rules['tiers']
    if not isinstance(tiers, list) or not 1 <= len(tiers) <= 100:
        fail('invalid_catalog', 'Enhancement tiers must be a bounded nonempty list')
    for tier in tiers:
        object_fields(tier, ('required_level', 'minimum_quality', 'cost'))
        integer(tier['required_level'], 1, 10000)
        integer(tier['minimum_quality'], 0, 100)
        amounts(tier['cost'])
    if tiers[0]['minimum_quality'] or any(tiers[0]['cost'].values()):
        fail('invalid_catalog', 'Initial enhancement tier must be free')
    for name in ('items', 'classes', 'characters'):
        if not isinstance(rules[name], dict):
            fail('invalid_catalog', 'Enhancement collections must be objects')
        for key in rules[name]:
            identifier(key)
    identities = set()
    for key, item in rules['items'].items():
        object_fields(item, ('role', 'slot_id', 'tier', 'quality'), ('fusion',))
        identifier(item['role'])
        identifier(item['slot_id'])
        if item['slot_id'] not in slots:
            fail('invalid_catalog', 'Enhancement item slot is undefined')
        integer(item['tier'], 1, len(tiers))
        integer(item['quality'], 1, 100)
        identity = (item['role'], item['slot_id'], item['quality'])
        if identity in identities:
            fail('invalid_catalog', 'Enhancement role/slot/quality must identify one item')
        identities.add(identity)
        if 'fusion' in item:
            fusion = item['fusion']
            object_fields(fusion, ('source', 'quantity', 'cost'))
            identifier(fusion['source'])
            integer(fusion['quantity'], 1, 1000000)
            amounts(fusion['cost'])
            source = rules['items'].get(fusion['source'])
            if (not isinstance(source, dict) or source.get('role') != item['role'] or
                    source.get('slot_id') != item['slot_id'] or type(source.get('quality')) is not int or
                    not 0 < source['quality'] < item['quality']):
                fail('invalid_catalog', 'Fusion must use a lower quality item for the same role and slot')
    for key, role in rules['characters'].items():
        identifier(role)
        if key not in characters:
            fail('invalid_catalog', 'Enhancement character is undefined')
    skill_ids = set()
    for definition in rules['classes'].values():
        object_fields(definition, ('skills',))
        skills = definition['skills']
        if not isinstance(skills, list) or not 1 <= len(skills) <= 100:
            fail('invalid_catalog', 'Enhancement classes require bounded skill lists')
        preceding = set()
        for skill in skills:
            object_fields(skill, ('skill_id', 'required_quality', 'cost'), ('prerequisite',))
            key = identifier(skill['skill_id'])
            integer(skill['required_quality'], 1, 100)
            amounts(skill['cost'])
            if 'prerequisite' in skill:
                identifier(skill['prerequisite'])
            if key in skill_ids or ('prerequisite' in skill and skill['prerequisite'] not in preceding):
                fail('invalid_catalog', 'Skills must be distinct and prerequisites must precede them in the same class')
            skill_ids.add(key)
            preceding.add(key)


def quality(state, rules):
    return (min(rules['items'][state['items'][slot]]['quality'] for slot in rules['slots'])
            if all(slot in state['items'] for slot in rules['slots']) else 0)


def available(player, rules):
    return bool(rules and player['level'] >= rules['required_level'] and
                ('required_item' not in rules or player['inventory'].get(rules['required_item'], 0) > 0))


def inventory(player, key, count):
    balance = player['inventory'].get(key, 0) + count
    if balance < 0:
        fail('insufficient_items', 'Required enhancement item is not owned', 409)
    player['inventory'][key] = integer(balance)
    if count < 0:
        from .objectives import track
        track(player, 'inventory_spent', key, -count)


def equip(service, player, character, state, rules, role, item_id):
    item = rules['items'].get(identifier(item_id))
    if item is None or item['role'] != role:
        fail('enhancement_role', 'Enhancement item does not match the character role', 409)
    unlocked = rules['tiers'][state['tier'] - 1]
    if item['tier'] > state['tier'] or (item['tier'] > 1 and character['level'] < unlocked['required_level']):
        fail('enhancement_tier_required', 'Enhancement tier/level requirements are not met', 409)
    slot = item['slot_id']
    old = state['items'].get(slot)
    if old == item_id:
        return
    inventory(player, item_id, -1)
    if old is not None:
        inventory(player, old, 1)
    state['items'][slot] = item_id


def select(state, rules, class_id, now):
    identifier(class_id)
    if class_id not in rules['classes']:
        fail('enhancement_class_not_found', 'Enhancement class is undefined', 404)
    if state['class_id'] != class_id:
        state.update(class_id=class_id, swap_available_at=integer(now + rules['swap_seconds']))
    state['learned'].setdefault(class_id, [])


def learn(service, player, state, rules, skill_id):
    identifier(skill_id)
    skills = rules['classes'].get(state['class_id'], {}).get('skills', [])
    skill = next((row for row in skills if row['skill_id'] == skill_id), None)
    if skill is None:
        fail('enhancement_skill_not_found', 'Skill does not belong to the selected class', 409)
    learned = state['learned'].setdefault(state['class_id'], [])
    if skill_id in learned:
        return
    if skill.get('prerequisite') and skill['prerequisite'] not in learned:
        fail('enhancement_prerequisite', 'Enhancement skill prerequisite is not learned', 409)
    if quality(state, rules) < skill['required_quality']:
        fail('enhancement_quality_required', 'Equipped enhancement quality is too low', 409)
    service._spend(player, skill['cost'])
    learned.append(skill_id)


def apply(service, player, command, parameters):
    rules = service.catalog.get('enhancements')
    if rules is None:
        fail('feature_unavailable', 'Enhancements are not configured', 404)
    fields = {'enhancement.equip': ('item_id',), 'enhancement.unequip': ('slot_id',),
              'enhancement.fuse': ('item_id',), 'enhancement.select_class': ('class_id',),
              'enhancement.learn': ('skill_id',), 'enhancement.refresh': (),
              'enhancement.tier_up': ('target_tier',), 'enhancement.bulk': ('class_id', 'target_quality')}
    if command not in fields:
        fail('unknown_command', 'Enhancement command is undefined')
    object_fields(parameters, ('character_id',) + fields[command], ('sources',) if command == 'enhancement.fuse' else ())
    key = identifier(parameters['character_id'])
    role = rules['characters'].get(key)
    character = player['characters'].get(key)
    if role is None or character is None:
        fail('character_not_owned', 'An owned enhancement-enabled character is required', 409)
    if not available(player, rules):
        fail('enhancement_locked', 'Enhancement account level or earned access requirement is not met', 409)
    state = character.setdefault('enhancement', {'items': {}, 'tier': 1, 'class_id': None,
                                                 'learned': {}, 'swap_available_at': 0})
    now = int(service.clock())
    recipes = {item_id: {'ingredients': {row['fusion']['source']: row['fusion']['quantity']},
                        'cost': row['fusion']['cost']}
               for item_id, row in rules['items'].items() if 'fusion' in row}
    if command == 'enhancement.equip':
        equip(service, player, character, state, rules, role, parameters['item_id'])
    elif command == 'enhancement.unequip':
        slot = identifier(parameters['slot_id'])
        if slot not in rules['slots']:
            fail('enhancement_slot_not_found', 'Enhancement slot is undefined', 404)
        old = state['items'].pop(slot, None)
        if old is not None:
            inventory(player, old, 1)
    elif command == 'enhancement.fuse':
        item_id = identifier(parameters['item_id'])
        definition = rules['items'].get(item_id)
        if definition is None or 'fusion' not in definition:
            fail('enhancement_fusion_unavailable', 'Enhancement item cannot be fused', 409)
        fusion = definition['fusion']
        if 'sources' in parameters:
            sources = parameters['sources']
            if (not isinstance(sources, list) or len(sources) != fusion['quantity'] or
                    any(source != fusion['source'] for source in sources)):
                fail('enhancement_recipe_mismatch', 'Declared fusion sources do not match the recipe', 409)
        # A single fuse consumes inventory directly; it never auto-crafts.
        inventory(player, fusion['source'], -fusion['quantity'])
        service._spend(player, fusion['cost'])
        inventory(player, item_id, 1)
    elif command == 'enhancement.select_class':
        select(state, rules, parameters['class_id'], now)
    elif command == 'enhancement.learn':
        learn(service, player, state, rules, parameters['skill_id'])
    elif command == 'enhancement.refresh':
        if quality(state, rules) < 1:
            fail('enhancement_matrix_required', 'A full enhancement matrix is required', 409)
    elif command == 'enhancement.tier_up':
        target = integer(parameters['target_tier'], 2, len(rules['tiers']))
        if target not in (state['tier'], state['tier'] + 1):
            fail('enhancement_tier_conflict', 'Enhancement tiers must advance in order', 409)
        tier = rules['tiers'][target - 1]
        if character['level'] < tier['required_level']:
            fail('enhancement_level_required', 'Character level is too low for this tier', 409)
        if target != state['tier']:
            if quality(state, rules) < tier['minimum_quality']:
                fail('enhancement_quality_required', 'Equipped matrix does not qualify for the next tier', 409)
            service._spend(player, tier['cost'])
            state['tier'] = target
    elif command == 'enhancement.bulk':
        target = integer(parameters['target_quality'], 1, 100)
        class_id = parameters['class_id']
        # Validate the class before touching any equipment.
        identifier(class_id)
        if class_id not in rules['classes']:
            fail('enhancement_class_not_found', 'Enhancement class is undefined', 404)
        for slot in rules['slots']:
            item_id = next((item_id for item_id, row in rules['items'].items()
                            if (row['role'], row['slot_id'], row['quality']) == (role, slot, target)), None)
            if item_id is None:
                fail('enhancement_quality_unavailable', 'Requested quality is not defined for every slot', 409)
            old = state['items'].get(slot)
            if old is not None and rules['items'][old]['quality'] >= target:
                continue
            if old is not None:
                inventory(player, old, 1)
                del state['items'][slot]
            if player['inventory'].get(item_id, 0) < 1:
                craft(service, player, {'recipes': recipes}, item_id, 1)
            equip(service, player, character, state, rules, role, item_id)
        select(state, rules, class_id, now)
        skills = rules['classes'][class_id]['skills']
        learned = state['learned'][class_id]
        current = max((index for index, row in enumerate(skills, 1) if row['skill_id'] in learned), default=0)
        if target > len(skills):
            fail('enhancement_skill_not_found', 'Class has no skill at the requested quality', 409)
        for row in skills[current:target]:
            learn(service, player, state, rules, row['skill_id'])
    state['quality'] = quality(state, rules)
    return {'character_id': key, 'character': deepcopy(character)}

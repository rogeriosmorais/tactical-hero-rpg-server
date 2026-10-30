"""Persist cosmetic selection and guide acknowledgements without game rewards."""
from copy import deepcopy

from .catalog import fail, identifier, object_fields


def validate(catalog):
    appearance = catalog.get('appearance')
    if 'appearance' in catalog:
        object_fields(appearance, ('default', 'choices'))
        identifier(appearance['default'])
        if not isinstance(appearance['choices'], dict) or appearance['default'] not in appearance['choices']:
            fail('invalid_catalog', 'Default appearance must be a defined choice')
        for key, choice in appearance['choices'].items():
            identifier(key)
            object_fields(choice, (), ('required_item',))
            if 'required_item' in choice:
                identifier(choice['required_item'])
        if 'required_item' in appearance['choices'][appearance['default']]:
            fail('invalid_catalog', 'Default appearance must be freely selectable')
    guides = catalog.get('guides', {})
    if not isinstance(guides, dict):
        fail('invalid_catalog', 'Guides must be an object')
    for guide_id, guide in guides.items():
        identifier(guide_id)
        object_fields(guide, ('events',))
        if not isinstance(guide['events'], dict):
            fail('invalid_catalog', 'Guide events must be an object')
        for event_id, event in guide['events'].items():
            identifier(event_id)
            object_fields(event, ('completes',), ('checkpoint',))
            if type(event['completes']) is not bool:
                fail('invalid_catalog', 'Guide completion flag must be boolean')
            if 'checkpoint' in event:
                identifier(event['checkpoint'])


def view(player, catalog):
    return {'appearance_id': player.get('appearance_id', (catalog.get('appearance') or {}).get('default')),
            'guides': {key: deepcopy(player.get('guides', {}).get(key, {'completed': False}))
                       for key in catalog.get('guides', {})}}


def apply(player, catalog, command, parameters):
    appearance_changed = None
    if command == 'appearance.select':
        object_fields(parameters, ('appearance_id',))
        key = identifier(parameters['appearance_id'])
        definition = (catalog.get('appearance') or {}).get('choices', {}).get(key)
        if definition is None:
            fail('appearance_not_found', 'Appearance is not defined', 404)
        item = definition.get('required_item')
        if item is not None and player['inventory'].get(item, 0) <= 0:
            fail('appearance_not_owned', 'Appearance has not been earned', 409)
        appearance_changed = view(player, catalog)['appearance_id'] != key
        player['appearance_id'] = key
    elif command == 'guide.acknowledge':
        object_fields(parameters, ('guide_ids',))
        keys = parameters['guide_ids']
        if not isinstance(keys, list) or not 1 <= len(keys) <= 100:
            fail('invalid_guides', 'Acknowledge 1–100 distinct guides')
        for key in keys:
            identifier(key)
            if key not in catalog.get('guides', {}):
                fail('guide_not_found', 'Guide is not defined', 404)
        if len(set(keys)) != len(keys):
            fail('invalid_guides', 'Acknowledged guides must be distinct')
        for key in keys:
            player.setdefault('guides', {}).setdefault(key, {'completed': False})['completed'] = True
    elif command == 'guide.event':
        object_fields(parameters, ('guide_id', 'event_id', 'success'))
        key, event_id = identifier(parameters['guide_id']), identifier(parameters['event_id'])
        if type(parameters['success']) is not bool:
            fail('invalid_event', 'Guide event success must be boolean')
        guide = catalog.get('guides', {}).get(key)
        if guide is None or event_id not in guide['events']:
            fail('guide_event_not_found', 'Guide event is not defined', 404)
        event = guide['events'][event_id]
        if parameters['success'] and (event['completes'] or 'checkpoint' in event):
            state = player.setdefault('guides', {}).setdefault(key, {'completed': False})
            if 'checkpoint' in event:
                state['checkpoint'] = event['checkpoint']
            if event['completes']:
                state['completed'] = True
    else:
        fail('unknown_command', 'Player preference command is not defined')
    result = view(player, catalog)
    if appearance_changed is not None:
        result['appearance_changed'] = appearance_changed
    return result

"""Milestone group views over trusted counters and shared objective claim ledgers."""
from .catalog import fail, identifier, integer, object_fields
from .objectives import period, period_for_key, view as objective_view


def validate(definitions, catalog):
    if not isinstance(definitions, dict) or 'objectives' not in catalog:
        fail('invalid_catalog', 'Milestones require objective definitions')
    rules = catalog['objectives']
    for key, definition in definitions.items():
        identifier(key)
        object_fields(definition, ('counter_id', 'objective_ids'), ('display_grace_seconds',))
        counter = rules['counters'].get(identifier(definition['counter_id']))
        if counter is None or counter['source']['kind'] not in ('activity', 'signals') or 'period' not in counter:
            fail('invalid_catalog', 'Milestones require period-bound trusted activity or signals')
        integer(definition.get('display_grace_seconds', 0), 0, 604800)
        entries = definition['objective_ids']
        if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
            fail('invalid_catalog', 'Milestone objectives require a bounded nonempty list')
        seen, previous = set(), -1
        for target in entries:
            identifier(target)
            entry = rules['entries'].get(target)
            if (entry is None or entry['counter_id'] != definition['counter_id'] or target in seen or
                    entry['threshold'] <= previous):
                fail('invalid_catalog', 'Milestone tiers require distinct shared-counter objectives and increasing thresholds')
            seen.add(target)
            previous = entry['threshold']


def read_one(player, catalog, key, now, period_key=None):
    definition = catalog['milestones'][key]
    rules = catalog['objectives']
    counter = rules['counters'][definition['counter_id']]
    current = period(counter, player, now)
    window = period_for_key(counter, player, now, period_key or current['key'])
    historical = window['key'] != current['key']
    state = objective_view(player, rules, window['starts_at'] if historical else now)
    entries = {target: {**state['objectives'][target], 'period': window,
                       'claimable': not historical and state['objectives'][target]['claimable']}
               for target in definition['objective_ids']}
    display_until = window['ends_at'] + definition.get('display_grace_seconds', 0)
    visible = (not historical and int(window['key']) >= 0 and window['starts_at'] <= now < display_until and
               any(row['level_available'] for row in entries.values()))
    return {'period': window, 'score': state['counters'][definition['counter_id']]['value'],
            'objectives': entries, 'visible': visible, 'display_until': display_until}


def view(player, catalog, now):
    result = {}
    for key, definition in catalog.get('milestones', {}).items():
        current = read_one(player, catalog, key, now)
        counter = catalog['objectives']['counters'][definition['counter_id']]
        known = set(player.get('activity_windows', {}).get(definition['counter_id'], {}))
        for target in definition['objective_ids']:
            known.update(player.get('objective_claims', {}).get(target, {}))
        if counter['source']['kind'] == 'signals':
            for signal in player.get('reward_signals', []):
                if set(signal['labels']) & set(counter['source']['label_ids']):
                    known.add(period(counter, player, signal['at'])['key'])
        result[key] = {'current': current, 'history': [read_one(player, catalog, key, now, period_key)
            for period_key in sorted(known) if period_key != current['period']['key']]}
    return {'milestones': result, 'server_time': now}

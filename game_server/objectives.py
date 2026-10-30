"""Transaction-derived counters, reset windows and atomic objective claims."""
from copy import deepcopy
from datetime import datetime, timezone

from .catalog import fail, identifier, integer, object_fields, rewards


COMMANDS = set('character.train character.train_items character.recruit character.rank_up character.ability_upgrade '
    'equipment.craft equipment.equip equipment.tier_up equipment.bulk_tier_up mission.begin mission.complete mission.sweep '
    'shop.purchase free_crate.claim tower.start tower.end tower.begin tower.complete tower.claim appearance.select '
    'guide.acknowledge guide.event squad.save enhancement.equip enhancement.unequip enhancement.fuse enhancement.select_class '
    'enhancement.learn enhancement.refresh enhancement.tier_up enhancement.bulk promotion.apply promotion.recycle '
    'player.login objective.claim pass.claim pass.unlock calendar.advance alliance.create alliance.join alliance.leave '
    'alliance.update alliance.acknowledge alliance.donate alliance.upgrade_boost alliance.upgrade_category '
    'raid.launch raid.join raid.lock raid.release raid.set_difficulty raid.auto_launch raid.begin raid.complete raid.checkpoint raid.reset raid.refill raid.heal raid.settle raid.refresh arena.refresh arena.set_defense arena.begin arena.complete seasonal.refresh seasonal.claim seasonal.buy blitz.refresh blitz.recharge blitz.begin blitz.complete dark.start dark.refresh dark.begin dark.complete dark.heal mail.read mail.claim mail.read_claim mail.delete loot.open'.split())
MEASURES = {'count', 'quantity', 'equipped', 'wallet_spent', 'inventory_spent', 'inventory_earned', 'login', 'alliance_experience'}


def validate_period(period):
    if isinstance(period, dict) and 'calendar' in period:
        object_fields(period, ('calendar', 'anchor'))
        if period['calendar'] != 'month':
            fail('invalid_catalog', 'Calendar counter period is undefined')
        integer(period['anchor'], 0, 253370764800)
        anchor = datetime.fromtimestamp(period['anchor'], timezone.utc)
        if anchor.day != 1 or anchor.hour or anchor.minute or anchor.second:
            fail('invalid_catalog', 'Monthly counter anchor must be a UTC month boundary')
        return
    object_fields(period, ('seconds', 'anchor'), ('local_time', 'alternate_anchor', 'repeat'))
    integer(period['seconds'], 1, 3155760000)
    if period['anchor'] != 'player_created':
        integer(period['anchor'])
    for field in ('local_time', 'repeat'):
        if field in period and type(period[field]) is not bool:
            fail('invalid_catalog', 'Reset flags must be boolean')
    if 'alternate_anchor' in period:
        integer(period['alternate_anchor'])


def validate(rules, catalog):
    object_fields(rules, ('counters', 'entries'))
    for collection in rules.values():
        if not isinstance(collection, dict):
            fail('invalid_catalog', 'Objective collections must be objects')
        for key in collection:
            identifier(key)
    source_fields = {'activity': ('rules',), 'level': (), 'owned_characters': (), 'highest_character_level': (),
        'total_power': (), 'top_power': ('count',), 'owned_matching': ('character_ids',),
        'mission_clear': ('mission_id',), 'inventory': ('item_id',), 'login_streak': (),
        'all_clear': ('objective_ids',), 'constant': ('value',), 'untracked': (), 'signals': ('label_ids',), 'alliance_contribution': ()}
    for definition in rules['counters'].values():
        object_fields(definition, ('source',), ('period',))
        if 'period' in definition:
            validate_period(definition['period'])
        source = definition['source']
        if not isinstance(source, dict) or not isinstance(source.get('kind'), str) or source['kind'] not in source_fields:
            fail('invalid_catalog', 'Counter source is undefined')
        kind = source['kind']
        object_fields(source, ('kind',) + source_fields[kind], ('multiplier',) if kind == 'signals' else ())
        if kind == 'signals':
            integer(source.get('multiplier', 1), 1, 1000000000)
        if kind == 'activity':
            if not isinstance(source['rules'], list) or not 1 <= len(source['rules']) <= 100:
                fail('invalid_catalog', 'Activity counters require bounded trigger lists')
            for trigger in source['rules']:
                object_fields(trigger, ('command', 'measure'), ('entity_ids', 'outcome', 'currency_id', 'item_id', 'multiplier', 'required_level', 'maximum_level'))
                if (not isinstance(trigger['command'], str) or not isinstance(trigger['measure'], str) or
                        trigger['command'] not in COMMANDS or trigger['measure'] not in MEASURES):
                    fail('invalid_catalog', 'Activity trigger is undefined')
                if trigger['command'] == 'player.login' and trigger['measure'] != 'login':
                    fail('invalid_catalog', 'Login activity must use daily deduplicated login facts')
                if trigger['command'] == 'calendar.advance' and trigger['measure'] not in ('quantity', 'inventory_earned'):
                    fail('invalid_catalog', 'Calendar activity must measure actual earned awards')
                if 'entity_ids' in trigger:
                    ids = trigger['entity_ids']
                    if not isinstance(ids, list) or not ids:
                        fail('invalid_catalog', 'Entity filter must be a nonempty list')
                    for key in ids:
                        identifier(key)
                    if len(set(ids)) != len(ids):
                        fail('invalid_catalog', 'Entity filters must be distinct')
                if 'outcome' in trigger and trigger['outcome'] not in ('victory', 'defeat', 'retreat'):
                    fail('invalid_catalog', 'Activity outcome is undefined')
                if trigger['measure'] == 'wallet_spent' and 'currency_id' not in trigger:
                    fail('invalid_catalog', 'Wallet spending requires a currency ID')
                if trigger['measure'] in ('inventory_spent', 'inventory_earned') and 'item_id' not in trigger:
                    fail('invalid_catalog', 'Item activity requires an item ID')
                for field in ('currency_id', 'item_id'):
                    if field in trigger:
                        identifier(trigger[field])
                integer(trigger.get('multiplier', 1))
                integer(trigger.get('required_level', 1), 1, 10000)
                integer(trigger.get('maximum_level', 10000), trigger.get('required_level', 1), 10000)
        elif kind == 'signals':
            labels = source['label_ids']
            if not isinstance(labels, list) or not labels:
                fail('invalid_catalog', 'Signal counters require distinct label IDs')
            for label in labels:
                identifier(label)
            if len(set(labels)) != len(labels):
                fail('invalid_catalog', 'Signal counter labels must be distinct')
        elif kind in ('owned_matching', 'all_clear'):
            ids = source['character_ids' if kind == 'owned_matching' else 'objective_ids']
            collection = catalog['characters'] if kind == 'owned_matching' else rules['entries']
            if not isinstance(ids, list) or not ids:
                fail('invalid_catalog', 'Derived counter references must be nonempty lists')
            for key in ids:
                identifier(key)
                if key not in collection:
                    fail('invalid_catalog', 'Derived counter references an undefined entity')
            if len(set(ids)) != len(ids):
                fail('invalid_catalog', 'Derived counter references must be distinct')
        elif kind in ('constant', 'top_power'):
            integer(source['value'] if kind == 'constant' else source['count'], 0 if kind == 'constant' else 1)
        elif kind == 'mission_clear':
            if identifier(source['mission_id']) not in catalog['missions']:
                fail('invalid_catalog', 'Counter mission is undefined')
        elif kind == 'inventory':
            identifier(source['item_id'])
    for definition in rules['entries'].values():
        object_fields(definition, ('counter_id', 'threshold', 'required_level', 'maximum_level', 'rewards'), ('claim_grace_seconds',))
        if identifier(definition['counter_id']) not in rules['counters']:
            fail('invalid_catalog', 'Objective counter is undefined')
        integer(definition['threshold'])
        integer(definition['required_level'], 1, 10000)
        integer(definition['maximum_level'], definition['required_level'], 10000)
        rewards(definition['rewards'])
        integer(definition.get('claim_grace_seconds', 0), 0, 86400)
    depths = {}

    def visit(key, path):
        if key in path or len(path) > 64:
            fail('invalid_catalog', 'All-clear dependencies must be acyclic and bounded')
        if key in depths:
            return depths[key]
        source = rules['counters'][key]['source']
        depth = 1
        if source['kind'] == 'all_clear':
            for objective_id in source['objective_ids']:
                depth = max(depth, 1 + visit(rules['entries'][objective_id]['counter_id'], path + (key,)))
        if depth > 64:
            fail('invalid_catalog', 'All-clear dependencies must be acyclic and bounded')
        depths[key] = depth
        return depth
    for key in rules['counters']:
        visit(key, ())


def period(definition, player, now):
    rule = definition.get('period')
    if rule is None:
        return {'key': 'lifetime', 'starts_at': None, 'ends_at': None, 'active': True}
    if rule.get('calendar') == 'month':
        anchor = datetime.fromtimestamp(rule['anchor'], timezone.utc)
        current = datetime.fromtimestamp(now, timezone.utc)
        index = (current.year - anchor.year) * 12 + current.month - anchor.month
        start, end = month_bounds(anchor, index)
        return {'key': str(index), 'starts_at': start, 'ends_at': end, 'active': start <= now < end}
    anchor = player['created_at'] if rule['anchor'] == 'player_created' else rule['anchor']
    if rule.get('alternate_anchor', 0) > 0 and player.get('alternate_reset', False):
        anchor = rule['alternate_anchor']
    elif rule.get('local_time', False):
        anchor -= player.get('reset_offset_seconds', 0)
    index = (now - anchor) // rule['seconds'] if rule.get('repeat', True) else 0
    start = anchor + index * rule['seconds']
    end = start + rule['seconds']
    return {'key': str(index), 'starts_at': start, 'ends_at': end, 'active': start <= now < end}


def month_bounds(anchor, index):
    year, month = divmod(anchor.year * 12 + anchor.month - 1 + index, 12)
    next_year, next_month = divmod(year * 12 + month + 1, 12)
    if not 1 <= year <= next_year <= 9999:
        fail('invalid_period', 'Month period is out of range')
    return (int(datetime(year, month + 1, 1, tzinfo=timezone.utc).timestamp()),
            int(datetime(next_year, next_month + 1, 1, tzinfo=timezone.utc).timestamp()))


def period_for_key(definition, player, now, key):
    current = period(definition, player, now)
    if key == current['key']:
        return current
    rule = definition.get('period', {})
    if (not isinstance(key, str) or len(key) > 12 or current['key'] == 'lifetime' or not rule.get('repeat', True)):
        fail('invalid_period', 'Counter period is invalid')
    try:
        index = int(key)
    except ValueError:
        fail('invalid_period', 'Counter period is invalid')
    if str(index) != key:
        fail('invalid_period', 'Counter period is invalid')
    if rule.get('calendar') == 'month':
        start, end = month_bounds(datetime.fromtimestamp(rule['anchor'], timezone.utc), index)
    else:
        start = current['starts_at'] + (index - int(current['key'])) * rule['seconds']
        end = start + rule['seconds']
    return {'key': key, 'starts_at': start, 'ends_at': end, 'active': start <= now < end}


def track(player, category, key, quantity):
    transaction = player.get('_transaction_activity')
    if transaction is not None and quantity > 0:
        values = transaction[category]
        values[key] = integer(values.get(key, 0) + quantity)


def login(player, now):
    day = now // 86400
    days = player.setdefault('login_days', [])
    fresh = day not in days
    if fresh:
        days.append(day)
    return {'login_day': day, 'new_login': fresh}


def view(player, rules, now):
    counters, entries = {}, {}

    def counter(key):
        if key in counters:
            return counters[key]
        definition = rules['counters'][key]
        source = definition['source']
        kind = source['kind']
        window = period(definition, player, now)
        tracked = kind != 'untracked'
        roster = player['characters']
        if kind == 'activity':
            value = player.get('activity_windows', {}).get(key, {}).get(window['key'], 0)
        elif kind == 'level':
            value = player['level']
        elif kind == 'owned_characters':
            value = len(roster)
        elif kind == 'highest_character_level':
            value = max((character['level'] for character in roster.values()), default=0)
        elif kind in ('total_power', 'top_power'):
            powers = sorted((character['power'] for character in roster.values()), reverse=True)
            value = sum(powers if kind == 'total_power' else powers[:source['count']])
        elif kind == 'owned_matching':
            value = sum(key in roster for key in source['character_ids'])
        elif kind == 'mission_clear':
            value = int(player['missions'].get(source['mission_id'], {}).get('best_stars', 0) > 0)
        elif kind == 'inventory':
            value = player['inventory'].get(source['item_id'], 0)
        elif kind == 'alliance_contribution':
            value = player.get('alliance_donations', {}).get('experience', 0)
        elif kind == 'signals':
            value = sum(signal['quantity'] for signal in player.get('reward_signals', [])
                        if set(source['label_ids']) & set(signal['labels']) and
                        (window['starts_at'] is None or window['starts_at'] <= signal['at'] < window['ends_at'])) * source.get('multiplier', 1)
        elif kind == 'login_streak':
            # Trusted offline imports may carry an original, already-earned
            # streak whose individual earlier login dates were never saved.
            # Keep that proven length as a seed, not invented historical days.
            seed = player.get('login_streak_seed')
            days = set(player.get('login_days', [])) | {
                seed['day'] if seed is not None else player['created_at'] // 86400}
            day = max(days)
            value = 1
            while day - 1 in days:
                value += 1
                day -= 1
            if seed is not None and day == seed['day']:
                value += seed['length'] - 1
        elif kind == 'all_clear':
            selected = [entry(key) for key in source['objective_ids']]
            selected = [item for item in selected if item['level_available'] and item['period']['active']]
            tracked = all(item['tracked'] for item in selected)
            value = int(bool(selected) and tracked and all(item['progress'] >= item['threshold'] for item in selected))
        else:
            value = source.get('value', 0)
        counters[key] = {'value': integer(value), 'tracked': tracked, 'period': window}
        return counters[key]

    def entry(key):
        if key in entries:
            return entries[key]
        definition = rules['entries'][key]
        progress = counter(definition['counter_id'])
        level = definition['required_level'] <= player['level'] <= definition['maximum_level']
        claimed = bool(player.get('objective_claims', {}).get(key, {}).get(progress['period']['key']))
        counter_definition = rules['counters'][definition['counter_id']]
        grace = (not counter_definition.get('period', {}).get('repeat', True) and
                 progress['period']['ends_at'] is not None and
                 progress['period']['ends_at'] <= now < progress['period']['ends_at'] + definition.get('claim_grace_seconds', 0))
        available = level and (progress['period']['active'] or grace) and progress['tracked']
        entries[key] = {'counter_id': definition['counter_id'], 'threshold': definition['threshold'],
            'progress': progress['value'], 'tracked': progress['tracked'], 'period': deepcopy(progress['period']),
            'level_available': level, 'available': available, 'claimed': claimed,
            'claimable': available and not claimed and progress['value'] >= definition['threshold']}
        return entries[key]
    for key in rules['counters']:
        counter(key)
    for key in rules['entries']:
        entry(key)
    updated = max([player.get('objectives_updated_at', player['created_at'])] +
                  [row['period']['starts_at'] for row in counters.values()
                   if row['period']['starts_at'] is not None and row['period']['starts_at'] <= now])
    return {'counters': counters, 'objectives': entries, 'server_time': now, 'updated_at': updated}


def record(player, rules, command, parameters, result, transaction, now):
    battle = result.get('battle', {})
    entities = (result.get('objective_ids', []) if command == 'objective.claim' else
                [row['calendar_id'] for row in result.get('calendar_awards', [])] if command == 'calendar.advance' else
                [parameters.get('item_id')] if command == 'equipment.craft' else
                [parameters.get('mission_id') or parameters.get('character_id') or parameters.get('offer_id') or
                 parameters.get('tier_id') or parameters.get('category_id') or parameters.get('raid_id') or parameters.get('product_id') or
                 parameters.get('tower_id') or battle.get('tower_id') or battle.get('mission_id')])
    for key, definition in rules['counters'].items():
        if definition['source']['kind'] != 'activity':
            continue
        window = period(definition, player, now)
        if not window['active']:
            continue
        delta = 0
        for rule in definition['source']['rules']:
            if not rule.get('required_level', 1) <= player['level'] <= rule.get('maximum_level', 10000):
                continue
            if rule['command'] != command or ('entity_ids' in rule and not set(rule['entity_ids']) & set(entities)):
                continue
            if 'outcome' in rule and parameters.get('outcome', battle.get('outcome')) != rule['outcome']:
                continue
            measure = rule['measure']
            if measure in ('wallet_spent', 'inventory_spent', 'inventory_earned'):
                value = transaction[measure].get(rule['currency_id'] if measure == 'wallet_spent' else rule['item_id'], 0)
            elif measure == 'login':
                value = int(result.get('new_login', False))
            elif measure == 'alliance_experience':
                value = result.get('alliance_experience', 0)
            elif measure == 'equipped':
                value = len(parameters.get('slots', parameters.get('plan', [])))
            elif measure == 'quantity':
                value = len(set(entities) & set(rule.get('entity_ids', entities))) if command in ('objective.claim', 'calendar.advance') else result.get('quantity', 1)
            else:
                value = 1
            delta = integer(delta + value * rule.get('multiplier', 1))
        if delta:
            windows = player.setdefault('activity_windows', {}).setdefault(key, {})
            windows[window['key']] = integer(windows.get(window['key'], 0) + delta)
    player['objectives_updated_at'] = now


def claim(service, player, parameters, awarded, now):
    object_fields(parameters, ('objective_ids',), ('period_keys',))
    rules = service.catalog.get('objectives')
    if rules is None:
        fail('feature_unavailable', 'Objectives are not configured', 404)
    keys = parameters['objective_ids']
    if not isinstance(keys, list) or not 1 <= len(keys) <= 100:
        fail('invalid_objectives', 'Claim 1–100 distinct objectives')
    for key in keys:
        identifier(key)
    if len(set(keys)) != len(keys):
        fail('invalid_objectives', 'Objective claim IDs must be distinct')
    state = view(player, rules, now)
    expected = parameters.get('period_keys')
    if 'period_keys' in parameters and (not isinstance(expected, dict) or set(expected) != set(keys) or
                                 any(not isinstance(value, str) for value in expected.values())):
        fail('invalid_period', 'Expected periods must cover exactly the claimed objectives')
    for key in keys:
        if key not in state['objectives']:
            fail('objective_not_found', 'Objective is undefined', 404)
        if expected is not None and expected[key] != state['objectives'][key]['period']['key']:
            fail('objective_period_conflict', 'Objective belongs to a different period', 409)
        if not state['objectives'][key]['claimable']:
            fail('objective_not_earned', 'Objective is unavailable, unearned or already claimed', 409)
    bundles = []
    for key in keys:
        reward = rules['entries'][key]['rewards']
        service._reward(player, reward)
        bundles.append((reward, 1))
        window = state['objectives'][key]['period']['key']
        player.setdefault('objective_claims', {}).setdefault(key, {})[window] = True
    return {'objective_ids': deepcopy(keys), 'rewards': awarded(*bundles)}

"""Raid closure and deferred reward delivery on the authoritative player ledger."""
from copy import deepcopy
import hashlib

from .catalog import fail, identifier, integer, object_fields, rewards
from .loot import roll
from .raids import project


def validate(policy, raid_rule, catalog):
    object_fields(policy, ('missions_per_team', 'thresholds', 'tier_table_ids', 'consolation_table_id',
                          'difficulty_bonuses', 'mail_duration_seconds'), ('contribution_counter_id',))
    integer(policy['missions_per_team'], 1, 10000)
    integer(policy['mail_duration_seconds'], 1, 31536000)
    thresholds = policy['thresholds']
    if not isinstance(thresholds, list) or not 1 <= len(thresholds) <= 100:
        fail('invalid_catalog', 'Raid settlement thresholds must be ordered and bounded')
    for value in thresholds:
        integer(value, 0, 100)
    if thresholds != sorted(thresholds):
        fail('invalid_catalog', 'Raid settlement thresholds must be ordered')
    if not isinstance(policy['tier_table_ids'], list) or len(policy['tier_table_ids']) != len(thresholds):
        fail('invalid_catalog', 'Raid settlement tables must cover all thresholds')
    tables = list(policy['tier_table_ids'])
    if policy['consolation_table_id'] is not None:
        tables.append(policy['consolation_table_id'])
    levels = policy['difficulty_bonuses']
    if not isinstance(levels, list) or len(levels) != len(raid_rule['difficulties']):
        fail('invalid_catalog', 'Raid settlement bonuses must cover each difficulty')
    for level in levels:
        object_fields(level, ('tier_rewards', 'completion_bonuses', 'first_completion_bonuses',
                             'first_table_id', 'first_table_minimum_percent'), ('tier_contribution_points',))
        rewards(level['tier_rewards'])
        integer(level.get('tier_contribution_points', 0))
        # Original configuration uses values above 100 (commonly 200) to
        # disable a first-clear table. Preserve that unreachable threshold;
        # clamping it to 100 would mint an otherwise unavailable reward.
        integer(level['first_table_minimum_percent'], 0, 10000)
        if level['first_table_id'] is not None:
            tables.append(level['first_table_id'])
        for field in ('completion_bonuses', 'first_completion_bonuses'):
            if not isinstance(level[field], list) or len(level[field]) > 100:
                fail('invalid_catalog', 'Raid completion bonuses must be bounded lists')
            for bonus in level[field]:
                object_fields(bonus, ('minimum_percent', 'rewards'), ('contribution_points',))
                integer(bonus['minimum_percent'], 0, 100)
                rewards(bonus['rewards'])
                integer(bonus.get('contribution_points', 0))
    allowed = {identifier(policy['contribution_counter_id'])} if 'contribution_counter_id' in policy else set()
    for table_id in tables:
        if identifier(table_id) not in catalog.get('loot_tables', {}):
            fail('invalid_catalog', 'Raid settlement reward table is unavailable')
        for choices in catalog['loot_tables'][table_id]['groups']:
            for choice in choices:
                for item in choice['items']:
                    if choice['weight'] and item['weight'] and set(item.get('counters', {})) - allowed:
                        fail('invalid_catalog', 'Raid settlement counter has no declared owner')


def ready(raid, rule, now):
    policy = rule.get('settlement')
    return policy is not None and raid['status'] == 'active' and (
        now >= raid['expires_at'] or sum(len(nodes) for nodes in raid['completed'].values()) >=
        policy['missions_per_team'] * rule['team_count'])


def settle(service, player, raid, now, awarded):
    rule = service.catalog['raids'][raid['raid_id']]
    if not ready(raid, rule, now):
        fail('raid_settlement_unavailable', 'Raid closure requires expiry or full completion', 409)
    policy = rule['settlement']
    count = sum(len(nodes) for nodes in raid['completed'].values())
    total = policy['missions_per_team'] * rule['team_count']
    if count > total:
        fail('invalid_raid_completion', 'Raid completion exceeds authored total', 409)
    percent = count * 100 / total if raid['team_index'] is not None else 0
    tier, table_ids, bundles, rolls, bonus_selections = None, [], [], [], []
    bonus_points = 0
    first_table_ids = []
    if raid['team_index'] is not None:
        eligible = [i for i, threshold in enumerate(policy['thresholds']) if count * 100 >= threshold * total]
        if eligible:
            tier = max(eligible) + 1
            table_ids.append(policy['tier_table_ids'][tier - 1])
        elif policy['consolation_table_id'] is not None:
            table_ids.append(policy['consolation_table_id'])
        level = policy['difficulty_bonuses'][raid['difficulty']]
        best = max((row['settlement']['completion_percent'] for row in player['raids'].values()
            if row['instance_id'] != raid['instance_id'] and row['raid_id'] == raid['raid_id'] and
            row['alliance_id'] == raid['alliance_id'] and row['status'] == 'ended' and
            row['difficulty'] == raid['difficulty'] and row.get('settlement')), default=-1)
        if tier is not None:
            bundles.append(level['tier_rewards'])
            bonus_points += level.get('tier_contribution_points', 0)
            bonus_selections.append({'kind': 'tier', 'index': 0})
        for kind, field in (('completion', 'completion_bonuses'), ('first_completion', 'first_completion_bonuses')):
            for index, bonus in enumerate(level[field]):
                if percent >= bonus['minimum_percent'] and (kind == 'completion' or best < bonus['minimum_percent']):
                    bundles.append(bonus['rewards'])
                    bonus_points += bonus.get('contribution_points', 0)
                    bonus_selections.append({'kind': kind, 'index': index})
        if level['first_table_id'] and best < level['first_table_minimum_percent'] <= percent:
            first_table_ids.append(level['first_table_id'])
        bonus_bundles, bundles = bundles, []
        for prefix, references in (('raid-end:', table_ids), ('raid-first:', first_table_ids)):
            if prefix == 'raid-first:':
                bundles.extend(bonus_bundles)
            for reference in references:
                result = roll(service.catalog['loot_tables'], reference, prefix + raid['instance_id'] + ':' + reference)
                rolls.append(result)
                bundles.append(result['rewards'])
    bundle = awarded(*((bundle, 1) for bundle in bundles))
    # Validate a deliverable grant before closing; inventory is only awarded by
    # the later mail claim. Empty authored tables remain empty, without fallback.
    service._reward(deepcopy(player), bundle)
    points = integer(bonus_points + sum(row['counters'].get(policy.get('contribution_counter_id'), 0) for row in rolls))
    ended_at = min(now, raid['expires_at'])
    payout = {'rank': 1 if raid['team_index'] is not None else None, 'completion_percent': percent,
        'tier': tier, 'table_ids': table_ids, 'first_table_ids': first_table_ids,
        'bonus_selections': bonus_selections,
        'rewards': bundle, 'contribution_points': points, 'loot_rolls': rolls,
        'ended_at': ended_at, 'reason': 'full_clear' if count == total else 'expired'}
    raid.update(status='ended', ended_at=ended_at, locks={}, settlement=payout)
    if raid['team_index'] is not None:
        mid = hashlib.sha256(('raid-mail:' + raid['instance_id']).encode()).hexdigest()[:24]
        bid = hashlib.sha256(('raid-box:' + raid['instance_id']).encode()).hexdigest()[:24]
        has_rewards = bool(points or any(bundle[field] for field in ('wallet', 'inventory', 'experience')) or bundle.get('signals'))
        player.setdefault('mail', {})[mid] = {'message_id': mid, 'gift_id': bid if has_rewards else None,
            'kind': 'raid_result', 'instance_id': raid['instance_id'], 'created_at': ended_at,
            'expires_at': ended_at + policy['mail_duration_seconds'], 'rewards': deepcopy(bundle),
            'contribution_points': points, 'read_at': None, 'claimed_at': None, 'deleted': False}
        payout['message_id'] = mid
    return {'raid': project(player, rule, raid, now), 'settlement': deepcopy(payout), 'server_time': now}


def apply(service, player, parameters, now, awarded):
    object_fields(parameters, ('instance_id',))
    raid = player.get('raids', {}).get(identifier(parameters['instance_id']))
    if raid is None:
        fail('raid_not_found', 'Raid instance does not belong to this player', 404)
    if raid['status'] == 'ended' and raid.get('settlement'):
        return {'raid': project(player, service.catalog['raids'][raid['raid_id']], raid, now),
                'settlement': deepcopy(raid['settlement']), 'server_time': now}
    return settle(service, player, raid, now, awarded)


def refresh(service, player, parameters, now, awarded):
    object_fields(parameters, ())
    closed = []
    for raid in player.get('raids', {}).values():
        if ready(raid, service.catalog['raids'][raid['raid_id']], now):
            closed.append(settle(service, player, raid, now, awarded)['raid'])
    return {'closed_raids': closed, 'server_time': now}

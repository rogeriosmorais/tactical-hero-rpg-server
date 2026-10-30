"""Trusted offline legacy-player cutover; never exposed as a player command.

The private translator must account for every original profile section and
translate past idempotency keys. No native source blobs reach this service.
"""
from copy import deepcopy
import calendar as calendar_module
from datetime import datetime, timezone
import hashlib
import json

from .catalog import fail, identifier, integer, amounts, object_fields
from .player_progression import level_for_experience


CORE = frozenset(('player_id', 'display_name', 'version', 'level', 'experience',
                  'wallet', 'inventory', 'characters', 'missions', 'offer_purchases'))
OPTIONAL = frozenset((
    'created_at', 'login_days', 'activity_windows', 'objective_claims', 'level_reached_at',
    'login_streak_seed',
    'reset_offset_seconds', 'alternate_reset', 'access_grants', 'alliance_id',
    'alliance_rank', 'alliance_sequence', 'alliance_bootstrapped',
    'alliance_unlocked_at', 'alliance_donations', 'alliance_donation_cooldown_until',
    'alliance_boosts', 'alliances', 'appearance_id', 'client_settings',
    'free_crates', 'guides', 'squads', 'squads_updated_at',
    'offer_daily_purchases', 'raid_energy_refills', 'raids',
    'calendar_progress', 'calendar_receipts', 'challenge_attempts',
    'level_reward_claims', 'objectives_updated_at', 'reward_signals',
    'pass_claims', 'pass_sources', 'mail', 'blitz', 'ranked_arena',
    'seasonal_boost', 'dark_dimension', 'roguelite', 'towers',
    'historic_objective_claim_imports', 'historic_promotion_imports',
    'historic_signal_imports',
))


def validate_calendar_history(service, snapshot):
    """Historical calendar claims must match the saved sequence, not reset it."""
    definitions = service.catalog.get('calendars')
    if definitions is None:
        if 'calendar_progress' in snapshot or 'calendar_receipts' in snapshot:
            fail('cutover_calendar', 'Historical attendance has no configured public calendar', 409)
        return
    progress = snapshot.get('calendar_progress')
    receipts = snapshot.get('calendar_receipts')
    if (not isinstance(progress, dict) or not isinstance(receipts, dict) or
            set(progress) != set(receipts) or set(progress) - set(definitions)):
        fail('cutover_calendar', 'Historical attendance progress and receipt ledgers are incomplete', 409)
    today = min(int(service.clock()) // 86400, 2500000)
    for key, saved in progress.items():
        history = receipts[key]
        definition = definitions[key]
        if (not isinstance(saved, dict) or set(saved) != {'step', 'period', 'last_day'} or
                not isinstance(history, dict) or not history or len(history) > 100000):
            fail('cutover_calendar', 'Historical calendar board lacks complete progress and receipts', 409)
        integer(saved['step'], 1, 1000000)
        integer(saved['last_day'], 0, today)
        if not isinstance(saved['period'], str):
            fail('cutover_calendar', 'Historic calendar period is not normalized', 409)
        ordered = []
        days = set()
        for receipt_key, row in history.items():
            if (not isinstance(row, dict) or set(row) !=
                    {'day', 'period', 'step', 'imported_legacy'} or row['imported_legacy'] is not True):
                fail('cutover_calendar', 'Historic calendar must use unredeemable trusted receipts', 409)
            day = integer(row['day'], 0, today)
            step = integer(row['step'], 1, 1000000)
            period = (datetime.fromtimestamp(day * 86400, timezone.utc).strftime('%Y-%m')
                      if definition['mode'] == 'monthly' else 'lifetime')
            if (row['period'] != period or receipt_key != period + ':' + str(day) or day in days):
                fail('cutover_calendar', 'Historic attendance day or period does not match receipt', 409)
            days.add(day)
            ordered.append((day, step, period))
        ordered.sort()
        previous = None
        reward_count = len(definition['rewards'])
        for day, step, period in ordered:
            if previous is None or (definition['mode'] == 'monthly' and previous[2] != period) or (
                    definition['mode'] == 'consecutive' and day > previous[0] + 1):
                expected_step = 1
            elif definition['mode'] == 'consecutive':
                expected_step = previous[1] % reward_count + 1
            else:
                expected_step = previous[1] + 1
            if step != expected_step or (definition['mode'] == 'finite' and step > reward_count):
                fail('cutover_calendar', 'Historic calendar paid sequence is not consistent', 409)
            if definition['mode'] == 'monthly':
                current = datetime.fromtimestamp(day * 86400, timezone.utc)
                if step > calendar_module.monthrange(current.year, current.month)[1]:
                    fail('cutover_calendar', 'Historic monthly attendance exceeds month length', 409)
            previous = day, step, period
        if (saved['last_day'], saved['step'], saved['period']) != previous:
            fail('cutover_calendar', 'Historic latest calendar claim disagrees with saved board', 409)


def validate_challenge_history(service, snapshot):
    """Never refill a saved section's current UTC-day quota during import."""
    limits = service.catalog.get('challenge_limits')
    state = snapshot.get('challenge_attempts')
    if limits is None:
        if 'challenge_attempts' in snapshot:
            fail('cutover_challenge', 'Saved Challenge attempts lack a public section policy', 409)
        return
    if not isinstance(state, dict) or set(state) != {'day_start', 'used'}:
        fail('cutover_challenge', 'Configured Challenge limits require explicit saved UTC-day usage', 409)
    now = int(service.clock())
    day = integer(state['day_start'], 0, now // 86400 * 86400)
    if day % 86400 or not isinstance(state['used'], dict) or set(state['used']) - set(limits):
        fail('cutover_challenge', 'Historic Challenge day or section usage is not normalized', 409)
    for section, count in state['used'].items():
        integer(count, 1, limits[section])


def validate_guide_history(service, snapshot):
    """A restored account cannot replay already acknowledged native guides."""
    definitions = service.catalog.get('guides')
    history = snapshot.get('guides')
    if definitions is None:
        if 'guides' in snapshot and history != {}:
            fail('cutover_guides', 'Original tutorial history has no public guide policy', 409)
        return
    if not isinstance(history, dict) or set(history) != set(definitions):
        fail('cutover_guides', 'Every authored guide needs complete saved completion state', 409)
    for key, state in history.items():
        if (not isinstance(state, dict) or set(state) - {'completed', 'checkpoint'} or
                'completed' not in state or type(state['completed']) is not bool):
            fail('cutover_guides', 'Historic guide completion flag has an unsupported shape', 409)
        if 'checkpoint' in state:
            available = {row['checkpoint'] for row in definitions[key]['events'].values()
                         if 'checkpoint' in row}
            if not isinstance(state['checkpoint'], str) or state['checkpoint'] not in available:
                fail('cutover_guides', 'Historic tutorial savepoint has no authored public event', 409)


def validate_client_setting_history(service, snapshot):
    """Do not silently restore defaults instead of saved client preferences."""
    definitions = service.catalog.get('client_settings')
    if definitions is None:
        if 'client_settings' in snapshot:
            fail('cutover_client_settings', 'Historic settings lack a typed public policy', 409)
        return
    saved = snapshot.get('client_settings')
    if not isinstance(saved, dict) or set(saved) != set(definitions):
        fail('cutover_client_settings', 'Original settings require every authored typed value', 409)
    from .client_settings import check
    for name, value in saved.items():
        check(value, definitions[name]['kind'])


def validate_appearance_history(service, snapshot):
    """Never import a selected portrait without its original earned item."""
    policy = service.catalog.get('appearance')
    if policy is None:
        if 'appearance_id' in snapshot:
            fail('cutover_appearance', 'Selected historic appearance lacks a public policy', 409)
        return
    selected = snapshot.get('appearance_id')
    if not isinstance(selected, str) or selected not in policy['choices']:
        fail('cutover_appearance', 'Historic selected portrait is missing or unmapped', 409)
    item = policy['choices'][selected].get('required_item')
    if item is not None and snapshot['inventory'].get(item, 0) <= 0:
        fail('cutover_appearance', 'Historic selected portrait has no owned item', 409)


def validate_pass_history(service, snapshot):
    """Accept historic pass claims only against earned public objective state."""
    sources = snapshot.get('pass_sources', {})
    claims = snapshot.get('pass_claims', {})
    if not isinstance(sources, dict) or not isinstance(claims, dict):
        fail('cutover_pass_history', 'Historic pass state must be normalized objects', 409)
    rules = service.catalog.get('passes', {})
    if (set(sources) | set(claims)) - set(rules):
        fail('cutover_pass_history', 'An unconfigured historic pass cannot be imported', 409)
    from .passes import read_one
    for pass_id in set(sources) | set(claims):
        if pass_id not in rules:
            fail('cutover_pass_history', 'Historic pass is not configured', 409)
        saved = sources.get(pass_id, {})
        awarded = claims.get(pass_id, {})
        if not isinstance(saved, dict) or not isinstance(awarded, dict):
            fail('cutover_pass_history', 'Historic pass periods must be objects', 409)
        allowed = {reward['reward_id']: reward for reward in rules[pass_id]['rewards']}
        for period_key in set(saved) | set(awarded):
            data = saved.get(period_key)
            if (not isinstance(period_key, str) or not isinstance(data, dict) or
                    set(data) != {'selected_at', 'premium'} or
                    type(data['premium']) is not bool):
                fail('cutover_pass_history', 'Historic pass subscription is incomplete', 409)
            integer(data['selected_at'])
            rows = awarded.get(period_key, [])
            if (not isinstance(rows, list) or len(rows) > len(allowed) or
                    any(not isinstance(key, str) or key not in allowed for key in rows) or
                    len(set(rows)) != len(rows)):
                fail('cutover_pass_history', 'Historic claimed rewards are invalid', 409)
            try:
                history = read_one(snapshot, service.catalog, pass_id, int(service.clock()), period_key)
            except (ValueError, KeyError, TypeError, OverflowError) as error:
                fail('cutover_pass_history', 'Historic pass period cannot be reconstructed', 409)
            if (history['period']['starts_at'] is not None and
                    data['selected_at'] < history['period']['starts_at']):
                fail('cutover_pass_history', 'Historic pass selection precedes its earning period', 409)
            for key in rows:
                reward = allowed[key]
                if reward['threshold'] > history['points'] or (reward['track'] == 'premium' and not data['premium']):
                    fail('cutover_pass_history', 'Historic claimed prize lacks earned eligibility', 409)


def validate_snapshot(service, snapshot):
    if not isinstance(snapshot, dict) or not CORE <= snapshot.keys():
        fail('cutover_incomplete', 'A complete normalized player snapshot is required', 409)
    if set(snapshot) - CORE - OPTIONAL:
        fail('cutover_unmapped_state', 'Private or unknown profile fields must not enter the server', 409)
    player_id = identifier(snapshot['player_id'])
    integer(snapshot['version'])
    if not isinstance(snapshot['display_name'], str) or not snapshot['display_name'].strip() or len(snapshot['display_name']) > 64:
        fail('cutover_invalid_name', 'Source display name is invalid', 409)
    level = integer(snapshot['level'], 1, service.catalog['maximum_player_level'])
    if level_for_experience(service.catalog, integer(snapshot['experience'])) != level:
        fail('cutover_level', 'Source level and earned experience disagree', 409)
    amounts(snapshot['wallet'])
    amounts(snapshot['inventory'])
    allowed = set(service.catalog['starting_wallet']) | set(service.catalog.get('resource_rules', {}))
    if not set(snapshot['wallet']) <= allowed:
        fail('cutover_currency', 'An unmapped source wallet balance would be lost', 409)
    if not set(service.catalog['starting_wallet']) <= set(snapshot['wallet']):
        fail('cutover_incomplete', 'An application wallet currency is missing', 409)
    if 'objectives' in service.catalog:
        for required in ('created_at', 'login_days', 'activity_windows',
                         'objective_claims', 'level_reached_at'):
            if required not in snapshot:
                fail('cutover_incomplete', 'Objective progress cannot be reset during cutover', 409)
    if 'level_rewards' in service.catalog and 'level_reward_claims' not in snapshot:
        fail('cutover_incomplete', 'Previously paid commander bonuses require an explicit historic ledger', 409)
    validate_calendar_history(service, snapshot)
    validate_challenge_history(service, snapshot)
    validate_guide_history(service, snapshot)
    validate_client_setting_history(service, snapshot)
    validate_appearance_history(service, snapshot)
    if 'access_rules' in service.catalog:
        if 'access_grants' not in snapshot:
            fail('cutover_incomplete', 'Historical feature unlocks require the original grant ledger', 409)
        grants = snapshot['access_grants']
        rules = service.catalog['access_rules']
        if not isinstance(grants, dict) or set(grants) - set(rules):
            fail('cutover_access', 'Historic feature access contains an unrecognized grant', 409)
        from .access import eligible
        for key, granted_at in grants.items():
            integer(granted_at, 0, int(service.clock()))
        for key, definition in rules.items():
            if key not in grants and (eligible(snapshot, definition) or
                                     snapshot['inventory'].get(definition['item_id'], 0) > 0):
                fail('cutover_access', 'Historic feature state would grant an unearned entitlement on replay', 409)
    elif 'access_grants' in snapshot:
        fail('cutover_access', 'Historical feature grants have no public access policy', 409)
    if 'login_streak_seed' in snapshot:
        seed = snapshot['login_streak_seed']
        if ('objectives' not in service.catalog or not isinstance(seed, dict) or
                set(seed) != {'day', 'length'} or
                not isinstance(snapshot.get('login_days'), list) or
                type(snapshot.get('created_at')) is not int):
            fail('cutover_login_history', 'Original login-streak anchor lacks complete objective history', 409)
        day = integer(seed['day'], 0, int(service.clock()) // 86400)
        integer(seed['length'], 0, 1000000)
        days = snapshot['login_days']
        if (not days or len(days) > 1000000 or
                any(type(value) is not int or value < day or value > int(service.clock()) // 86400
                    for value in days) or len(days) != len(set(days)) or
                day not in days or snapshot['created_at'] > day * 86400 + 86399):
            fail('cutover_login_history', 'Historic login days must be observed and include the streak anchor', 409)
        if not any(row.get('source', {}).get('kind') == 'login_streak'
                   for row in service.catalog['objectives']['counters'].values()):
            fail('cutover_login_history', 'Source login streak lacks a public objective definition', 409)
    if 'level_reward_claims' in snapshot:
        receipts = snapshot['level_reward_claims']
        policy = service.catalog.get('level_rewards')
        if (policy is None or not isinstance(receipts, dict) or
                len(receipts) > level):
            fail('cutover_level_rewards', 'Historic commander reward ledger has no public policy', 409)
        for key, receipt in receipts.items():
            if (not isinstance(key, str) or not 1 <= len(key) <= 5 or not key.isascii() or
                    not key.isdecimal() or str(int(key)) != key or
                    not 2 <= int(key) <= level or key not in policy['awards'] or
                    not isinstance(receipt, dict) or
                    receipt.get('policy_id') != policy['policy_id']):
                fail('cutover_level_rewards', 'Historic commander reward is not mapped or earned', 409)
            if set(receipt) == {'policy_id', 'historic'}:
                if receipt['historic'] is not True:
                    fail('cutover_level_rewards', 'Historic commander award flag is invalid', 409)
            elif set(receipt) == {'policy_id', 'at'}:
                integer(receipt['at'], 0, int(service.clock()))
            else:
                fail('cutover_level_rewards', 'Historic award cannot invent an exact timestamp', 409)
    if not isinstance(snapshot['characters'], dict) or not isinstance(snapshot['missions'], dict):
        fail('cutover_incomplete', 'Character and mission state must be objects', 409)
    for key, state in snapshot['characters'].items():
        if identifier(key) not in service.catalog['characters'] or not isinstance(state, dict):
            fail('cutover_character', 'Source character has no public definition', 409)
        integer(state.get('level'), 1, service.catalog['characters'][key]['maximum_level'])
        integer(state.get('power'), 1)
        if 'development' in service.catalog and key in service.catalog['development']['characters']:
            for required in ('experience', 'rank', 'abilities', 'tier', 'equipment'):
                if required not in state:
                    fail('cutover_incomplete', 'Character development cannot be reset during cutover', 409)
        if 'enhancement' in state:
            rules = service.catalog.get('enhancements')
            if not rules or key not in rules['characters']:
                fail('cutover_enhancement', 'Historical enhancement state has no public character policy', 409)
            iso = state['enhancement']
            if (not isinstance(iso, dict) or set(iso) !=
                    {'items', 'tier', 'class_id', 'learned', 'swap_available_at', 'quality'}):
                fail('cutover_enhancement', 'Historical ISO equipment and class state is incomplete', 409)
            rank = integer(iso['tier'], 1, len(rules['tiers']))
            integer(iso['swap_available_at'])
            if (not isinstance(iso['items'], dict) or set(iso['items']) - set(rules['slots'])
                    or not isinstance(iso['learned'], dict)):
                fail('cutover_enhancement', 'Historical ISO slots/classes are invalid', 409)
            for slot, item_id in iso['items'].items():
                definition = rules['items'].get(item_id)
                if (not definition or definition['slot_id'] != slot or
                        definition['role'] != rules['characters'][key] or definition['tier'] > rank):
                    fail('cutover_enhancement', 'Historical ISO crystal or role is unmapped', 409)
            from .enhancements import quality as enhancement_quality
            if integer(iso['quality'], 0, 100) != enhancement_quality(iso, rules):
                fail('cutover_enhancement', 'Historical ISO matrix quality is inconsistent', 409)
            selected = iso['class_id']
            if selected is not None and selected not in rules['classes']:
                fail('cutover_enhancement', 'Historical ISO selected class is unmapped', 409)
            if selected is not None and selected not in iso['learned']:
                fail('cutover_enhancement', 'Historical ISO selected class history is missing', 409)
            for class_id, learned in iso['learned'].items():
                cls = rules['classes'].get(class_id)
                if (cls is None or not isinstance(learned, list) or
                        len(learned) > len(cls['skills'])):
                    fail('cutover_enhancement', 'Historical ISO skill history is invalid', 409)
                expected = [row['skill_id'] for row in cls['skills'][:len(learned)]]
                if learned != expected:
                    fail('cutover_enhancement', 'Historical ISO class skill order is not earned', 409)
    for key, state in snapshot['missions'].items():
        if identifier(key) not in service.catalog['missions'] or not isinstance(state, dict):
            fail('cutover_mission', 'Source mission has no public definition', 409)
        if set(state) != {'wins', 'best_stars'}:
            fail('cutover_mission', 'Imported earned mission clear requires complete public progress', 409)
        # An imported star clear with zero wins would incorrectly pay its
        # first-clear prize again. A zero-star entry cannot establish a past
        # victory, so it must not masquerade as an earned mission clear.
        integer(state['wins'], 1)
        integer(state['best_stars'], 1, 3)
    if not isinstance(snapshot['offer_purchases'], dict):
        fail('cutover_incomplete', 'Source offer purchase state is not represented', 409)
    for offer_id, count in snapshot['offer_purchases'].items():
        definition = service.catalog['offers'].get(identifier(offer_id))
        if definition is None or definition.get('reset_policy') == 'utc_day_once':
            fail('cutover_offers', 'Historical permanent purchase has no compatible public offer', 409)
        integer(count, 1, definition['purchase_limit'])
    if 'offer_daily_purchases' in snapshot:
        history = snapshot['offer_daily_purchases']
        if not isinstance(history, dict) or len(history) > len(service.catalog['offers']):
            fail('cutover_offers', 'Historical daily shop stock is not represented', 409)
        current_day = int(service.clock()) // 86400
        for offer_id, row in history.items():
            definition = service.catalog['offers'].get(identifier(offer_id))
            if definition is None or definition.get('reset_policy') != 'utc_day_once':
                fail('cutover_offers', 'Historical daily stock has no compatible public offer', 409)
            if not isinstance(row, dict) or set(row) != {'day', 'count'}:
                fail('cutover_offers', 'Historical daily shop receipt is incomplete', 409)
            integer(row['day'], 0, current_day)
            integer(row['count'], 1, definition['purchase_limit'])
    if 'squads' in snapshot or 'squads_updated_at' in snapshot:
        if 'squads' not in snapshot or 'squads_updated_at' not in snapshot:
            fail('cutover_incomplete', 'Saved squads and their update time must be preserved together', 409)
        integer(snapshot['squads_updated_at'])
        groups = snapshot['squads']
        if not isinstance(groups, dict) or len(groups) > 100:
            fail('cutover_squads', 'Saved squad groups are invalid', 409)
        if groups:
            if 'squad_groups' not in service.catalog:
                fail('cutover_squads', 'Saved squads have no public policy', 409)
            # Reuse exactly the same ownership, level, formation and earned
            # capacity checks as the normal public squad-save transaction.
            from .squads import save as validate_saved_squads
            validate_saved_squads(deepcopy(snapshot), service.catalog['squad_groups'],
                                  {'groups': groups}, snapshot['squads_updated_at'])
    if 'pass_sources' in snapshot or 'pass_claims' in snapshot:
        validate_pass_history(service, snapshot)
    return player_id


def import_complete_player(service, snapshot, provenance):
    """Import a fully translated public player only into a previously empty ID.

    Every old profile section needs an audited mapping before import. Historical
    neutral operation keys become immutable conflict tombstones; replays fail
    rather than paying the original operation again. No rewards are generated.
    """
    from .service import canonical
    object_fields(provenance, ('source_digest', 'source_sections',
                               'section_map', 'used_command_keys'))
    digest = provenance['source_digest']
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        fail('cutover_source', 'Source fingerprint must be a SHA-256 hash', 409)
    sections, mapping = provenance['source_sections'], provenance['section_map']
    if (not isinstance(sections, list) or not sections or len(sections) > 256 or
            any(not isinstance(value, str) for value in sections) or
            len(set(sections)) != len(sections) or not isinstance(mapping, dict) or
            set(sections) != set(mapping)):
        fail('cutover_incomplete', 'Every source profile section must be mapped', 409)
    mapped_fields = set()
    for section, fields in mapping.items():
        identifier(section)
        if (not isinstance(fields, list) or not fields or len(fields) > len(CORE | OPTIONAL) + 1 or
                any(not isinstance(field, str) or field not in CORE | OPTIONAL | {'legacy_receipts'} for field in fields) or
                len(set(fields)) != len(fields)):
            fail('cutover_incomplete', 'Source section has an unsupported target field', 409)
        mapped_fields.update(fields)
    if not CORE <= mapped_fields | {'legacy_receipts'} or 'legacy_receipts' not in mapped_fields:
        fail('cutover_incomplete', 'Core state and historical operation coverage must be mapped', 409)
    used = provenance['used_command_keys']
    if (not isinstance(used, list) or len(used) > 200000 or
            any(not isinstance(key, str) for key in used) or len(set(used)) != len(used)):
        fail('cutover_receipts', 'Historic command keys must be distinct and bounded', 409)
    for key in used:
        identifier(key)
    player_id = validate_snapshot(service, snapshot)
    if mapped_fields != set(snapshot) | {'legacy_receipts'}:
        fail('cutover_incomplete',
             'Cutover manifest must account for exactly the normalized snapshot fields', 409)
    snapshot_digest = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
    with service.transaction() as connection:
        previous = connection.execute('SELECT data FROM players WHERE id=?', (player_id,)).fetchone()
        if previous:
            saved = json.loads(previous[0])
            if saved.get('legacy_cutover') == {'source_digest': digest,
                                               'snapshot_digest': snapshot_digest,
                                               'receipt_count': len(used)}:
                return {'status': 'already_imported', 'player': saved}
            fail('cutover_conflict', 'Target player is not empty or comes from another source', 409)
        if connection.execute('SELECT 1 FROM commands WHERE player_id=? LIMIT 1', (player_id,)).fetchone():
            fail('cutover_conflict', 'Existing commands prevent migration', 409)
        if connection.execute('SELECT 1 FROM battles WHERE player_id=? LIMIT 1', (player_id,)).fetchone():
            fail('cutover_conflict', 'Existing battles prevent migration', 409)
        player = deepcopy(snapshot)
        player['legacy_cutover'] = {'source_digest': digest,
                                    'snapshot_digest': snapshot_digest,
                                    'receipt_count': len(used)}
        connection.execute('INSERT INTO players VALUES (?,?)', (player_id, canonical(player)))
        for key in used:
            connection.execute('INSERT INTO commands VALUES (?,?,?,?)',
                (player_id, key, 'legacy-cutover-consumed', '{"historic":true}'))
        return {'status': 'imported', 'player': player}

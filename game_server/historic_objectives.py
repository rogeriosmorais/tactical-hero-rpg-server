"""Trusted offline import of already-claimed original achievement periods.

Migrates legacy AchievementRules.migrate_claim_periods authority to the public
objective claim ledger. The private side translates native claim evidence to
neutral objective/window IDs. No public client command or HTTP route exists.
"""
from copy import deepcopy

from .catalog import fail, identifier


def import_claims(service, player_id, claims):
    """Record historic rewards as already claimed, without granting them.

    claims: {neutral_objective_id: [neutral_period_key, ...]}. The source
    extractor conservatively expands ambiguous legacy UTC windows. This
    operation only closes claim rights; never increases balances or counters.
    Safe to retry, even after additional claims have been earned on the server.
    """
    rules = service.catalog.get('objectives')
    if rules is None:
        fail('feature_unavailable', 'Objectives are not configured', 404)
    if not isinstance(claims, dict) or len(claims) > len(rules['entries']):
        fail('invalid_legacy_import', 'Historical objective claims must be a bounded map')
    normalized = {}
    for objective, periods in claims.items():
        if identifier(objective) not in rules['entries']:
            fail('invalid_legacy_import', 'Historic objective is not defined')
        if (not isinstance(periods, list) or len(periods) > 10000 or
                len(periods) != len(set(period for period in periods if isinstance(period, str)))):
            fail('invalid_legacy_import', 'Historic objective periods must be distinct bounded strings')
        counter = rules['counters'][rules['entries'][objective]['counter_id']]
        periodic = 'period' in counter
        values = []
        for key in periods:
            if (not isinstance(key, str) or not 1 <= len(key) <= 32 or
                    (periodic and (not (key.lstrip('-').isdigit() and str(int(key)) == key))) or
                    (not periodic and key != 'lifetime')):
                fail('invalid_legacy_import', 'Historic claim period is not valid for objective')
            values.append(key)
        normalized[objective] = values
    from .service import canonical
    with service.transaction() as connection:
        player = service._player(connection, player_id)
        history = player.setdefault('historic_objective_claim_imports', {})
        current = player.setdefault('objective_claims', {})
        imported = {}
        changed = False
        for objective, windows in normalized.items():
            prior = current.setdefault(objective, {})
            receipts = history.setdefault(objective, {})
            for window in windows:
                if prior.get(window) is not True:
                    prior[window] = True
                    imported.setdefault(objective, []).append(window)
                    changed = True
                # Track the import source independent of current claim
                # success; an already claimed current reward is not reissued.
                if receipts.get(window) is not True:
                    receipts[window] = True
                    changed = True
        if changed:
            player['version'] += 1
            connection.execute('UPDATE players SET data=? WHERE id=?',
                               (canonical(player), player_id))
        return {'imported': imported, 'historic_imports': deepcopy(history)}

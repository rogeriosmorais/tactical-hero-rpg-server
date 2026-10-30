"""Trusted cutover of earned reward-tag receipts from the retired game ledger.

The old backend.event_tags.award recorded tagged, nonspendable actions. This
module owns their authoritative successor on the standalone player's ledger.
Callers must translate native tag names to neutral labels *before* import.
There is deliberately no public HTTP/player command that can forge imports.
"""
from copy import deepcopy
import hashlib

from .catalog import fail, identifier, integer
from .service import canonical


def import_historic_reward_signals(service, player_id, records):
    """Import already-earned signal receipts exactly once, never grant currency.

    records: [{receipt_id, at, labels, quantity}]. One source action remains
    one signal even when it has several labels. The import journal persists
    its canonical digest, so retries are exact and conflicting replays fail.
    """
    if not isinstance(records, list) or len(records) > 10000:
        fail('invalid_legacy_signals', 'Historic signal receipts require a bounded list')
    normalized, seen = [], set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {'receipt_id', 'at', 'labels', 'quantity'}:
            fail('invalid_legacy_signals', 'Historic signal receipt fields are invalid')
        receipt_id = identifier(record['receipt_id'])
        if receipt_id in seen:
            fail('duplicate_legacy_signal', 'A historic receipt appears twice in one import')
        seen.add(receipt_id)
        at = integer(record['at'])
        quantity = integer(record['quantity'], 1)
        labels = record['labels']
        if (not isinstance(labels, list) or not 1 <= len(labels) <= 64 or
                any(not isinstance(label, str) for label in labels)):
            fail('invalid_legacy_signals', 'A historic reward action needs source-translated labels')
        for label in labels:
            identifier(label)
        if len(labels) != len(set(labels)):
            fail('invalid_legacy_signals', 'A historic reward action requires distinct labels')
        action = {'receipt_id': receipt_id, 'at': at, 'labels': deepcopy(labels), 'quantity': quantity}
        normalized.append((receipt_id, action, hashlib.sha256(canonical(action).encode()).hexdigest()))
    with service.transaction() as connection:
        player = service._player(connection, player_id)
        journal = player.setdefault('historic_signal_imports', {})
        occupied = {item['signal_id'] for item in player.get('reward_signals', [])}
        added = []
        for receipt_id, action, digest in normalized:
            prior = journal.get(receipt_id)
            if prior is not None:
                if prior != digest:
                    fail('historic_signal_conflict', 'Historic reward action changed after import', 409)
                continue
            signal_id = 'legacy.' + receipt_id
            if signal_id in occupied:
                fail('historic_signal_conflict', 'Historic signal identity already exists', 409)
            imported = {'signal_id': signal_id, 'at': action['at'],
                        'labels': deepcopy(action['labels']), 'quantity': action['quantity']}
            player.setdefault('reward_signals', []).append(imported)
            journal[receipt_id] = digest
            occupied.add(signal_id)
            added.append(imported)
        if added:
            player['version'] += 1
            connection.execute('UPDATE players SET data=? WHERE id=?', (canonical(player), player_id))
        return {'imported': deepcopy(added), 'imported_count': len(added),
                'journal_count': len(journal)}

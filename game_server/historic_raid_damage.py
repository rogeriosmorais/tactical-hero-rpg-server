"""Trusted cutover of earned legacy raid damage from original battle receipts.

The original gateway owns source transcript parsing and neutral node/team
translation. This public importer only commits previously earned, validated
damage aggregates, matching the old backend.raid_damage.node_total purpose.
There is intentionally no client command or HTTP entry point.
"""
from copy import deepcopy

from .catalog import fail, identifier, integer
from .service import canonical


def import_historic_raid_damage(service, player_id, instance_id, totals):
    """Import [{team_index, node_id, damage}] once into an existing raid.

    A new raid instance and any other imported state must already be restored
    by a separate trusted cutover. Never overlay a node that has public combat
    activity: doing so could double-count a successful battle or erased room.
    """
    if not isinstance(totals, list) or not 1 <= len(totals) <= 10000:
        fail('invalid_historic_raid_damage', 'Legacy raid import needs bounded node totals')
    identifier(player_id)
    identifier(instance_id)
    validated = {}
    for row in totals:
        if not isinstance(row, dict) or set(row) != {'team_index', 'node_id', 'damage'}:
            fail('invalid_historic_raid_damage', 'Legacy raid total fields are invalid')
        team = integer(row['team_index'], 0, 100000)
        node = identifier(row['node_id'])
        damage = integer(row['damage'])
        key = (str(team), node)
        if key in validated:
            fail('duplicate_historic_raid_damage', 'Legacy node total appears twice')
        validated[key] = damage
    with service.transaction() as connection:
        player = service._player(connection, player_id)
        raid = player.get('raids', {}).get(instance_id)
        if raid is None:
            fail('raid_not_found', 'Historic raid instance must be imported before its damage', 404)
        rule = service.catalog.get('raids', {}).get(raid['raid_id'])
        if rule is None:
            fail('raid_not_found', 'Historic raid definition is unavailable', 404)
        journal = raid.setdefault('historic_damage_imports', {})
        additions = []
        for (team, node), damage in validated.items():
            if int(team) >= rule['team_count'] or node not in rule['nodes']:
                fail('invalid_historic_raid_damage', 'Legacy raid team/node is outside current policy')
            journal_key = team + ':' + node
            previous = journal.get(journal_key)
            if previous is not None:
                if previous != damage:
                    fail('historic_raid_damage_conflict', 'Already imported node damage changed', 409)
                continue
            current = raid.get('node_damage', {}).get(team, {}).get(node, 0)
            if (current or node in raid.get('node_states', {}).get(team, {}) or
                    node in raid.get('completed', {}).get(team, {})):
                fail('historic_raid_damage_conflict', 'Public combat already owns this node damage', 409)
            additions.append((team, node, damage))
        for team, node, damage in additions:
            raid.setdefault('node_damage', {}).setdefault(team, {})[node] = damage
            raid.setdefault('damage', {})[team] = integer(raid['damage'].get(team, 0) + damage)
            journal[team + ':' + node] = damage
        if additions:
            player['version'] += 1
            connection.execute('UPDATE players SET data=? WHERE id=?', (canonical(player), player_id))
        return {'imported_nodes': [{'team_index': int(team), 'node_id': node, 'damage': damage}
                                   for team, node, damage in additions],
                'total_damage_imported': sum(damage for _, _, damage in additions),
                'journal': deepcopy(journal)}

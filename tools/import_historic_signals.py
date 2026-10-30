"""Trusted maintenance import of neutral legacy event receipts.

Only a previously exported neutral JSON file is accepted. Run explicitly
after preparing an isolated public application database and catalog. This is
not a public HTTP route and cannot alter an unregistered player.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from game_server.historic_signals import import_historic_reward_signals
from game_server.service import GameService


def import_file(database, catalog, player_id, source):
    database, catalog, source = map(Path, (database, catalog, source))
    if not database.is_file() or not catalog.is_file() or not source.is_file():
        raise ValueError('Existing server database, catalog and neutral export are required')
    document = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or set(document) != {'schema_version', 'signals'} or document['schema_version'] != 1:
        raise ValueError('Unsupported neutral signal export schema')
    service = GameService(database, json.loads(catalog.read_text(encoding='utf-8')))
    return import_historic_reward_signals(service, player_id, document['signals'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--player-id', required=True)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args(argv)
    result = import_file(args.database, args.catalog, args.player_id, args.input)
    print(json.dumps({'imported_count': result['imported_count'],
                      'journal_count': result['journal_count']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Trusted offline import of neutral legacy achievement claim receipts.

The server never reads old native achievement IDs or the original profile.
Import is explicit, idempotent, atomic and unavailable through public HTTP.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from game_server.historic_objectives import import_claims
from game_server.service import GameService


def import_file(database, catalog, player_id, source):
    database, catalog, source = map(Path, (database, catalog, source))
    if not all(path.is_file() for path in (database, catalog, source)):
        raise ValueError('Existing server database, catalog and neutral export are required')
    document = json.loads(source.read_text(encoding='utf-8'))
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'claims'}
            or document['schema_version'] != 1):
        raise ValueError('Unsupported neutral achievement export schema')
    service = GameService(database, json.loads(catalog.read_text(encoding='utf-8')))
    return import_claims(service, player_id, document['claims'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--player-id', required=True)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args(argv)
    result = import_file(args.database, args.catalog, args.player_id, args.input)
    print(json.dumps({'imported_count': sum(map(len, result['imported'].values())),
                      'journal_count': sum(map(len, result['historic_imports'].values()))}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

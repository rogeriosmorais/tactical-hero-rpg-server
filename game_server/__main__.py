import argparse
import json
from pathlib import Path

from .http_api import make_server
from .service import GameService


def main():
    parser = argparse.ArgumentParser(description='Independent JSON game application service')
    parser.add_argument('--database', type=Path, default=Path('var/game.sqlite'))
    parser.add_argument('--catalog', type=Path, help='Optional API-v1 gameplay catalog; default is authored demo content')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18080)
    parser.add_argument('--service-token', help='Independently chosen internal service credential')
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding='utf-8')) if args.catalog else None
    service = GameService(args.database, catalog)
    with make_server(service, args.host, args.port, args.service_token) as server:
        print(json.dumps({'ready': True, 'host': args.host, 'port': server.server_port, 'api_version': 1}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()

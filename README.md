# Public game server

This repository contains a standalone game application service and its own
versioned JSON API. It uses the Python standard library and independently
authored demonstration content. It has no client protocol decoder, original
game files, captured responses, proprietary keys, sibling imports or asset links.

## Personal hobby project

This is a personal pet project for learning, experimentation, and enjoyment.
It is maintained in the author's spare time, without a business or revenue
model. The author does not charge for this repository, sell access to it, or
solicit or accept payments or donations for this project.

There is no commitment to provide hosting, technical support, updates, security
fixes, compatibility, or continued maintenance. Features and availability may
change, and development may stop at any time.

## Independence and third-party rights

This is an independent personal project. It is not affiliated with,
endorsed by, sponsored by, or approved by any third-party game developer,
publisher, or intellectual property owner.

References to third-party products or names identify technical context only.
Third-party trademarks and other intellectual property remain the property
of their respective owners. This repository does not provide or grant rights
to proprietary game clients, artwork, characters, audio, game data, keys, or
other third-party materials. Users must obtain any required rights separately.

The project's hobby status and absence of payments do not create permission
to use someone else's intellectual property or an exemption from applicable law.

## License, warranty, and responsibility

The code and documentation in this repository are available under the
[MIT License](LICENSE). The author's noncommercial intent describes this
project; it does not restrict the uses permitted by that license, including
commercial use.

The software is provided **as is**, without warranties, including warranties
of merchantability, fitness for a particular purpose, or noninfringement.
The MIT License contains the full warranty disclaimer and limitation of
liability, subject to applicable law.

Users are responsible for their own modifications, deployments, use of
third-party materials, and compliance with applicable laws and agreements.
Publishing this code does not endorse unlawful activity, infringement,
unauthorized access, or misuse. No statement here guarantees legal clearance
or excludes liability that applicable law does not allow to be excluded.

## Run locally

With Python 3.12 or later, from this directory:

```powershell
python -S tools/check.py
python -S -m game_server --database var/game.sqlite --port 18080
```

No `pip install` step is needed.

API v1 supports players, catalogs, wallets,
inventory, character training, mission execution, sweeping, purchases and
configurable Towers with no account-level unlock gate. An optional authored
`free_crates` catalog section now provides server-owned refill, rotation,
claims and idempotent rewards; it does not load client asset/rule files.
Tower progression,
cooldowns, scoring and rewards are authoritative application transactions.
See [API v1 and catalog schema](API.md).
The server calculates prices and rewards, persists state, and enforces atomic
idempotent commands. The default catalog is a small authored example, not
any proprietary game's content or balance.

An optional authored [Roguelite MVP catalog](examples/roguelite-mvp.json) exercises
replayable runs with 1–5-character squads, seeded five-enemy encounters, permanent
shard choices and repeatable progression earned from victories. Defeat grants
no XP, currency, materials or shard choice; earlier winnings remain permanent. Its rules
and persistence live entirely in this service; it requires no private runtime.
Its authored enemy-level reward bands grant larger currency/material bundles and
shard quantities as opposition levels rise. Rewards are frozen with the issued
encounter and battle. The example still has a three-level cap; it does not define
the complete mode's progression range or economy.
Use a new database when selecting a different catalog.

An optional `--catalog PATH` reads only this API's documented catalog schema.
`--service-token TOKEN` sets a separately chosen application credential;
it is required when binding outside loopback. It is unrelated to client keys.

The isolation test exports only this repository's runtime and core tests,
runs them with `-I -S -B`, audits filesystem/SQLite access, and starts a real
HTTP server.

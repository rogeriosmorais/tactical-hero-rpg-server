# Campaign and challenge eligibility ownership

Legacy `backend/campaign.py` evaluated original mission requirements,
character filters and earned stars against the mutable player roster.
The independent application service now owns the corresponding **optional**
neutral `missions[mission_id].eligibility` policy. Before `mission.begin`
spends energy or issues a battle, it evaluates all authored section, chapter
and node scopes against the same persisted player and selected owned team.
Failure rolls back the command; retries have the existing idempotent receipt.

Each `eligibility` object can contain:

- `minimum_commander_level` and `minimum_mission_stars` (a mapping from
  neutral mission IDs to required earned stars, 1–3).
- `must_include` (distinct neutral character IDs required in the squad).
- `each_character` (a normalized filter that every selected hero must pass).
- `any_character` (one or more alternative filters) with optional
  `minimum_matching` (default: all selected heroes).
- `scopes` (up to three independent objects, for section/chapter/node).

Character filters accept `character_ids`, `excluded_character_ids`,
`minimum_level`, `minimum_rank`, `minimum_tier`, and
`minimum_promotion_rank`. Missing baseline rank/tier means level 1; an
unearned promotion begins at 0. The server does not read original names,
traits, client mission schemas, captured responses or source files.

Private `content.mission_eligibility.compile_registry` resolves the original
source scopes, trait filters and mission aliases into these neutral fields
for explicitly mapped missions. Original weekday gates and unsupported
source filters are **not silently granted**; the compiler rejects them.
Source file reading and actor rendering remain private. The old accepted
runtime still has its own `backend/campaign.py` caller until that runtime
is fully retired; this incremental migration does not authorize deleting
the whole `backend/` directory.

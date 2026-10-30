### Trusted historical objective-claim cutover (no client endpoint)

`game_server.historic_objectives.import_claims` owns idempotent,
transactional import of already-paid legacy claim periods. Only
neutral `{objective_id: [period_key,...]}` evidence is accepted,
validated against the currently configured public objective catalog
before any mutation. Imported periods populate `objective_claims`
and `historic_objective_claim_imports`, bumping player version only
when a new claim/import receipt is preserved. No resources or rewards
are granted. Private source window attribution (including the
legacy UTC-key overlap fallback) remains in the private
`content/achievement_claims.py` translator. The offline entrypoint
`tools/import_historic_objective_claims.py` requires an existing
database, catalog file, player and neutral JSON schema v1 export.
No HTTP or player-issued command can invoke this maintenance import.
# Internal game application API v1

## Original Supplies daily stock ownership

An optional offer `reset_policy: "utc_day_once"` migrates the retained
Supplies `buy_supply` / `SupplyRules.stock` rule. Its `purchase_limit` is
the maximum quantity in **one** batch (the original rule allows up to 10),
not a count of independent purchases. After an accepted `shop.purchase`
the server atomically saves `offer_daily_purchases[offer_id]` with the
UTC day and quantity alongside wallet debit, inventory credit and the
idempotent command receipt. Any other nonce on the same day is rejected,
including after a restart; a new UTC day permits a new batch. Expired
windows are read logically and need no gateway-side reset. Offers without
`reset_policy` keep their original lifetime `purchase_limit` semantics.

Original `/shop/buy` field aliases, source shop/slot IDs and native prize
shape remain private. The gateway optionally compiles selected preserved
Supplies slots to normalized cost, reward, batch quantity and reset policy,
requiring an **exact catalog match** before any public transaction.
The retained legacy backend handler is not retired until old runtime
dependencies are removed; no native store/catalog acceptance is asserted.

## Legacy DS state and database backup ownership

The optional `client_settings` catalog defines 1–128 neutral setting IDs with
strict boolean, signed 32-bit integer, or up-to-256-character text defaults.
`GET /api/v1/players/{id}/client-settings` is read-only. The idempotent
`client_settings.patch` command atomically validates and persists a nonempty
set of neutral values inside the public player/receipt transaction. It cannot
write native DS fields, gameplay progress, wallet balances, entitlements,
other players or arbitrary profile blobs. The private gateway owns explicit
`profile`/`player_queue` aliases and translates original `/ds/{wire-user}`
requests without sending original identifiers or templates to this API.

Trusted maintenance code can call `GameService.backup(destination)` to copy
the entire public SQLite database (player state, battle snapshots, commands,
catalog digest) using SQLite's consistent online-backup mechanism. It refuses
an existing destination, verifies the copy, and cleans incomplete copies.
This is not an HTTP/native-client endpoint and does not import old saves.

## Legacy Challenge section attempt ownership

The optional `challenge_limits` catalog and each mission's
`challenge_section` assign UTC-day section-wide attempt caps to public
transactions. `GET /api/v1/players/{id}/challenge-limits` is read-only.
`mission.begin` gates attempts, `mission.complete` consumes only on victory,
and `mission.sweep` spends exactly the requested number; all checks, debits,
rewards and committed receipts share the same atomic public SQLite
transaction. Gateway-only original section IDs, native daily-limit shapes,
hero filters and source calendar metadata never cross this boundary.
No original backend is retired by this partial migration.

The optional mission `eligibility.event_access` also moves the legacy
event-section combat gate to the public transaction. Each selected event
node references its complete authored `section_mission_ids` and exactly
one policy: `permanent_minimum_level` or `windows` with integer
`starts_at`, `ends_at` and `required_level`. Empty windows explicitly
mean an unavailable historical event; the server never synthesizes
future occurrences. `mission.begin` checks the window, earned level
and whether all nodes in the section were cleared **before payment**.
`mission.sweep` checks the same section/window gate and blocks any
already-cleared one-time challenge. A battle legitimately issued during
an active window may settle afterward; its original authorization is
preserved in the battle receipt. The gateway only compiles source
`EventVisibility` policy into these neutral rules and displays events;
it cannot authorize an expired battle or manufacture an earned clear.

Sweep authorization also rechecks the authored Challenge local weekdays,
commander minimums, and mission-star prerequisites on every attempt.
An already earned three-star clear is the squad proof: the replay does
not require resubmitting a team or rechecking hero filters, matching the
original empty-squad sweep path. Rejected attempts leave balances,
progress, and the supplied idempotency key untouched.

This contract is defined by the application service, independently of any game
client's routes, encryption, field names or response templates. JSON request
objects have closed fields: unsupported fields are errors. The service never
accepts a client route, raw protocol envelope, response template, SQL statement,
or opaque player-state replacement as a command.

## Replayable Roguelite MVP

The additive optional `roguelite` catalog and `roguelite.*` commands are defined
in [the public contract](../API.md#optional-replayable-roguelite-mvp).
`GET /api/v1/players/{id}/roguelite` is a pure projection; the existing owned
battle endpoint returns frozen combat snapshots. Public state owns all run,
opponent, squad-use, choice and reward decisions. Select 1–5 owned characters;
five is optional. Optional `opening_enemy_team` selects 1–5 initial opponents;
later templates contain five. Only victories grant progression or shard choices;
defeat grants nothing and preserves previous winnings. Distinct recipient shards, immediate restart and atomic
retries are application behavior; private projection/native acceptance is separate.
Existing timed `tower.*` operations are unchanged.
Optional ordered `reward_bands` select complete victory bundles and shard
multipliers using the lowest issued enemy level. Encounter and battle views
freeze `victory_rewards`, `shard_multiplier` and `reward_enemy_level`; settlement
uses that snapshot, independent of later account growth. Gateway projects the
issued quantities into quantity-specific native cards and validates all reward
aliases, without selecting bands or calculating grants. See API.md for the
additive catalog and view fields. Flat catalogs and saved flat battles remain
supported; a changed catalog digest still requires explicit migration/new DB.
The optional authored shard pool supports up to 500 recipient characters
(including a full 388-character roster) and deterministic weighted
selection without replacement. `weight`, `era` and `affinities` are neutral
catalog metadata; actual player level and strongest owned heroes bias odds
without fixing upgrade/new/synergy slots. Choices remain saved and replay-safe.
The private reward-bias calculation includes real earned rank, tier, equipped
items and ability upgrades as well as base level-power, without mutating any
combat actor snapshot or adding source-client stat formulas to the server.

## HTTP interface

Default address: `http://127.0.0.1:18080`. POST requests require
`Content-Type: application/json` and a body of at most 1 MiB. When configured,
all requests require `Authorization: Bearer <service-token>`.

### Optional Dark Dimension persistent runs and source loot (candidate)

The optional neutral `dark_dimension` policy declares an earned
`required_level`, bounded per-character `health_caps`, two
normalized healing currencies with a finite premium conversion,
escalating `heal_costs`/`revive_costs`, and one or more bounded
campaign DAGs. Each campaign has a starting room, connecting
rooms, encounter IDs, source-health-capped neutral enemy IDs,
allowed team size, mandatory owned character IDs, recurring
and first-run reward bundles/tables, and completion reward
bundles/tables. The validator rejects unconnected rooms, nodes
belonging to multiple rooms, missing actors, undefined reward
tables, unsupported currencies and malformed prices.

`GET /api/v1/players/{id}/dark-dimension` returns a pure
list of run states. `GET .../dark-dimension/{campaign_id}`
returns a pure projection of one run, its currently accessible
rooms/nodes and whether a daily reset is due.
`dark.start` creates a run only after the earned commander-level
gate and refuses to replace an unfinished run; completed runs
can be restarted with an incremented run number.
`dark.refresh` with an exact owned `run_id` applies one
UTC-day recovery, restoring wounded heroes without
discarding saved enemy damage or rewarding a read.
`dark.begin` validates a reachable node, owned alive team,
source team-size constraints and no active battle;
it issues a durable battle identity with frozen enemy HP,
hero health/energy, policy and run context.
`dark.complete` accepts only a trusted, source-adjudicated
outcome that matches the issued run/node, complete actor
identities, bounded HP and three ability-energy values.
It saves persistent damage and hero state. First
node victory grants recurring node loot and first-run
source loot only on the first run; a final first run
also settles source completion loot. All loot selections,
run advancement and status transitions are transactional,
with immutable exact-key replay and no double settlement.
`dark.heal`/revive checks current actual wound/death state,
per-hero escalation, optional expected-cost assertions and
strict resource/premium fallback before atomically
restoring HP/energy.

The private original-source compiler has successfully
translated all **6 Insanity maps (125 rooms, 87 encounters)
and 93 original weighted loot boxes** using neutral IDs.
One preserved `oneofeach` box has 178 ordered groups;
the generic source-loot validation limit is now 256 groups
with an explicit regression rejecting 257, preserving
complete original rewards without an unbounded catalog.

**Important release boundary:** The public ledger has no
native mission definitions, original hero IDs, PowerRules
templates, transcript verification or client protocol.
The private binder must build/verify the original battle
and certify its result before sending `dark.complete`.
The current neutral hero health policy and original enemy
maximum HP are **not** proof of complete native combat
parity. Original-client DD routes, real user profile/schema
conversion, full wave/session replay and emulator acceptance
remain open. Retain the accepted old DD runtime until then.

### Optional public Blitz ladder and battle ledger (native-frontend pending)

The optional `blitz` catalog specifies `required_level`,
`maximum_tier`, `matchup_expiry_seconds`, two distinct
neutral recharge currencies with `cost_per_character`,
and bounded neutral event definitions. Each event has an explicit
UTC `starts_at`/`ends_at` or recurring `period_seconds`/
`anchor` window and sourced `cooldown_seconds`,
`win_points`, `lose_points`, `win_rewards` and
`lose_rewards`. Weekly rotation reuses **one stable catalog**;
separate saved `event_id:start` occurrence keys isolate
old points and tiers, without a catalog-digest mismatch.

`GET /api/v1/players/{id}/blitz` is pure. `blitz.refresh`
validates owned selected heroes/current power and persists three
neutral candidate identities and target powers with an expiry and
frozen roster signature. `blitz.recharge` charges exactly the
currently cooling heroes from the declared charge resource,
or the authored premium fallback, with optional expected-cost
assertion. `blitz.begin` requires a live saved matchup,
owned distinct team and no other active combat; it freezes the
selected enemy target, owned team and event policy, persists a
server-issued battle identity, and applies source cooldowns.
`blitz.complete` requires an active owned battle and a *trusted,
privately adjudicated* victory/defeat, consistent stars and bounded
duration, then atomically applies sourced rewards, earned tier,
streak and points, and the immutable battle receipt. Exact retries
cannot duplicate payment or a victory.

The public `target_power` is a **neutral source-policy target**,
not a fabricated original-client legal hero build. The private
gateway must still validate original native CombatMode 6
sessions/results before exposing any native combat route.
Original opponent hero stats, native request projection,
existing saves and original-client QA remain release blockers.

### Optional normalized seasonal boost economy (partial migration)

The optional `seasonal_boost` catalog declares bounded `starts_at`/
`ends_at`, `enabled`, `maximum_accrual_minutes`,
`claim_throttle_seconds`, `xp_currency_id`, ascending
`tier_thresholds`, finite nonnegative `score_multipliers`,
`character_tags`, 1–256 increasing `rates` (each with neutral
`minimum_score` and `wallet_per_minute`), and `boosts` (neutral
traits/stats with bounded `tier_requirements` and actual currency
`costs` by level). Only independently declared neutral character
and wallet IDs occur in this catalog. No original config file,
chapter name, hero native trait, captured account or source asset is
available to the server.

`GET /api/v1/players/{player_id}/seasonal-boost` returns a **pure**
view of availability, score components, rate, persisted state,
completed pending accrual minutes and pending wallet awards.
`seasonal.refresh` initializes an owned chapter state with no award.
`seasonal.claim` grants only whole completed minutes of current
source-policy per-minute awards, capped by maximum accrual duration,
chapter end, active window and claim throttle. `seasonal.buy`
checks the authoritatively saved expected level, XP tier and exact
next-step currency costs atomically, then stores monotonically
numbered boosts with purchase timestamps. Native request parameters
may never specify a cost, grant, modified price, balance, trait bonus
or claimed timestamp. The public command ledger controls exact-key
replay and rollback and emits ordinary wallet receipt differences.

**Important frontier:** the neutral score currently uses public
character power and declared normalized age/chapter tags. It is
*not* the source-native `PowerRules.calculate` including all
equipped gear, trait projections and combat session-stat modifiers.
The private compiler can match an actual preserved chapter policy,
but native boosts are not yet wired to the public combat model,
historical original save state cannot yet be imported, and
the original SPB client routes have not been enabled. Those are
separate release/retirement gates, not an assertion of complete
seasonal feature parity.

| Method | Path | Input | Result |
|---|---|---|---|
| GET | `/health` | none | `status`, `api_version` |
| GET | `/api/v1/catalog` | none | `catalog`, its SHA-256 `digest` |
| POST | `/api/v1/players` | `player_id`, `display_name` | `player` |
| GET | `/api/v1/players/{player_id}` | none | `player` |
| GET | `/api/v1/players/{player_id}/free-crates` | none | Server-time-adjusted free-crate schedule, without changing saved state |
| GET | `/api/v1/players/{player_id}/squads` | none | `squads`: configured group IDs to saved teams, current capacity, team size and level availability; read-only |
| GET | `/api/v1/players/{player_id}/battles/{battle_id}` | none | owned `battle` |
| GET | `/api/v1/players/{player_id}/towers/{tower_id}` | none | `tower_id`, `server_time`, `available`, persisted `tower` progress with expired cooldowns omitted from the read projection |
| POST | `/api/v1/players/{player_id}/receipt-lookup` | `idempotency_key`, optional `request_hash` | Read-only `status`: `match` with saved `result`, `conflict`, or `missing` |
| POST | `/api/v1/players/{player_id}/commands` | command envelope below | domain result and updated `player` |

Creating an existing player returns current persisted state; it never reseeds
balances or overwrites a display name. IDs contain 1–64 ASCII letters, digits,
dots, underscores or hyphens. Display names contain 1–64 characters.

Errors use HTTP 400/401/404/409/413/415 as appropriate, with
`{"error":{"code":"...","message":"..."}}`. Errors do not commit partial
balances, progression, battle completion or idempotency receipts.

Receipt lookup follows the existing server authentication policy: loopback may
omit a service token; when configured, valid bearer credentials are required.
Non-loopback binding requires a service token. It reads only the named player's existing
command ledger. `request_hash`, when supplied, is 64 lowercase SHA-256 hex
characters over the complete ordinary command envelope, serialized as sorted-key,
compact, UTF-8 JSON with literal Unicode (`ensure_ascii=False`). A matching hash
returns the original saved result; an existing key with a different or omitted
hash returns `conflict` without disclosing its result. An absent key returns
`missing`. Unknown players fail with 404. No command, refill, catalog validation,
RNG, state update or receipt insertion runs. A missing result is only an
observation; command submission still performs atomic idempotency checking.
For fingerprint-bearing commands the hash includes that fingerprint. This also
works for existing receipts: no new persisted fields or database migration.
Callers must reconstruct the original neutral command identity; a changed
mapping/revision cannot be inferred from a request hash alone.

## Command envelope

```json
{
  "command": "character.train",
  "idempotency_key": "train-001",
  "parameters": {"character_id": "scout", "levels": 1}
}
```

The idempotency key belongs to one player and one normalized command. An exact
retry returns the exact stored result, even after a restart or concurrent
submission. Reusing the key with different command parameters returns
`idempotency_conflict`. A successful result includes `player`, `wallet_changes`
and `inventory_changes`; changes report actual previous and new quantities.

| Command | Required parameters | Rules |
|---|---|---|
| `character.train` | `character_id`, `levels` | Owned character, 1–100 requested levels, catalog level cap and training cost |
| `squad.save` | `groups` | Atomically replace 1–100 configured squad groups; enforce server-owned level, earned capacity, team size and distinct owned characters |
| `free_crate.claim` | none | Claim the next free crate from the server-owned timed rotation; no client-selected prize or time, atomic idempotent award |
| `shop.purchase` | `offer_id`, `quantity` | 1–100 purchases, authored cost/rewards and per-player purchase limit |
| `mission.begin` | `mission_id`, `team` | Unlocked mission, 1–5 distinct owned characters, authored cost; issues a battle ID |
| `mission.complete` | `battle_id`, `mission_id`, `outcome`, `stars` | Owned issued battle, matching mission, one settlement; victory requires 1–3 stars; defeat/retreat require 0 |
| `mission.sweep` | `mission_id`, `quantity` | Prior three-star clear, 1–100 repetitions, authored aggregate cost/rewards |
| `tower.start` | `tower_id`, `difficulty_id` | Start/resume a run in the availability window; all difficulties are available at account level 1 |
| `tower.end` | `tower_id`, `run_id` | End a run after settling its issued battle; preserve cooldowns, best score, milestone claims and floor reward history |
| `tower.begin` | `tower_id`, `run_id`, `floor_index`, `encounter_id`, `team` | Current zero-based floor and matching encounter, time gate, required owned/eligible team and cooldown checks; charge catalog cost and issue a battle |
| `tower.complete` | `battle_id`, `outcome`, `stars` | Owned issued Tower battle and validated adapter outcome; victory advances once, scores, grants first-clear floor prizes and consumes winning characters' cooldowns |
| `tower.claim` | `tower_id`, `milestone_id` | Best run meets the threshold; milestone grants once for this Tower identity |

The optional mission `eligibility.local_weekdays` (also supported inside
`eligibility.scopes`) transfers the original Challenge `dayOfWeekLocal`
restriction into the public mission-begin authorization, before the
mission's energy payment. It is a nonempty set of distinct integers
Sunday=0 through Saturday=6. For compatibility with the legacy
`CampaignRegistry.eligible_challenge`, the server derives the current
process-local weekday from the command's authoritative timestamp; no
client-provided clock or second gateway-side eligibility decision is
accepted. Deployments must retain the intended server-local timezone.

The optional `eligibility.one_time` boolean migrates the original event
Challenge rule that rejects re-entry after a prior earned clear. It is
validated in the public catalog and enforced against server-owned mission
stars before payment or battle issuance. The gateway merely compiles the
original event-Challenge designation; exact command receipts still replay.

The optional `eligibility.first_entry_minimum_mission_stars` contains
neutral mission IDs and required earned stars (1–3). Unlike an explicit
`minimum_mission_stars` requirement, it is checked only until the target
mission has its own saved progress. This migrates the original campaign's
implicit previous-node/previous-chapter entry gate while retaining replay
rights for already-cleared missions.

Mission results return a `battle` and the actual credited `rewards`. First-clear
rewards are included only on the first victory. Purchase/sweep reward quantities
are totals for the requested count, not per-unit previews. A completed battle
cannot pay again under a different idempotency key. Client-supplied prices or
rewards are not accepted. The application consumes the trusted adapter's
validated battle outcome; it does not claim to simulate client combat physics.

## Player and battle representations

A player has `player_id`, `display_name`, `version`, `level`, `experience`,
`wallet` (currency ID to integer balance), `inventory` (item ID to integer
quantity), `characters` (character ID to level and derived power), `missions` (mission ID to
best stars and win count), and `offer_purchases` (offer ID to count).

A battle has `battle_id`, `mission_id`, `team`, `status`, and after settlement
`outcome` and `stars`. It contains no client battle transcript or native session
template. Each successful command increments the player's version once.
Balances and quantities are nonnegative integers, at most 9,000,000,000,000,000;
booleans, floating-point values, overspending and overflows are rejected.

## Catalog schema

`schema_version` is 1. Required collections are `starting_wallet`,
`starting_characters`, `characters`, `missions` and `offers`. Required progression
settings are positive `experience_per_level` and `maximum_player_level`.

- A character defines `maximum_level`, `training_cost` (currency quantities),
  and `power_per_level`.
- A mission defines `required_level`, `cost`, and `rewards`, with optional
  `prerequisite` and `first_clear_rewards`. Prerequisites must exist and be acyclic.
- An offer defines `cost`, `rewards`, and `purchase_limit`.
- A reward bundle has optional `wallet`, `inventory`, and `experience` fields.

`game_server/catalog.py` contains the full independently authored demo catalog.
A database is pinned to its catalog digest. Changing definitions requires an
explicit migration or a new database; restart cannot silently change an
existing economy. Databases belonging to another application are rejected
before any schema changes.

## Enhancements and promotion items

`GET /api/v1/players/{player_id}/augmentations` returns read-only
`enhancements_available` and `promotions_available` flags. With either catalog
extension configured, successful commands also include an atomic `augmentations`
view and `server_time`; exact retries retain the original saved view/time.

The optional `enhancements` catalog defines `required_level`, optional earned
`required_item`, `swap_seconds`, distinct `slots` (1–8), `characters` (character ID
to role ID), `items`, ordered `tiers` and `classes`. Level alone does not grant
access when an earned item is required. That item is not consumed by access.

Each item defines `role`, `slot_id`, positive `tier` and global `quality` (1–100).
An optional `fusion` defines a lower-quality `source` for the same role/slot,
positive `quantity` and wallet `cost`. A role/slot/quality identifies exactly one
item. Each tier defines `required_level`, `minimum_quality` and wallet `cost`;
the initial tier must be free. Each class defines ordered `skills`, each with
`skill_id`, `required_quality`, wallet `cost`, and optional earlier same-class
`prerequisite`. Skill IDs are globally distinct.

Every `enhancement.*` command requires an owned configured `character_id`:

| Command | Additional parameters |
| --- | --- |
| `enhancement.equip` | `item_id` |
| `enhancement.unequip` | `slot_id` |
| `enhancement.fuse` | `item_id`; optional exact declared `sources` list |
| `enhancement.select_class` | `class_id` |
| `enhancement.learn` | `skill_id` |
| `enhancement.refresh` | none |
| `enhancement.tier_up` | `target_tier` |
| `enhancement.bulk` | `class_id`, `target_quality` |

Equipping consumes one owned item and refunds the replaced one; selecting the
same item does not spend again. Fusion consumes its configured sources directly;
bulk upgrades can recursively fuse missing materials but cannot mint base items
without a recipe. A full matrix's quality is its weakest equipped slot; an
incomplete matrix has quality zero. Tier upgrades require the next tier (or the
already-unlocked tier without charging again), character level and full-matrix
quality. Class changes retain paid skills separately for each class and report
`swap_available_at`; this mirrors UI timing metadata without introducing an
additional cooldown gate. Learning an already-owned skill is free. All bulk
crystal changes, fusion, class selection and learning share one transaction.

Characters persist `enhancement` with `items` (slot to item), `tier`, `class_id`,
`learned` (class to skill IDs), `swap_available_at` and derived `quality`.

The optional `promotions` catalog has `required_level`, `maximum_base_rank`
and `characters`. Each character defines `items` mapping inventory IDs to `rank`
and `recycle_rewards`, and `advanced_items` mapping inventory IDs to positive
`rank`. Item IDs are unique across characters/categories; ranks are unique in
each category. `promotion.apply` takes an owned `character_id`, applies the
highest owned base entitlement capped to character rank, and never consumes it.
An advanced item requires full character base rank and an already-applied full
base promotion. Saved `promotion_rank` never decreases.

`promotion.recycle` takes a configured `character_id` (recruitment is not
required), retains one highest base item and converts all remaining base copies
using their catalog reward bundles. Advanced items are untouched. It returns the
actual credited `rewards`; application returns `character_id`/`character`, as do
enhancement commands. All use ordinary atomic player commands, balance changes
and persistent receipts. No original item-name encoding or private profile
journal is accepted as an application command.

## Character development

The optional `development` catalog has `characters`, `training_items` and
`recipes` objects. Every ID uses the normal independent application identifier
format. A configured character must exist in the base character catalog.

Each development character defines:

- `experience_thresholds`: strictly increasing total XP, starting at zero, with
  one value per level through the base character's `maximum_level`.
- `recruitment`: a payment object, `initial_rank`, and `rank_upgrades`: payments
  for each transition from rank 1 to 2, 2 to 3, and so on. Prices are incremental.
- `abilities`: ordered lists of upgrade steps; each step has `required_level`,
  `required_tier` and `payment`. `initial_abilities` has matching initial levels;
  zero means locked and null denotes an undefined ability with no steps.
- `tiers`: ordered maps of equipment slot IDs to `item_id`/`required_level`.
  Each tier has at most six slots; promotion consumes no additional items once
  every current-tier slot is equipped and clears equipment for the next tier.

A payment has `wallet` and `inventory` amount maps. A training item defines
positive `experience` and a wallet `cost` per unit consumed. A recipe defines
positive `ingredients` quantities and wallet `cost`, producing one unit of its
item ID. Crafting uses available ingredients before recursively crafting missing
ones. Cycles and dependency chains exceeding 64 recipes are rejected at startup.

| Command | Parameters |
| --- | --- |
| `character.recruit` | `character_id` |
| `character.rank_up` | `character_id` |
| `character.train_items` | `character_id`, `expected_level`, `items` (positive inventory quantities) |
| `character.ability_upgrade` | `character_id`, `ability_index`, boolean `unlock`; optional `allowed_levels` target-level guard |
| `equipment.craft` | `item_id`, `quantity` (1–1000); optional owned `character_id` |
| `equipment.equip` | `character_id`, `slots` (objects with `slot_id`/`item_id`), boolean `craft_missing` |
| `equipment.tier_up` | `character_id`, `target_tier` (exactly the next tier) |
| `equipment.bulk_tier_up` | `character_id`, `target_tier`, `plan` (objects with `tier`/`slot_id`/`item_id`) |

Training consumes the exact selected items/currency and cannot exceed account
level. `character.train` is disabled for development-enabled characters so that
its simpler per-level price cannot bypass material costs. Recruitment requires
an unowned character. Ability unlock requires saved level zero; ordinary upgrade
requires a positive saved level, and both enforce the configured gates/costs.
The optional allowed-level guard constrains the resulting ability level; it
never supplies a price or reward. Exact retries are resolved before live-state
validation using the normal persistent application receipt.

The bulk tier plan must exactly describe all missing slots in the current tier
and all required slots before the target. Every spend, recursive craft, equip
and promotion shares one transaction. A later failure rolls the entire plan
back without a receipt. Current-tier slot requirements govern promotion; future
slot level requirements apply when equipping those slots.

Starting/recruited development characters persist `experience`, `rank`, `tier`,
`abilities` and `equipment` alongside `level`/`power`. Power retains this API's
independent base level formula; no original-client stat curves are imported.
Commands return `character_id` and the resulting `character`, except independent
crafting, which returns `item_id`. Standard actual wallet/inventory changes and
updated player/receipt fields are included. No client price, native rule blob,
original character alias or native equipment encoding enters this schema.

## Appearance and guide progress

The optional `appearance` catalog has `default` (a choice ID) and `choices`.
Each choice is an object with optional `required_item`: a positive inventory
balance of that neutral item is required to select it and is never consumed.
The default choice must exist and have no required item.

The optional `guides` catalog maps independent guide IDs to an `events` object.
Each event has a boolean `completes` and an optional neutral `checkpoint` ID.
A successful event persists its checkpoint and/or completion flag. Failed events
and ordinary events do not advance saved progress. The application acknowledges
client UI progress; it does not execute scenes, require event order or grant any
rewards. Explicit guide acknowledgement can complete configured guides directly.

`GET /api/v1/players/{player_id}/preferences` returns `appearance_id` (or null
when appearance is unconfigured) and `guides`, mapping each configured guide ID
to a `completed` boolean and optional saved `checkpoint`. Reading never mutates
state. `appearance.select` takes `appearance_id`; `guide.acknowledge` takes
`guide_ids` (1–100 distinct configured IDs); `guide.event` takes `guide_id`,
`event_id` and boolean `success`. All use the ordinary atomic command/receipt
transaction and return the current preference view with the updated player.
Appearance selection also returns `appearance_changed`. Stored player fields
are `appearance_id` and `guides`; all IDs are independent application aliases.

## Saved teams

The optional `squad_groups` catalog maps independent group IDs to positive
`required_level`, `capacity` (1–1000) and `team_size` (1–5). An optional
`capacity_item` is a neutral inventory item ID: usable slots equal the lesser
of its earned inventory quantity and `capacity`. Saving does not consume it.
Without it, capacity is fixed.

`squad.save` takes `groups`, a mapping of group IDs to ordered lists of teams.
Each team is an ordered list of owned character IDs or `null` for empty formation
positions. Duplicate characters in one team are rejected; empty teams are allowed.
An empty group list clears that group. Other groups remain unchanged. Every
replacement must satisfy its definition before anything commits. The result
includes the current `squads` view and the standard updated player/receipt. Both reads and saves report the persisted
`updated_at` timestamp; reads never change it.
Persisted `player.squads` contains group IDs to team lists. No original category,
save selector, native slot-map encoding or captured profile enters this schema.

## Tower definitions and state

The optional `towers` catalog collection maps independent Tower IDs to
`starts_at`, `ends_at`, positive `cooldown_seconds`, `team_size` (1–5),
`difficulties` and `milestones`. No account-level restriction field is accepted.
Unix availability is half-open `[starts_at, ends_at)`.

Each difficulty ID maps to an ordered list of floors. A floor defines an
independent `encounter_id`, nondecreasing `unlock_offset` from the Tower start,
boolean `reset_cooldowns`, nonnegative `score`, `cost` and `rewards`. Optional
`eligible_characters` lists distinct catalog character IDs. Encounter IDs are
opaque references for a separate renderer: no client mission/map files, actor
templates, modifier blobs or combat transcripts enter this service.

Each milestone defines `milestone_id`, positive strictly increasing `points`
and a reward bundle. A best-run score, rather than cumulative replay score,
qualifies. Floor prizes pay once per difficulty/floor per Tower identity;
milestones pay once across all runs/difficulties. Replay cannot farm prizes.

Player state lazily adds `towers[tower_id]`: `active_run`, `last_run`,
`high_score`, `highest_floor`, `cooldowns` (character IDs to expiry timestamps),
`claimed_milestones` and `rewarded_floors`. A run has `run_id`, `difficulty_id`,
`next_floor`, `score`, `status`, `battle_id` and `started_at`. Difficulty changes
require ending the old run. Run changes preserve the Tower-wide cooldowns.

Victory cooldowns expire on period boundaries anchored to `starts_at`; a
checkpoint clears them. Loss/retreat do not advance, reward or consume hero
cooldowns. A battle spanning a boundary uses its settlement period. An issued
battle may settle after the event ends; new starts, begins and claims require
the active window. Reads do not mutate state or player version.

Tower battles have `mode: "tower"`, `tower_id`, `run_id`, `difficulty_id`,
`floor_index`, `encounter_id`, `team` and status/outcome metadata. Ordinary
`mission.complete` cannot settle them. Every Tower mutation uses the same atomic
player command/receipt transaction and returns updated state; Tower commands
also return the committed `server_time`. A matching `encounter_id` on begin
prevents a stale renderer from charging for another difficulty's encounter.

## Objectives and recurring passes

Optional `objectives` defines `counters` and `entries`. Counter sources are
transaction activity, current level/roster/power/mission/inventory state,
login streaks, dated reward signals, explicit constants, untracked statistics,
or all-clear dependencies on objective thresholds. Dependencies must be acyclic.
There is no command for assigning counters or importing claimed progress.

Activity rules select a supported application `command` and `measure`:
`count`, `quantity`, `equipped`, `wallet_spent`, `inventory_spent`,
`inventory_earned` or `login`. Spending uses gross successful costs, including
transactions which also award the same currency. Optional selectors are
`entity_ids`, `outcome`, `currency_id`/`item_id`, `multiplier`, `required_level`
and `maximum_level`. Failed commands and exact receipt retries never increment
counters. Login rules require the deduplicated `login` measure.

Optional counter `period` contains `seconds`, `anchor` (timestamp or
`player_created`), and optional `local_time`, `alternate_anchor`, `repeat`.
Creation accepts immutable `reset_offset_seconds` and `alternate_reset` options;
calling creation again never changes them. Without a period a counter is lifetime.
Objective entries define `counter_id`, `threshold`, `required_level`,
`maximum_level`, `rewards` and optional `claim_grace_seconds` for nonrepeating
windows. All-clear uses eligible thresholds, without requiring their claims.
Untracked counters cannot qualify for awards, even at threshold zero.

`GET /api/v1/players/{player_id}/objectives` returns `counters`, `objectives`,
`server_time` and stable `updated_at`; entries include availability, tracked
status, progress, period and claim status. Reads never change durable state.
`player.login {}` records at most one login per UTC day, independently of nonce.
`objective.claim {"objective_ids":["..."]}` atomically validates and claims
1-100 distinct earned entries. Claims are per entry/reset period.

Reward bundles may contain `signals`: a list of `{labels:["..."],quantity:N}`.
Each award is journaled with a server-generated ID and timestamp. Matching
multiple labels counts the award once. Signals are not spendable inventory.

Optional `passes` maps pass IDs to `counter_id`, `required_level`,
`maximum_level`, `premium_required_level`, optional `claim_grace_seconds`,
and `rewards`. Prize rows contain distinct `reward_id`, `threshold`,
`track` (`free` or `premium`) and `rewards`. Points must use an activity or
signal counter. Premium access is automatic at the configured level.
`GET /api/v1/players/{player_id}/passes` returns read-only current and historical
periods, points, access, selection time and prize status for each pass.

`pass.claim` takes `pass_id`, `period_key` and either `reward_ids` or `all:true`.
The service chooses unclaimed eligible prizes for `all:true`; explicit batches
are atomic. `pass.unlock` takes `pass_id`, `period_key` and acknowledges existing
automatic premium access without charging. New claims require the current
active claim window. Historical selection, premium flags and claims persist;
raising player level never upgrades an archived season. Exact committed receipt
retries return their original result after rollover or restart.
Configured successful transactions return committed `objective_state`/`pass_state`
alongside actual balance changes.


## Earned feature access

Optional `access_rules` maps independent access IDs to `required_level`,
`mission_ids` and `item_id`. Mission IDs must exist and be distinct; each grant
uses a distinct item. A player qualifies after reaching the required level and
winning every required mission. An empty mission list provides a level-only gate.
Creation and successful commands reconcile newly earned access in the same
transaction as the player state. Each rule grants one inventory item once and
records its server timestamp in `access_grants`. There is no client-selected
unlock command, no grant on failed commands and no replay duplication.

`GET /api/v1/players/{player_id}/access` returns read-only `access`: each rule's
`eligible`, persistent `unlocked` and `granted_at` state. Successful commands with
configured rules return this view and the list of newly earned `access_grants`,
alongside actual inventory changes. Reading never grants access or increments
player version. Native names and original lock parsing do not enter this API.
Individual application modes still own their command eligibility; an access grant
alone is not an implementation of a mode or its transactions.


## Player XP and earned level awards

Optional `experience_thresholds` is a strictly increasing list with one cumulative
XP threshold per player level, beginning at zero. Without it the existing linear
`experience_per_level` curve applies. XP can continue accumulating at the level cap;
`next_level_experience` is null at cap and further XP cannot create new level awards.

Optional `level_rewards` contains `policy_id` and `awards`, a map of canonical
level numbers (2 through the cap) to reward bundles. Awards cannot contain XP.
Crossing multiple levels pays every configured crossed level inside the triggering
command transaction. `player.level_reward_claims` records the policy and timestamp
once per level. There is no client-selected award/level mutation and no automatic
backfill from reads, reconnect, or repeated player creation. Any failure rolls
back the original command, XP, all awards, access grants and receipts together.

`GET /api/v1/players/{player_id}/progression` returns read-only `progression` with
level, experience, minimum_experience, next_level_experience, maximum_level and
awarded_levels. With configured progression, successful commands additionally
return committed progression, levels_gained, level_rewards, experience_change and
server_time. Award amounts are also included in actual wallet/inventory changes;
the command's ordinary rewards remain separate from level_rewards presentation.
Locally authored/inferred schedules are configuration, not recovered publisher data.


## Resource policies and estimated level-award configuration

Optional `resource_rules` maps currency IDs to `mode` (`finite` or `unlimited`),
optional soft `capacity` and nondecreasing `level_capacities`. Unlimited rules
require a positive `display_balance`; creation initializes that balance and all
spending/rewards preserve it. Finite currencies retain normal debit checks.
Soft capacity is authoritative metadata, not a clamp: rewards may exceed it.
These rules implement no regeneration timers. Actual wallet_changes always
report actual balances; unlimited nominal costs can still feed activity counters,
and nominal level rewards remain visible in award bundles without increasing the
fixed balance. Failed finite costs roll back the complete mixed transaction.

`GET /api/v1/players/{player_id}/resources` returns read-only `resources` keyed
by configured currency, with mode, actual balance and current capacity where
configured. Successful commands with resource policies also return this view.
Capacity changes at a crossed level do not require a wallet balance change.

`game_server.level_award_policy.estimated_policy` provides an explicitly local,
estimated schedule as a neutral level_rewards table, with caller-supplied
currency/item IDs. It preserves the prior experimental 4-110 payout schedule,
including its special level-21 bundle. It imports no original code/data and is
not enabled by the demo catalog. Selecting it requires explicit configuration;
it does not backfill previous levels or establish publisher parity.


## Login calendars

Optional `calendars` maps neutral board IDs to `mode`, `required_level`, distinct
processing `order`, and a nonempty sequence of `rewards`. Modes are `finite`
(exhaust once), `repeat` (cycle by attendance), `consecutive` (cycle with reset
after a missing UTC day), and `monthly` (reset by calendar month; at most one tile
per attended day, bounded by actual days in that month). Optional `ends_at` uses
the UTC day boundary, `required_item_id` requires a currently owned item, and
`requires_alliance` requires actual server membership. Membership-producing
application commands are a separate migration; the flag never grants membership.

`GET /api/v1/players/{player_id}/calendars` is read-only. It returns board step,
period, availability, next tile, daily claimability and consecutive-reset status;
shared metadata contains server_time, day, month, days_in_month, end_of_day and
end_of_month. Effective day never moves behind a previously awarded board day.

`calendar.advance {}` advances every eligible board in explicit catalog order,
atomically awarding at most one tile per board per UTC day. No client-selected
board, date, tile or prize enters this command. Calendar receipts retain each
board/period/day award; a different nonce on the same day earns nothing, while an
exact operation retry returns its original response even after restart. Responses
include calendar_awards, quantity, rewards and committed calendar_state. Any later
board failure rolls back every grant and receipt. Reads and player creation never
claim tiles or import historical attendance. The activity engine accepts only
actual award quantity/inventory earned for calendar advancement; polling cannot
farm an activity count. Backward clock movement never issues a new reward.

`calendar_policy.estimated_boards` and `split_inventory_awards` explicitly author
local repeating schedules and quantity-preserving splits using neutral IDs.
They are not publisher schedules and are not enabled by the demo catalog.


## Milestone views, period guards and UTC months

Optional `milestones` maps neutral group IDs to a period-bound `counter_id`, an
ordered nonempty `objective_ids` list and optional `display_grace_seconds`.
Objectives must share that activity/signal counter and have strictly increasing
thresholds. Rewards and claimed flags use the existing objective ledger; native
wrapper/feeder views do not introduce a second reward ledger.

`GET /api/v1/players/{player_id}/milestones` returns current group score, period,
objective progress/claims, visibility and display_until, plus known historical
periods. History derives only from committed activity, signals and objective
claims. Reads do not enroll, award, seed or change player versions. Display grace
does not extend claim eligibility: objective windows remain authoritative.
Configured successful transactions also return committed `milestone_state`.

`objective.claim` optionally accepts `period_keys`, an object mapping every
requested objective ID to its expected period key. Partial maps, null and wrong
periods are errors, with atomic rollback. This guards an old native event URL
against claiming a new period's award. Exact operation receipts are checked first,
so a committed retry still returns its original response after rollover/restart.

Counter periods additionally support `{"calendar":"month","anchor":0}` (or
another UTC month-boundary anchor). These periods use actual UTC calendar months,
including leap years, rather than a 30-day approximation. Local/alternate resets
are not accepted for this period form. Period histories, including public pass
histories, use the same resolver. Signal sources optionally accept a positive
`multiplier`; one multi-label award still counts once before multiplication.
No captured totals, original routes, aliases or reports enter this contract.


## Profile-scoped local alliances

Optional `alliances` defines access level, name/tag/message length limits, typed
visibility IDs and joinability, default visibility, maximum_members,
leave_on_create, rejoin_cooldown_seconds, rank IDs (including owner/member),
inactive-day settings and explicit alliance XP progression parameters.
An optional typed bootstrap team is placed once when access is earned; there
are no captured or simulated teammates. Each profile owns its saved local teams;
only its current team has one actual member. This preserves the local model,
not a shared multiplayer directory. Creation/membership never grants currency.

`GET /api/v1/players/{player_id}/alliances` returns read-only availability,
unlocked_at, current membership, known teams and donation cooldown metadata.
`GET .../alliances/{alliance_id}` reads an owned local team or fails.
`POST .../alliances/search` is read-only and accepts optional name, tag,
visibility_id, exact, exclude_full and limit (1-100). Filters are authoritative
application queries, not gateway searches over captured alliance records.

Commands: `alliance.create {name,tag,...}`, `alliance.update
{alliance_id,name,tag,...}`, `alliance.join {alliance_id,allow_switch}`,
`alliance.leave {alliance_id}`, and `alliance.acknowledge {}`. Create/update may
include message, visibility_id and settings (emblem, raid_rank, churn_days).
Creation defaults are server-owned; omitted update settings are preserved.
Unknown fields are rejected; no opaque native data blob enters the API.

These commands share player transactions and exact idempotency receipts. They
validate level, name uniqueness, visibility, identity and ownership; active raids
prevent switching/leaving their team. Actual raid integration is still pending.
Joining a different saved team sets the configured donation cooldown. Leaving
never triggers automatic re-placement. XP grants reconcile placement immediately,
so a later calendar tile can observe newly earned real membership in the same
transaction. Successful configured commands return committed alliance_state.
Donation and combat-boost transactions remain a separate migration frontier.


## Alliance donations and permanent boosts

Optional `alliance_economy` requires alliance policy and defines `donations` and
`boosts`. A donation tier is a list of ordinal steps: cost, rewards, maxed_rewards
and experience. The donation ordinal is profile-wide per UTC day across tiers and
memberships. `alliance.donate {tier_id}` requires access, current membership and
an expired join cooldown. It atomically spends the selected ordinal's cost,
selects maxed_rewards only when every configured boost is at its cap, awards the
player bundle, increments lifetime contribution and advances the actual team's
XP using the configured alliance curve. Day counts persist across team switches
and clock rollback; a different nonce cannot evade a previously exhausted day.

Boost categories map IDs to level steps with required_level, cost and finite,
nonnegative, nondecreasing percent. `alliance.upgrade_boost {category_id,boost_id}`
advances one level. `alliance.upgrade_category {category_id}` advances every boost
in a category by one level; every cap/gate and the aggregate payment must pass
before any debit or level change. Boosts belong to the player and persist across
membership changes. Purchase requires current membership; earned combat benefits
remain available after leaving. No client-selected percentage enters the API.

Alliance views/committed alliance_state include an economy view with actual UTC
donation count, lifetime contribution, cooldown, maxed status and boost levels/
percentages/next steps. `alliance_contribution` objective sources read earned
lifetime XP; activity rules can measure committed alliance_experience on donation
commands and actual wallet costs. Combat begin records an immutable alliance_boosts
snapshot on ordinary and Tower battles; later purchases cannot alter the issued
battle. Original trait/stat interpretation and native rounding remain private.
Exact operation retries preserve debit/grant receipts after resets and restart.


## Raid launch and participation

Optional `raids` is a map of neutral raid IDs. Each definition declares
`required_level`, `duration_seconds`, `join_cooldown_seconds`, `lock_seconds`,
`team_count`, `launch_cost`, `starting_room_ids`, `rooms`, `nodes`, `difficulties`,
`maximum_launch_difficulty` and `nonsequential_difficulties`. Rooms declare
`next_room_ids` and `node_ids`; each globally distinct node declares an
`encounter_id`. Every node belongs to one room, every reference resolves, and
all rooms must be reachable from a starting room. Empty connectors traverse
without counting as completed battle rooms. Cycles terminate during traversal.

Difficulty zero has a null prerequisite; later entries declare `difficulty`
and `completion_percent` of an earlier tier. Sequential launch requires actual
same-alliance ended settlement history; choosing a preference grants no progress.
Nonsequential definitions bypass that history gate, while preserving launch caps.

| Command | Parameters | Behavior |
| --- | --- | --- |
| `raid.launch` | `raid_id`, optional `difficulty` (default 0) | Charge launch cost once, generate instance identity/expiry, prevent duplicate active type in the current alliance. Check earned level, membership and launch rank. |
| `raid.join` | `raid_id`, `instance_id`, integer `team_index` | Check current-alliance ownership, expiry and joining cooldown; freeze the owned roster on first participation. Team selection cannot switch or replace that snapshot. |
| `raid.lock`, `raid.release` | `raid_id`, `instance_id`, `team_index`, `node_id` | Require the joined team and matching definition; check topology, completed nodes and foreign locks. Selection lock expiry is bounded by instance expiry. |
| `raid.set_difficulty` | `raid_id`, `difficulty` | Validate launch permission/cap/history, retain future preference on the alliance, return actual completion history. Existing instances and balances are unchanged. |

`GET /api/v1/players/{player_id}/raids` lists current-alliance instances.
The list additionally returns `pending_settlement_ids`: a sorted list of all
owned instances (including earlier alliances) whose configured end/clear rules
require closure. This is a pure, server-authoritative decision from the same
`raid_settlement.ready` rule as the atomic `raid.refresh` command. Native
gateways must not independently calculate expiry, completion thresholds or
mail eligibility from raw player state; they may request `raid.refresh` when
this list is nonempty.
`GET /api/v1/players/{player_id}/raids/{instance_id}` returns an owned instance.
Views include available/expired status, per-team members, room access, node
completion and unexpired locks. They never rewrite durable state. Membership
changes remain blocked while the alliance has an unexpired active raid.
Alliance views include `raid_progress` with earned difficulty/completion and
selected future difficulty. All command retries use the ordinary atomic ledger.

Native raid combat translation remains pending. Refill, healing, authored
daily scheduling and the configured raid-end/mail candidate have bounded HTTP coverage,
not original-client acceptance. The normalized combat commands below do not
establish a complete playable original-client raid mode.

### Ranked Arena state and original daily payouts (candidate)

Optional neutral catalog `ranked_arena` declares `required_level`,
`maximum_rank`, `attacks_per_day` and contiguous, non-overlapping
`payout_ranges` of `min_rank`, `max_rank`, `tier` and standard normalized
`rewards`. Every rank from 1 through `maximum_rank` must appear exactly once.
The public API refuses undefined wallet currencies. All original gacha-token
aliases and payout source tables are compiled **privately** and matched
against the public definition before the gateway can serve an account.

`GET /api/v1/players/{player_id}/ranked-arena` is a read-only state
projection. `arena.refresh` initializes a locally earned Arena rank and
daily attempt counter, generates durable neutral candidate identities,
and, on UTC day rollover, awards **at most one** daily payout at the
previous persisted rank, atomically with resetting attempts. It retains
40 recent payout receipts. Concurrent identical operation keys replay
the saved result; a failed transaction cannot award partial payouts.
`arena.set_defense` stores one to five distinct, actually owned
neutral character IDs, increments the opponent-selection revision and
rebuilds the candidates. Every mutation is part of the ordinary public
application transaction and immutable receipt ledger, including wallet/
inventory changes, milestones and other configured activity effects.

The local three-candidate difficulty curve uses the retained 4%, 10%
and 20% rank steps and local target-power ratios. Candidate `team`,
`target_power` and identities are **neutral application targets**, not
original native `HeroRules` enemy builds; source-accurate opponent stats
and combat sessions have not yet been reconstructed.
`arena.begin` requires the opponent ID from the currently saved
three-candidate board, a distinct owned 1–5 character team, remaining
earned daily attacks and no other active battle. It freezes the issued
opponent, rank, chosen roster and a new server-issued battle ID
atomically with decrementing one attack. `arena.complete` settles
only that active owned battle after **trusted private adjudication**,
with a consistent victory/defeat, stars and bounded duration.
Victory improves rank only to the issued opponent's rank;
defeat does not change rank, and neither directly grants prizes.
The immutable receipt prevents repeated completion or
an additional attack. If a battle ends after UTC midnight,
the previous rank is paid and daily attacks reset **before**
the current battle result moves rank.

The original Battlegrounds routes remain unavailable in the new gateway
until source-native combat/session/transcript and receipt compatibility
pass verification. Native `is_autosim` is unsupported.
This is a partial ownership migration, not playable Arena/native acceptance
or authority to retire the old backend.

### Daily raid automatic launches

Optional public catalog `raid_schedule` contains `enabled`, `hour_utc` (0–23),
`minutes` (0–59), and one to 32 distinct neutral `entries` keyed by stable
internal slot IDs, each containing `raid_id` and `difficulty`. The gateway
selects which original source raids are supported and compiles their original
UTC default clock separately; the public catalog contains **no** native raid
IDs, original resource names, or captured schedule/profile fields.

Alliance creation/bootstrap seeds a durable schedule with
`last_modified_at` equal to actual creation time and `last_attempt_at` zero.
The server computes the latest UTC slot not after its current clock using
the original day/offset formula. `GET /api/v1/players/{player_id}/raid-schedule`
is read-only and returns `due`, `slot`, `schedule` and `server_time`. Only an
enabled, earned schedule with a slot after the last successful attempt and not
before its last edit is due; disabled and unearned schedules never auto-launch
on ordinary reads.

`raid.auto_launch` accepts an optional boolean `skip_if_unfunded` (default false)
and atomically tries every configured
entry against the same public player/alliance transaction. It reuses the
existing `raid.launch` eligibility, active-instance guard, difficulty history
and actual launch prices. An already-active raid is skipped, but any other
failed launch (including an unaffordable *later* entry) rolls back every prior
launch/payment and leaves the slot due. On success the server saves the slot
cursor and returns `launched_raids`; an already-consumed or disabled slot
returns an empty list. Ordinary idempotency receipts make exact retries safe
after newer spending or server restart. `raid.set_difficulty` updates the
matching alliance schedule entries for **future** launches only, just as the
retained preference path did. Native requests never set prices, clocks,
schedule entries or replacement state.

The original offline native read/probe uses `skip_if_unfunded=true`.
For that one expected failure the **server** restores the entire pending
multi-raid transaction and returns `skipped="insufficient_funds"` with
no player write, command receipt or daily-slot advance; retrying the same
slot/key after earning more currency is allowed. Other errors still fail
atomically. The gateway only converts this server-owned outcome to a
native no-op; it no longer interprets affordability exceptions.

This models the retained **local offline daily policy**, not a recovered
publisher schedule. Original event-feed projection, complete native UI
compatibility, historical save import and other native raid features remain
separate acceptance requirements.


## Normalized raid combat transactions

A raid definition may add `combat`, with `energy_caps` keyed by neutral character
ID and `nodes` covering every raid node and every configured difficulty. Each
character has three ability energy caps: an integer means a constant cap at all
ability levels; a list gives caps by owned ability level. Each node/difficulty
encounter declares `encounter_id`, `cost`, `maximum_team_size`, `gates`,
`required_character_ids`, `enemies` (neutral actor IDs to maximum HP), `rewards`,
`contribution_points`, and `time_limit_seconds` (null means no timer). Optional
`initial_enemy_health` supplies authored starting HP without changing maxima.
A gate declares `minimum_team_size` and per-character alternative minimum clauses
for `level`, `rank`, `tier`, `promotion_rank` or `enhancement_level`. Every selected
character must satisfy each gate using the immutable roster saved at raid join.

All four commands require `raid_id`, `instance_id`, integer `team_index` and
`node_id`, using current-alliance ownership and an unexpired joined instance:

| Command | Additional parameters | Behavior |
| --- | --- | --- |
| `raid.begin` | `team`; optional `difficulty`, `formation`, `checkpoint` | Validate node access, owned lock, frozen roster, restrictions, deaths, energy caps and payment before creating an attempt. Freeze selected roster, boosts, health/energy, enemy state and encounter rules. A valid replacement abandons an older raid attempt without refund or health commits; another mode cannot be abandoned. |
| `raid.complete` | `battle_id`, `outcome`, `duration_seconds`, `allies`, `enemy_health` | Validate context, actor identities, numeric health/energy bounds, outcome and timer. Persist injuries, deaths, energy and enemy HP on losses too. Actual positive HP damage accumulates per-node/team contribution. Only victory clears the node and grants configured rewards/contribution points. |
| `raid.checkpoint` | `battle_id`, `checkpoint` | Authenticate the active issued attempt and exact begin checkpoint; grant no resources or progress. |
| `raid.reset` | none | Reset the selected node's entire accessible incomplete room after a settled attempt. Reject active combat or foreign locks. Remove only that room's enemy/difficulty progress and contribution; retain roster injury, energy and past costs. |

A checkpoint has `enemy_health` (null or a complete known actor/HP map) and
`progress` (a finite fraction from zero to one). It is protocol keepalive metadata,
not a mutation of saved enemy progress. Allied result rows contain `health`,
`maximum_health` and three `energy` values. Enemy result HP must cover the exact
issued encounter identities and remain within catalog maxima. Actor maximum HP
is an attestation by the private combat renderer/adjudicator: it must derive from
the frozen issued roster and original stat interpretation, never client-written
Stat fields. The application validates normalized bounds and owns all resulting
state; it does not interpret original transcripts or simulate the native combat.

The native raid begin/complete/checkpoint/reset translator and reward-box compiler
are not yet connected to this contract. Real HTTP tests exercise this normalized
API and read its committed state through native list/roster/room projections.
Those tests are not original-client combat acceptance. Fixed configured rewards
in independent tests do not substitute for original weighted reward boxes.


## Weighted reward tables

Optional `loot_tables` maps neutral table IDs to ordered `groups`. Each group
contains weighted choices with `weight`, `multiplier` and `items`. Each item has
`weight`, inclusive `minimum`/`maximum`, `multiplier` and unit `rewards`; quantity
is the sampled integer times both multipliers. A null reward marks an empty
outcome and requires zero quantity fields; it consumes no quantity RNG draw.
An empty group list is an intentionally empty table. Weights are nonnegative
integers with positive totals for every nonempty selection list. Definitions
validate all outcomes and maximum products before the service opens its database.

Items may also declare neutral unit `counters`. These are contextual outcomes,
not automatically granted to player inventory or the signal journal. The owning
application transaction must declare how to consume them. Raid encounter rules
may declare `reward_tables` (ordered references, including intentional repeats)
and `contribution_counter_id`. Only that counter may appear in their reachable
loot outcomes; an undeclared counter rejects the catalog. Contributions remain
on the raid, alongside its directly configured contribution points.

On victory, raid completion draws each referenced table using the issued battle
identity and neutral table ID. It adds the resulting ordinary bundle and scoped
points to the same atomic transaction, persisting `loot_rolls` (selection indices,
quantities, bundle and contextual counters) with the completion receipt. Losses
do not draw. Exact retries return the saved result; tables remain pinned by the
catalog digest. There is no client-selected seed or unrestricted roll command.
Paid orb opening and original-client reward projection still require their own
translated command paths. This preserves the retained local roll interpretation;
it does not claim a recovered publisher RNG or current live prize parity.


## Paid loot openings

Optional `loot_products` maps neutral product IDs to `revision`, `table_id`,
`payment_item_id`, `unit_cost`, per-opening `experience`, `required_level`,
`batch_required_level` and `maximum_quantity`. Referenced tables must exist and
cannot contain contextual counters without an application owner. Quantity/cost
products are bounded and validated before opening the profile database.

`loot.open` requires `product_id`, `revision` and `quantity` and accepts optional
`request_fingerprint` (64 lowercase SHA-256 hex characters). The digest binds
caller request identity through ordinary replay/conflict detection, without
decoding private data or accepting caller-selected prices or prizes. Existing
callers may omit it. The original `server_time` is saved in the receipt. It validates
revision, single/batch level gates and available inventory tokens, generates a
server seed, and rolls each opening with `seed:index`. The gross token debit,
all actual prizes, opening XP, seed and individual `loot_rolls` are saved in one
command receipt. The result includes `payment` with gross quantity, previous
balance and final balance, including any prize of the payment token itself.
Gross inventory spending and earned inventory feed existing activity counters.
A result with no ordinary prizes fails before awarding opening XP; failure
leaves balances, progress and receipts unchanged. Exact retries and restart
return the saved opening instead of rerolling. Callers cannot submit a seed,
price, prize or replacement player state.

The internal seed/roll receipt is projected into original reward rows by the
private gateway. Source context, native catalog/box versions, shop types and
rarity presentation remain private. This does not activate a personal release
or establish original-client UI acceptance.


## Raid closure and deferred mail

Optional neutral raid `settlement` contains `missions_per_team`, ordered
percentage `thresholds`, matching `tier_table_ids`, an optional
`consolation_table_id`, `difficulty_bonuses` for every level, and
`mail_duration_seconds`. Difficulty rows supply `tier_rewards`,
`completion_bonuses`, `first_completion_bonuses`, `first_table_id` and
`first_table_minimum_percent`; optional `tier_contribution_points` and bonus
`contribution_points` remain raid-scoped. Referenced loot outcomes may contain
only the declared `contribution_counter_id`. A first-table percentage above
100, including the original 200% disabled sentinel, is intentionally
unreachable and must never be clamped to 100.

The public ledger counts actual finished nodes in all strike teams against
`missions_per_team * team_count`, without inventing other players. An active
raid closes on expiry or exact full clear; `raid.complete` closes atomically on
the final winning node. `raid.settle` accepts `instance_id` for a single
eligible closure, while `raid.refresh` accepts empty parameters and closes all
eligible instances in one transaction. Re-settling an ended instance returns
its recorded payout. Source rank-one tiers/consolation, per-difficulty awards,
same-alliance and same-difficulty first-completion history, deterministic
weighted rolls and contribution counters are saved once. Deliverability is
checked before closure, but **rewards are not granted until mail claim**.
Unjoined instances have no gift; an authored empty payout remains empty.

`GET /api/v1/players/{player_id}/mail` is a pure view of unexpired visible
messages and server time. Optional catalog `mail_messages` declares archived
read/delete-only message identities and creation/expiry times; it contains no
captured gift records. Raid-result mail retains its settlement identity, saved
prize bundle, points, read/claimed/deleted state and configured expiry
(normally 30 days). `mail.read`, `mail.delete` and `mail.read_claim` accept
one to eight distinct `message_ids`; claim commands require matching ordered
`gift_ids`, and `mail.claim` can instead resolve one `gift_id`. An unclaimed
gift cannot be deleted. Claims atomically apply the saved prize, once, and
credit saved raid contribution; any failed element rolls back the batch.

Native-form callers may provide SHA-256 `request_fingerprint` and opaque
`projection_fingerprint` on mail commands. Read-only receipt lookup accepts
the matching command and request fingerprint for `mail.read`, `mail.claim`,
`mail.read_claim` and `mail.delete`. It retrieves the exact committed result
before current mail or clock reads; the ordinary nonce/replay engine still
guards idempotency. The server never receives original mail templates, native
gift aliases or private protocol frames.

These are bounded application/HTTP comparisons, **not** native-client
acceptance, historical native resource-version recovery or save import.
Original raid combat/scheduling, other modes and active legacy callers still
block deletion of the retained backend.

## Raid energy recharges

Optional `raid_energy` defines `required_level`, `currency_id`,
`payment_currency_id`, nonempty positive `prices` and `quantities` arrays, and
`conversion` (null, or a distinct finite `currency_id` and positive numeric `rate`).
It requires alliance policy and a finite target resource with positive capacity
in `resource_rules`. Target, primary and conversion currencies are distinct.

`raid.refill` requires `expected_cost` and accepts optional SHA-256
`request_fingerprint`. The server checks earned level/current membership, rejects
energy already at its soft capacity, selects the current UTC-day price and checks
the displayed expected price. If primary funds are insufficient it converts the
whole price with `ceil(price * rate)`, retaining partial primary funds. It grants
the ordinal quantity (repeating the last quantity when that array is shorter),
without truncating an accepted refill at the soft capacity. All spending, grants,
activity and daily history share the existing atomic command receipt. Exact
retries retain old balances and time, including after reset/restart.

`GET /api/v1/players/{player_id}/raid-energy` is read-only and returns
`server_time`, current-day `count`, `remaining` and level/membership `available`.
Old daily counters remain saved. It follows ordinary service authentication.
This does not import existing profiles or establish native acceptance. A client supplies no quantities, conversion rates or replacement state.


## Raid healing and revival

A raid may configure `healing`: `limit` (-1 for unlimited), and separate `heal`
and `revive` rules. Each rule has `currency_id`, positive nonempty `prices`, and
`conversion` (null, or a distinct finite `currency_id` and positive `rate`).
Prices escalate independently per character and action, repeating the final
price. The limit counts every healed/revived character, including batches.

`raid.heal` requires `raid_id`, `instance_id`, `node_id`, `characters` (one to five
rows), SHA-256 `request_fingerprint` and opaque SHA-256 `projection_fingerprint`. Each row has `character_id`, boolean
`revive`, trusted `maximum_health`, and optional `expected_cost`. The stat
attestation must derive from the frozen joined roster's unboosted private stat
interpretation, never client HP/Stat fields or current upgraded characters.
This has the same trusted adjudicator boundary as normalized combat results.

The server checks current matching active raid/team, known node, no active
battle in any mode, remaining recovery limit, distinct owned joined characters,
actual death/injury state and expected prices. It spends primary funds or the
whole converted price (`ceil(price * rate)`), retaining partial primary funds.
The batch updates health, preserves ability energy, advances counters and records
all payments in the ordinary atomic transaction. Any later invalid row rolls back
earlier rows and leaves the nonce reusable. The receipt contains ordered
`payments`, changed `characters`, complete projected `raid` and original time.

The read-only receipt lookup additionally accepts `command: "raid.heal"` with
`request_fingerprint`, as an alternative to `request_hash`. Both fields must be
present; they cannot be combined with `request_hash`. It returns a result only
when the saved receipt was stamped by `raid.heal` with the exact fingerprint.
Missing/conflict behavior and authentication are unchanged. Ordinary command
hash replay remains exact; this lookup permits the gateway to recover the saved
receipt before reading a now-expired raid or regenerating stat attestations.
The server stores no native body or response. Foreign users cannot fetch receipts.

This implements configured raid recovery; other native modes sharing the heal
route, save import and full native acceptance remain separate requirements.

Recovery receipts also retain the opaque projection fingerprint. The gateway
checks its current mapping/template rendering revision before projecting a saved
receipt. A mismatch fails explicitly without another debit; recovering historical
projection definitions remains required for cross-revision native compatibility.
Unknown historical ability energy defaults to `[0,-1,-1]` inside the saved recovery
transaction, matching the retained original response and allowing receipt replay.
The internal API accepts trusted adjudication only: configured service tokens
protect it, non-loopback binding requires a token, and optional tokenless access
is local loopback. Do not expose trusted stat-attestation commands directly to
untrusted native clients. Original-save death flags require explicit normalization
to zero public health during the still-unimplemented save transition.
A key-only receipt probe returns `missing` or `conflict`; it never returns a result.

## Trusted complete-player cutover (offline only)

`game_server.profile_cutover.import_complete_player(service, snapshot, provenance)`
transfers a complete **neutral application state** into a previously unused
player ID. It is an explicit trusted Python maintenance entry point, **not**
a command, native route or HTTP API. Original-source private fields and
untranslated progression are rejected. The provenance requires a SHA-256
fingerprint of the source, an exhaustive inventory of original profile
sections, an explicit source-to-neutral field map and every historically used
**neutral idempotency key**. No unaccounted sections or missing core state are
accepted. Old operation keys become durable conflict tombstones, ensuring
an original purchase/claim cannot pay again after reconnecting. Reimporting
the exact same source is idempotent even after subsequent public gameplay.
A different source or an already-used public account conflicts. The import
does not invent or grant rewards, open battles, bypass the catalog digest,
load captures, or modify any original player database.
The cutover section map must cover **exactly** the neutral snapshot fields
(plus the separately tracked `legacy_receipts` field). Omitting existing
neutral progress or claiming an absent field fails before any write. This
checks the consistency of the translated manifest; it does not prove that
the private translator accounted for every field in the original source.
For historical saved teams, `squads` and `squads_updated_at` must be supplied
together. The import validates every group, formation, owned character, level
gate and purchased capacity against the same public rules as `squad.save`,
without executing a new command or overwriting the original timestamp.
Unsupported original squad modes or characters must be rejected privately;
no missing team may be silently replaced by an empty public group.

Imported earned mission clears must include both a positive `wins` count and
`best_stars` in 1–3, with no omitted or extra fields. A star-only imported
clear (or positive stars with zero wins) would wrongly award first-clear prizes
on the next victory, so it is rejected before any write. The original private
mission translator preserves the exact earned star rating and supplies only a
conservative confirmed-win floor because the old profile did not record exact
lifetime win counts. Imported three-star sweeps and downstream mission unlocks
therefore remain available without duplicating first-clear rewards.

Historic shop stock is imported without replaying purchases or crediting items.
`offer_purchases` accepts only earned positive counts within the authored
lifetime offer limit; `offer_daily_purchases` accepts only correctly typed
`{day, count}` receipts for authored `utc_day_once` offers, no future UTC day
and no more than the permitted one-batch count. A migrated paid batch therefore
remains unavailable on its original UTC date and resets under ordinary server
policy the next day. The private source translator must prove which original
stock slots and historical operation receipts earned each entry; this receiver
does not infer purchases from wallet contents.

This is the receiving boundary only: the private legacy-profile translator
must still prove **complete source-field and receipt coverage**, normalize
each supported progression domain and block cutover if any native data is
unrepresentable. Do **not** use a partial snapshot or claim the existing
personal saved profile is migrated based solely on these isolated tests.

For enhancement-enabled characters carrying imported ISO-8 equipment,
`import_complete_player` also validates complete neutral `enhancement` state:
each equipped slot/item belongs to its configured character role and earned
tier; reported matrix quality equals the actual equipped matrix; class IDs,
ordered purchased skills (including previous-class history), and swap
availability are mapped and bounded. Invalid historical equipment or skill
history rejects the entire transaction before creating a player or receipt.
The private original identifier/history compiler lives in
`private_gateway/content/iso_cutover.py`; it is invoked through the private
augmentation projection and must be used by a complete offline profile
translator. The public service never imports original ISO item IDs, source
rules, class strings or unverified reward transactions.

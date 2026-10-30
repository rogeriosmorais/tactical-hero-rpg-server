# Optional legacy Challenge attempt sections

The optional neutral `challenge_limits` catalog maps section IDs to a positive
daily attempt cap (1–100). Mission definitions can declare
`challenge_section` referencing one configured section. Several missions may
share the same section. This migrates the application-owned limit and UTC
rollover behavior formerly held in the legacy backend's
`challenge_remaining`, `require_challenge_attempt` and
`use_challenge_attempt` methods; original section names, mission contexts and
native response formatting remain private.

`GET /api/v1/players/{id}/challenge-limits` is a **pure read** returning
`day_start`, `next_reset` and each section's `daily_limit`, `used` and
`remaining`. A challenge `mission.begin` checks availability but does not
consume an attempt. A successful `mission.complete` consumes one attempt;
defeat/retreat consumes none. A `mission.sweep` consumes its exact quantity
in the same transaction as energy debit and rewards. A failed or over-budget
operation rolls back entirely. UTC midnight resets the section budget without
requiring a write or client request. Public command receipts still enforce
exact idempotent replay across restarts.

This optional policy is independent of the native Challenges weekday,
hero-filter and live-event availability rules, which have not been migrated
by this change. Its absence leaves existing ordinary missions unchanged.

# Internal game application API v1

This contract is defined by the application service, independently of any game
client's routes, encryption, field names or response templates. JSON request
objects have closed fields: unsupported fields are errors. The service never
accepts a client route, raw protocol envelope, response template, SQL statement,
or opaque player-state replacement as a command.

## Optional replayable Roguelite MVP

`roguelite` is an optional independently authored catalog section. It adds
`GET /api/v1/players/{player_id}/roguelite` and these commands without changing
the existing timed `tower.*` contract:

| Command | Parameters | Behavior |
| --- | --- | --- |
| `roguelite.start` | none | Create a server-seeded run or return the existing active run |
| `roguelite.begin` | `run_id`, `encounter_id`, `team`, optional `formation` | Freeze a battle and consume the unordered selected combination |
| `roguelite.complete` | `battle_id`, `outcome`, `stars` | Settle an owned active battle; victory advances and grants rewards plus a shard choice; defeat retains opponents and grants nothing |
| `roguelite.choose` | `run_id`, `choice_id`, `option_id`, optional `request_fingerprint` | Grant one server-issued character-specific shard option |
| `roguelite.abandon` | `battle_id` | Close combat without rewards; keep the floor, opponents and spent combination |
| `roguelite.end` | `run_id` | Resolve a pending choice using its saved first option, abandon outstanding combat without combat rewards, and close the run |

Select **one to five distinct owned characters**. Five is optional. Formation is
one distinct integer slot 0–9 per selected character, defaulting to 0..N-1. An
unordered combination may be issued once per run, including losses and abandoned
combat. A character can appear in other combinations. Restart immediately after
retirement; there are no energy costs, timers or first-clear-only payouts.

The section's closed fields are `minimum_team_size` (1), `maximum_team_size`
(5), `enemy_teams` (1–64 template IDs to five-character arrays),
`enemy_base_level`, `enemy_level_step`, `victory_rewards`, `defeat_rewards`,
`shard_pool` (1–500 unique `character_id`, `item_id`, positive `quantity` objects),
and `choice_count` (1–3, no larger than the pool). Enemy characters must be
declared; levels saturate at their individual maximum. Each shard item must be
the recruitment payment for its stated recipient and no other recipient.
Optional `opening_enemy_team` is a list of 1–5 declared character IDs used only
on floor 1. Later floors retain the randomized five-enemy templates. The authored
MVP selects a three-actor opener with the same initial strength as its three starters.
`defeat_rewards` remains accepted for catalog compatibility but is no longer paid;
use `{}` in new catalogs. Defeat never creates a shard choice. Existing winnings
remain permanent, including when the next fight is lost or the run is retired.
Ordinary victory rewards use the existing reward-bundle schema. All rules are authored
application data; no external content readers are required.

Optional `reward_bands` contains 1–500 complete reward definitions ordered by
strictly increasing `minimum_enemy_level`, starting at 1. Each entry contains
`minimum_enemy_level`, `victory_rewards` (the reward-bundle schema) and a positive
integer `shard_multiplier`. The last band whose threshold is no greater than the
lowest issued enemy level supplies the payout. Using the lowest enemy level
prevents a single high-level actor from selecting a richer band for an otherwise
weak team. A band replaces the flat `victory_rewards`; it does not add to them.
Without bands, the existing flat rewards and shard multiplier 1 apply.

The issued encounter and battle expose frozen `victory_rewards`,
`shard_multiplier` and `reward_enemy_level`. Account level, later upgrades,
formation and refreshes do not raise an issued payout. Victory grants its frozen
bundle; its permanent shard options multiply the configured recipient quantities
by the frozen multiplier. Defeat and abandonment grant neither bundle nor choice.
Existing saved flat-catalog battles without a snapshot retain their flat payout.
Catalog changes still require a new database or explicit migration.

Reward bands are authored economy data: match them to actual training, ability,
gear and recruitment/rank costs, including the required material tiers. The
mechanism alone does not prove useful progression. The example enables three
bands (10/20/40 credits and 5/10/15 shards); the independent high-level test
economy exercises funded development through level 110. Full private progression
and its native acceptance remain separate work.

Each shard option can also declare neutral `weight` (1–1000, default 100),
`era` (0–100, low for early and high for late account progression) and
`affinities` (up to 20 distinct known character IDs). The service performs
weighted sampling **without replacement**, so all eligible characters retain
positive probability and the three displayed cards need not be one per
hardcoded category. Era similarity gives a 1x–6x factor based on account
level, a strongly upgraded roster affinity gives up to a 5x factor, and
already-owned recipients receive a 2x factor for possible upgrades.
Unspecified era has a neutral factor; no affinity has the baseline factor.
Affinity strength accounts for the relative earned level-power, recruitment
rank, equipment tier, equipped items and ability upgrades of the actual
owned roster. It is a **reward weighting score**, not a native combat-stat
override; upgrading gear or rank can change odds without artificially
changing a character's server combat power or forcing a particular card.
Weights, offered items and quantities belong to the immutable catalog,
not to a native selection request. The issued three choices are persisted
before display, so reconnect and retries do not reroll the candidate set.

Read results contain `available` and `roguelite: {active_run, last_run}`. Active
runs persist `run_id`, seed, 1-based floor, display-only victory count `score`,
used combinations, current encounter, active battle ID and pending choice.
Encounters store their ID, template, floor and five neutral enemy snapshots.
Choices store their ID and immutable options with an `option_id`, recipient,
item and quantity. Get a complete owned frozen battle through the existing
battle-read endpoint. Reads never generate encounters or choices.

Victory requires 1–3 stars; defeat requires zero. Stars do not multiply rewards.
Result settlement and choices share the ordinary player transaction and command
receipts. Exact-key retries return saved results, including after run closure;
new-key requests against terminal battles or consumed choices fail without grants.
Invalid shapes/teams return 400, missing battles/mode return 404, and stale run,
encounter, active-battle, pending-choice or used-combination conflicts return 409.
Retirement receipts disclose `choice.automatic` when the prototype auto-selects.

A choice caller may supply a SHA-256 `request_fingerprint` to bind its original
request identity to the durable receipt. The read-only receipt-lookup endpoint
accepts `command: roguelite.choose` with that fingerprint and the idempotency key,
returning match/conflict/missing without needing the current pending choice. The
fingerprint contains no external protocol data and never determines rewards.

The authored [MVP catalog](examples/roguelite-mvp.json) starts at account level 1
with three heroes (`scout`, `striker`, `healer`) and zero earnings. One victory plus a shard selection funds
training, an ability upgrade and recruitment using the existing development
commands. Fifty victories accumulate fifty payouts and choices; zero victories
earn no progression. These numbers demonstrate mechanisms; broader balance is pending.
The service accepts combat outcomes from its trusted application caller under
the existing service-token/loopback policy; it does not simulate combat.

## HTTP interface

Default address: `http://127.0.0.1:18080`. POST requests require
`Content-Type: application/json` and a body of at most 1 MiB. When configured,
all requests require `Authorization: Bearer <service-token>`.

| Method | Path | Input | Result |
|---|---|---|---|
| GET | `/health` | none | `status`, `api_version` |
| GET | `/api/v1/catalog` | none | `catalog`, its SHA-256 `digest` |
| POST | `/api/v1/players` | `player_id`, `display_name` | `player` |
| GET | `/api/v1/players/{player_id}` | none | `player` |
| GET | `/api/v1/players/{player_id}/free-crates` | none | Clock-adjusted free-crate schedule; read-only |
| GET | `/api/v1/players/{player_id}/battles/{battle_id}` | none | owned `battle` |
| GET | `/api/v1/players/{player_id}/towers/{tower_id}` | none | `tower_id`, `server_time`, `available`, persisted `tower` progress with expired cooldowns omitted from the read projection |
| GET | `/api/v1/players/{player_id}/squads` | none | `squads`: configured group IDs to saved teams, current capacity, team size and level availability; read-only |
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
| `free_crate.claim` | none | Consume one authored crate, credit server-chosen rewards, persist progress and reject repeated claims |
| `shop.purchase` | `offer_id`, `quantity` | 1–100 purchases, authored cost/rewards and per-player purchase limit |
| `mission.begin` | `mission_id`, `team` | Unlocked mission, 1–5 distinct owned characters, authored cost; issues a battle ID |
| `mission.complete` | `battle_id`, `mission_id`, `outcome`, `stars` | Owned issued battle, matching mission, one settlement; victory requires 1–3 stars; defeat/retreat require 0 |
| `mission.sweep` | `mission_id`, `quantity` | Prior three-star clear, 1–100 repetitions, authored aggregate cost/rewards |
| `tower.start` | `tower_id`, `difficulty_id` | Start/resume a run in the availability window; all difficulties are available at account level 1 |
| `tower.end` | `tower_id`, `run_id` | End a run after settling its issued battle; preserve cooldowns, best score, milestone claims and floor reward history |
| `tower.begin` | `tower_id`, `run_id`, `floor_index`, `encounter_id`, `team` | Current zero-based floor and matching encounter, time gate, required owned/eligible team and cooldown checks; charge catalog cost and issue a battle |
| `tower.complete` | `battle_id`, `outcome`, `stars` | Owned issued Tower battle and validated adapter outcome; victory advances once, scores, grants first-clear floor prizes and consumes winning characters' cooldowns |
| `tower.claim` | `tower_id`, `milestone_id` | Best run meets the threshold; milestone grants once for this Tower identity |

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
- Optional `free_crates` defines `capacity` (1–100), positive `refill_seconds`,
  a rotation of independent crate IDs and an authored `rewards` bundle for each
  crate ID. The server alone advances time and selects rewards. The `free_crate.claim`
  request takes no client-selected crate, price, reward or timestamp. Claims
  use the same atomic idempotency receipts as other game commands.

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
`GET /api/v1/players/{player_id}/raids/{instance_id}` returns an owned instance.
Views include available/expired status, per-team members, room access, node
completion and unexpired locks. They never rewrite durable state. Membership
changes remain blocked while the alliance has an unexpired active raid.
Alliance views include `raid_progress` with earned difficulty/completion and
selected future difficulty. All command retries use the ordinary atomic ledger.

Native raid combat translation, refill/healing, automatic scheduling, end
settlement and result-mail delivery are still pending. The normalized combat
commands below do not establish a complete playable original-client raid mode.


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

`loot.open` requires `product_id`, `revision` and `quantity` and accepts an
optional `request_fingerprint` (64 lowercase
SHA-256 hex characters) to distinguish caller requests that normalize to the same
opening. The digest participates in ordinary command replay/conflict detection;
the server never decodes it or accepts caller-selected prices or prizes. No
fingerprint is required for existing API callers. It validates
revision, single/batch level gates and available inventory tokens, generates a
server seed, and rolls each opening with `seed:index`. The gross token debit,
all actual prizes, opening XP, seed and individual `loot_rolls` are saved in one
command receipt, including the original `server_time`. The result includes `payment` with gross quantity, previous
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

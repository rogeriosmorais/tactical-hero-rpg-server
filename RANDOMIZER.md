# Population-scale character randomizer and soft choice offers

**Status:** Implemented as standalone stdlib logic in the independent public
server, **not integrated** with gameplay, API, rewards, catalog or database.
The algorithm never reads private MSF files. Future callers supply neutral
candidate IDs, team memberships, team strengths and owned character scores.

**Owner:** server/game_server/character_randomizer.py, with tests in
server/tests/test_character_randomizer.py. Original-client team reference
remains in the separate client repository.

## Intended player experience

A reward offers three distinct characters for the player to choose from.
The choices should **often** include:

- an already-owned character worth further upgrading;
- a new character synergistic with the player's strongest owned roster;
- a new, unrelated character to encourage exploration.

**Never hardcode one slot for each.** All three positions use the same
probabilistic selector. Any category can appear zero, once, twice or three
times. An offer can contain three upgrades or three discoveries. No timer,
special unlock or fixed character exclusion is added.

## Why the initial design failed

In a 388-character pool, neutral probability is **1/388 = 0.2577%** per
character. Multiplying one character's chance by 1.2 only moves it to about
0.31% before accounting for all other weights. The original 25-character
demonstrations hid this issue.

The revised implementation treats two distinct questions:

1. **Which characters are relatively attractive?** Account progression and
   roster-upgrade context determine each candidate's baseline score.
2. **What kind of choice set should players usually see?** Soft shares of
   *total offer probability* favor upgrades, synergies, and unrelated new
   characters. Those shares are stochastic preferences, not slots or quotas.

## Phase 1: candidate scoring

The generic records are immutable dataclasses:

- Candidate(character_id, base_weight=1, strength=None): strength is an
  optional explicit [0,1] assessment, important for premium standalones that
  otherwise appear in old affiliation groups.
- Team(team_id, members, strength, kind="team"): no membership limit of
  five; characters may appear in any number of teams. The other kind is
  "affiliation", which is a broad relationship, not a combat synergy.
- BiasConfig(level_share=.25, synergy_share=.15, owned_share=.10,
  level_temperature=4, synergy_power=3, owned_power=2,
  max_relative_lift=8, affiliation_factor=.20, max_anchors=4).
- score_candidates(candidates, teams, owned_scores, *, account_level,
  level_min, level_max, config=BiasConfig()) -> tuple[CandidateScore,...]

Account progress is clamped between caller-defined minimum and maximum
levels. Specific-team strength (or explicit standalone strength) favors
older/weaker teams early, newer/stronger ones late. A character's strongest
specific team defines their default strength; broad affiliations cannot
make an otherwise weak character strong.

Owned upgrade scores are ranked relative to the entire owned roster.
Tied midrank percentiles and the top four above-median owned characters
provide anchor signals; absolute account power does not change their
relative ordering. Shared specific teams produce synergy, attenuated by
team size, while broad affiliation overlap counts for less. MAX rather
than SUM avoids double-counting heavily cross-listed characters.

Each scoring bias redirects a *portion of total pool probability* toward
high-signal candidates, using smooth exponential/power focus. An
eight-times-neutral individual ceiling prevents a small subgroup from
absorbing the entire mass. The untouched neutral remainder means every
eligible character retains a positive chance. Three features are kept
explainable via level_probability_delta, synergy_probability_delta, and
owned_probability_delta. They are **absolute probability-point changes**,
not percentage multipliers.

## Phase 2: stochastic offer composition

OfferBias defaults (user-tunable, not hardcoded slots):

| Preference channel | Initial share of the *whole next choice* |
| --- | ---: |
| Already-owned upgrade | 30% |
| New character with roster synergy | 30% |
| New character for unrelated discovery | 30% |
| Neutral exploration | 10% |

The channels describe continuous **weights**, not exclusive labels or
one-per-offer requirements:

- Upgrade affinity for an owned candidate is (0.4 + 0.6 *
  owned_percentile) raised to upgrade_power; unowned affinity is zero.
- Synergy affinity for an unowned candidate is synergy_signal raised to
  synergy_power; owned candidates receive zero on this channel.
- Discovery affinity for an unowned candidate is
  (1 - synergy_signal) raised to discovery_power; owned receives zero.
- Neutral affinity is always positive for all eligible candidates.

Each channel independently normalizes the phase-1 candidate probabilities
multiplied by its affinity. The next-pick distribution is a configurable
weighted mixture of these normalized channels. If a channel has no
eligible candidates (e.g. brand-new roster with no owned characters),
its share automatically falls back to neutral exploration.

After each pick, the next pick receives a **soft diversity discount**
for preference channels already represented in the offer. The discount is
1 / (1 + diversity_strength * accumulated_affinity), with default
diversity_strength=1.5. The remaining characters always retain positive
probabilities. The algorithm does not select a category first, enforce a
minimum, reserve positions, or manufacture an impossible choice.

- offer_probabilities(scores, *, selected=(), config=OfferBias())
  returns each remaining ID's probability **and its contribution from all
  four channels**, for explainability or simulation.
- draw_offer(scores, count, rng, *, config=OfferBias()) returns a tuple of
  **distinct** character IDs using sequential, dynamically reweighted
  draws; deterministic with an injected seeded random.Random instance.
- The older draw_one and draw_unique remain available for callers who
  want draws only from phase-1 character scores.

Only the future caller may set eligibility, upgrade caps, legal
characters, drop quantities, save state and persistent choice IDs.
The server must eventually persist issued choices, RNG seed and catalog
revision within its idempotent transaction; the private gateway only
translates presentation.

## Full-pool acceptance: 388 real eligible IDs

A one-off **offline** validation read the private client ranking/reference
externally and called this public, data-agnostic module. Five illustrative
account rosters were tested against all 388 playable characters with
3,000 reproducibly seeded three-choice offers per roster. The test script
and original data were never imported into server runtime.

For evaluation only, an unowned candidate's synergy was considered
meaningful at signal >= 0.15; weaker overlaps counted as discovery.

| Illustrative account | Avg upgrades | Avg synergistic new | Avg unrelated new | All three types in an offer |
| --- | ---: | ---: | ---: | ---: |
| Starter, old-faction focus | 0.91 | 1.00 | 1.09 | 39.2% |
| Early, leader focus | 0.94 | 0.95 | 1.11 | 41.3% |
| Midgame, mixed roster | 0.91 | 0.98 | 1.11 | 40.5% |
| Late, specialist focus | 0.93 | 0.87 | 1.20 | 40.1% |
| Endgame, elite faction focus | 0.92 | 0.89 | 1.19 | 41.0% |

These are observed sample results, **not promises** about future game
economies. Roughly 60% of offers do *not* have one of each, by design.
Different roster sizes, stronger or weaker synergies and eligibility rules
will change outcomes. Tune OfferBias after native reward UI and economy
decisions. This deliberately fixes the original tiny per-character bias.

## Standalone usage (invented catalog)

~~~python
from random import Random
from game_server.character_randomizer import (
    Candidate, Team, score_candidates, draw_offer, offer_probabilities,
)

candidates = [Candidate("captain"), Candidate("partner"),
              Candidate("other"), Candidate("newcomer")]
teams = [
    Team("old-team", ("captain", "partner", "fourth"), strength=.2),
    Team("new-team", ("other", "newcomer", "fifth"), strength=.9),
]
roster = {"captain": 5000, "spare": 100}
scores = score_candidates(
    candidates, teams, roster, account_level=20,
    level_min=1, level_max=100,
)
choices = draw_offer(scores, 3, Random("persist-this-run-seed"))
next_probs = offer_probabilities(scores, selected=choices[:1])
~~~

## EGM design and verification

This revision follows user feedback that the initial EGM plan overlooked
full-population selection odds. The first independent EGM Goldfish review
raised numerical and contract issues that were reconciled in the original
implementation; the new offer design was additionally validated on a real
388-character data snapshot plus synthetic and probability-focused tests.
This does not claim a fresh Goldfish approval or any native acceptance.

In the independent server directory (Python 3.12+):

~~~cmd
python -S -m unittest discover -s tests -p test_character_randomizer.py -v
python -S tools/check.py
~~~

There is **no gameplay integration, migration, private import, API change,
state change or client-specific implementation** in this phase.

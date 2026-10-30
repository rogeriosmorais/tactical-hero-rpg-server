"""Neutral, progression-aware weighted character selection.

This module knows no proprietary characters, sources, modes, catalogs, storage,
client protocol, or transaction boundaries. Applications supply all metadata.
Selection is reproducible with an injected random.Random instance.
"""

from dataclasses import dataclass
from math import exp, isfinite, sqrt


@dataclass(frozen=True)
class Candidate:
    character_id: str
    base_weight: float = 1.0
    strength: float | None = None  # optional independent strength for standalones


@dataclass(frozen=True)
class Team:
    team_id: str
    members: tuple[str, ...]
    strength: float
    kind: str = "team"  # 'team' or 'affiliation' (broad grouping)


@dataclass(frozen=True)
class BiasConfig:
    # Shares of the *entire eligible pool* rather than tiny per-character
    # multiplicative bonuses. Remaining share always stays broad/neutral.
    level_share: float = 0.25
    synergy_share: float = 0.15
    owned_share: float = 0.10
    level_temperature: float = 4.0
    synergy_power: float = 3.0
    owned_power: float = 2.0
    max_relative_lift: float = 8.0
    affiliation_factor: float = 0.20
    max_anchors: int = 4


@dataclass(frozen=True)
class CandidateScore:
    character_id: str
    probability: float
    base_weight: float
    multiplier: float
    team_strength: float
    level_signal: float
    synergy_signal: float
    owned_signal: float
    synergy_anchor_id: str | None = None
    synergy_team_id: str | None = None
    neutral_probability: float = 0.0
    level_probability_delta: float = 0.0
    synergy_probability_delta: float = 0.0
    owned_probability_delta: float = 0.0
    is_owned: bool = False


@dataclass(frozen=True)
class OfferBias:
    """Soft preference shares for a *whole offer*, never reserved slots.

    Each draw uses every active preference, so a 3-choice offer may have
    zero, one, two, or three owned, synergistic or unrelated characters.
    """
    upgrade_share: float = 0.30
    synergy_share: float = 0.30
    discovery_share: float = 0.30
    neutral_share: float = 0.10
    diversity_strength: float = 1.5
    upgrade_power: float = 1.0
    synergy_power: float = 1.0
    discovery_power: float = 2.0


@dataclass(frozen=True)
class OfferScore:
    character_id: str
    probability: float
    neutral_contribution: float
    upgrade_contribution: float
    synergy_contribution: float
    discovery_contribution: float


def _normalize_positive(values):
    """Normalize finite, nonnegative weights (caller guarantees nonzero sum)."""
    total = sum(values)
    return [value / total for value in values]


def _blend_capped(current, neutral, preferred, requested, max_lift):
    """Move toward a target without exceeding an individual neutral-odds cap.

    Unusable reserved probability automatically remains in the current
    distribution; it is NOT forced onto a single owned/eligible character.
    This prevents e.g. a one-character roster from receiving all 10% of an
    ownership channel at the expense of a 388-character pool.
    """
    if requested <= 0 or preferred is None:
        return current
    share = requested
    for current_p, neutral_p, target_p in zip(current, neutral, preferred):
        if target_p > current_p:
            available = max(0.0, max_lift * neutral_p - current_p)
            share = min(share, available / (target_p - current_p))
    if share <= 0:
        return current
    return [(1.0 - share) * present + share * target
            for present, target in zip(current, preferred)]


def _id(value, label):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or not value.strip() or value != value.strip()
            or any(ord(c) < 32 for c in value)):
        raise ValueError(label + " must be a nonblank stable identifier (up to 128 chars)")
    return value


def _number(value, label, minimum, maximum):
    if type(value) not in (int, float):
        raise ValueError(label + " must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(label + " must be a finite number") from error
    if not isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label} must be finite and between {minimum} and {maximum}")
    return number


def _percentiles(owned_scores):
    """Tied midrank percentiles; raw upgrade-scale magnitude never matters."""
    if not owned_scores:
        return {}
    order = sorted(owned_scores, key=lambda identifier: (owned_scores[identifier], identifier))
    if len(order) == 1 or owned_scores[order[0]] == owned_scores[order[-1]]:
        return {identifier: 0.5 for identifier in order}
    ranks = {}
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and owned_scores[order[end]] == owned_scores[order[start]]:
            end += 1
        percentile = ((start + end - 1) / 2) / (len(order) - 1)
        for identifier in order[start:end]:
            ranks[identifier] = percentile
        start = end
    return ranks


def score_candidates(
    candidates, teams, owned_scores, *, account_level, level_min, level_max,
    config=BiasConfig(),
):
    """Produce immutable, normalized candidate scores and explainable factors.

    Each supplied Candidate represents one unique *character*, not a per-team
    entry. Multiple team memberships are folded into one character probability.
    All candidates keep positive probability regardless of ownership or level.
    """
    if type(account_level) is not int or type(level_min) is not int or type(level_max) is not int:
        raise ValueError("Account levels must be integers")
    if not 0 <= level_min < level_max:
        raise ValueError("Expected nonnegative level_min < level_max")
    if not isinstance(config, BiasConfig):
        raise ValueError("config must be BiasConfig")
    for label in ("level_share", "synergy_share", "owned_share",
                  "affiliation_factor"):
        _number(getattr(config, label), label, 0.0, 0.5)
    for label in ("level_temperature", "synergy_power", "owned_power"):
        _number(getattr(config, label), label, 0.1, 10.0)
    _number(config.max_relative_lift, "max_relative_lift", 4.0, 20.0)
    if config.level_share + config.synergy_share + config.owned_share > 0.8:
        raise ValueError("Combined bias shares cannot exceed 80% of the pool")
    if type(config.max_anchors) is not int or not 1 <= config.max_anchors <= 64:
        raise ValueError("max_anchors must be an integer between 1 and 64")

    try:
        rows = tuple(candidates)
        memberships = tuple(teams)
        roster = dict(owned_scores)
    except (TypeError, ValueError) as error:
        raise ValueError("Expected candidate/team iterables and owned-score mapping") from error
    if not 1 <= len(rows) <= 10000 or len(memberships) > 10000 or len(roster) > 10000:
        raise ValueError("Candidate/team/owned data exceeds supported bounds")

    candidates_by_id = {}
    for row in rows:
        if not isinstance(row, Candidate):
            raise ValueError("candidates must contain Candidate records")
        character_id = _id(row.character_id, "candidate ID")
        if character_id in candidates_by_id:
            raise ValueError("Duplicate candidate ID: " + character_id)
        # A bounded dynamic range keeps even the smallest eligible
        # probability representable by a conventional random.Random draw
        # when up to 10,000 candidates are included.
        _number(row.base_weight, "base_weight", 1e-3, 1e3)
        if row.strength is not None:
            _number(row.strength, "candidate strength", 0.0, 1.0)
        candidates_by_id[character_id] = row

    # Canonical ordering makes both probabilities and seeded picks independent
    # of upstream catalog/dict iteration order. Source iterables stay intact.
    rows = tuple(candidates_by_id[identifier] for identifier in sorted(candidates_by_id))

    for character_id, upgrade_score in roster.items():
        _id(character_id, "owned character ID")
        _number(upgrade_score, "upgrade score", 0.0, 1e300)

    teams_by_id = {}
    teams_for_character = {}
    for team in memberships:
        if not isinstance(team, Team):
            raise ValueError("teams must contain Team records")
        team_id = _id(team.team_id, "team ID")
        if team_id in teams_by_id:
            raise ValueError("Duplicate team ID: " + team_id)
        if team.kind not in ("team", "affiliation"):
            raise ValueError("team.kind must be 'team' or 'affiliation'")
        _number(team.strength, "team strength", 0.0, 1.0)
        if isinstance(team.members, (str, bytes)):
            raise ValueError("team.members must be a sequence of unique IDs")
        try:
            member_ids = tuple(team.members)
        except TypeError as error:
            raise ValueError("team.members must be a sequence") from error
        if not 2 <= len(member_ids) <= 10000:
            raise ValueError("Team requires 2-10000 distinct members")
        for member in member_ids:
            _id(member, "team member ID")
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("Duplicate members in team " + team_id)
        for member in member_ids:
            teams_for_character.setdefault(member, set()).add(team_id)
        teams_by_id[team_id] = (team, len(member_ids))

    ranks = _percentiles(roster)
    anchors = sorted(
        (character_id for character_id, rank in ranks.items() if rank > 0.5),
        key=lambda character_id: (-ranks[character_id], character_id),
    )[:config.max_anchors]
    progress = min(1.0, max(0.0, (account_level - level_min) / (level_max - level_min)))
    result = []
    for row in rows:
        character_id = row.character_id
        own_teams = teams_for_character.get(character_id, set())
        specific_strength = [
            teams_by_id[team_id][0].strength for team_id in own_teams
            if teams_by_id[team_id][0].kind == "team"
        ]
        team_strength = (
            float(row.strength) if row.strength is not None
            else max(specific_strength, default=0.5)
        )
        level_signal = (progress - 0.5) * (2 * team_strength - 1)

        synergy_signal = 0.0
        synergy_anchor_id = None
        synergy_team_id = None
        for anchor_id in anchors:
            if anchor_id == character_id:
                continue  # Ownership alone, not team-matching with self.
            common = own_teams & teams_for_character.get(anchor_id, set())
            if not common:
                continue
            for team_id in sorted(common):
                affinity = sqrt(min(1.0, 5.0 / teams_by_id[team_id][1]))
                if teams_by_id[team_id][0].kind == "affiliation":
                    affinity *= config.affiliation_factor
                candidate_signal = (2 * ranks[anchor_id] - 1) * affinity
                if candidate_signal > synergy_signal:
                    synergy_signal = candidate_signal
                    synergy_anchor_id = anchor_id
                    synergy_team_id = team_id

        owned_signal = ranks.get(character_id, 0.0)
        result.append((row, team_strength, level_signal,
                       synergy_signal, owned_signal, synergy_anchor_id, synergy_team_id))

    # Population-scale model: mixture probabilities, NOT an almost invisible
    # 1.2x bump on top of a ~1/388 chance. Each channel places some of the
    # *total pool mass* onto a relevant group. There is always a substantial
    # neutral share, and the capped blend guards small groups against extreme
    # individual concentration.
    neutral = _normalize_positive([float(row.base_weight) for row, *_ in result])
    level_focus = abs(2.0 * progress - 1.0)
    level_preference = _normalize_positive([
        base * exp(config.level_temperature * signal)
        for base, (_, _, signal, *_rest) in zip(neutral, result)
    ])
    after_level = _blend_capped(
        neutral, neutral, level_preference,
        config.level_share * level_focus, config.max_relative_lift,
    )
    synergy_weights = [
        base * synergy ** config.synergy_power
        for base, (_, _, _, synergy, *_rest) in zip(neutral, result)
    ]
    synergy_preference = (
        _normalize_positive(synergy_weights) if sum(synergy_weights) > 0 else None
    )
    after_synergy = _blend_capped(
        after_level, neutral, synergy_preference,
        config.synergy_share, config.max_relative_lift,
    )
    owned_weights = [
        base * owned ** config.owned_power
        for base, (_, _, _, _, owned, *_rest) in zip(neutral, result)
    ]
    owned_preference = _normalize_positive(owned_weights) if sum(owned_weights) > 0 else None
    final = _blend_capped(
        after_synergy, neutral, owned_preference,
        config.owned_share, config.max_relative_lift,
    )
    return tuple(
        CandidateScore(
            character_id=row.character_id, probability=probability,
            base_weight=float(row.base_weight), multiplier=probability / neutral_p,
            team_strength=float(strength), level_signal=float(level_signal),
            synergy_signal=float(synergy), owned_signal=float(owned),
            synergy_anchor_id=anchor_id, synergy_team_id=team_id,
            neutral_probability=neutral_p,
            level_probability_delta=level_p-neutral_p,
            synergy_probability_delta=synergy_p-level_p,
            owned_probability_delta=probability-synergy_p,
            is_owned=row.character_id in roster,
        )
        for (row, strength, level_signal, synergy, owned, anchor_id, team_id),
            neutral_p, level_p, synergy_p, probability in zip(
                result, neutral, after_level, after_synergy, final
            )
    )


def draw_unique(scores, count, rng):
    """Draw distinct characters without replacement from precomputed scores."""
    choices = list(scores)
    if type(count) is not int or count < 0 or count > len(choices):
        raise ValueError("count must be between 0 and the candidate pool size")
    if not hasattr(rng, "random") or not callable(rng.random):
        raise ValueError("An injected RNG with random() is required")
    if any(not isinstance(row, CandidateScore) for row in choices):
        raise ValueError("scores must be CandidateScore records")
    if (len({row.character_id for row in choices}) != len(choices)
            or any(not isfinite(row.probability) or row.probability <= 0
                   for row in choices)):
        raise ValueError("scores must have unique IDs and positive finite probabilities")
    winners = []
    for _ in range(count):
        total = sum(row.probability for row in choices)
        value = rng.random()
        if not isinstance(value, (int, float)) or not isfinite(value) or not 0 <= value < 1:
            raise ValueError("rng.random() must return a finite value in [0,1)")
        threshold = value * total
        index = len(choices) - 1
        for candidate_index, row in enumerate(choices):
            threshold -= row.probability
            if threshold < 0:
                index = candidate_index
                break
        winners.append(choices.pop(index).character_id)
    return tuple(winners)


def draw_one(scores, rng):
    """Draw a single character ID from a score_candidates result."""
    return draw_unique(scores, 1, rng)[0]


def _offer_features(row, config):
    """Soft overlapping signals; no fixed category assigned to each slot."""
    if row.is_owned:
        return (0.4 + 0.6 * row.owned_signal) ** config.upgrade_power, 0.0, 0.0
    synergy = max(0.0, min(1.0, row.synergy_signal))
    return (0.0, synergy ** config.synergy_power,
            (1.0 - synergy) ** config.discovery_power)


def _validated_offer_rows(scores, config):
    if not isinstance(config, OfferBias):
        raise ValueError("config must be OfferBias")
    shares = (
        config.neutral_share, config.upgrade_share, config.synergy_share,
        config.discovery_share,
    )
    for name in ("neutral_share", "upgrade_share", "synergy_share", "discovery_share"):
        _number(getattr(config, name), name, 0.0, 1.0)
    if abs(sum(shares) - 1.0) > 1e-9 or config.neutral_share <= 0:
        raise ValueError("Offer shares must sum to 1 and neutral_share must be positive")
    _number(config.diversity_strength, "diversity_strength", 0, 10)
    for name in ("upgrade_power", "synergy_power", "discovery_power"):
        _number(getattr(config, name), name, 0.1, 10)
    rows = tuple(scores)
    if not 1 <= len(rows) <= 10000:
        raise ValueError("Offer requires 1-10000 CandidateScore records")
    if any(not isinstance(row, CandidateScore) for row in rows):
        raise ValueError("Offer requires CandidateScore records")
    if len({row.character_id for row in rows}) != len(rows):
        raise ValueError("Offer character IDs must be unique")
    for row in rows:
        _id(row.character_id, "offer candidate ID")
        _number(row.probability, "candidate probability", 1e-15, 1)
        _number(row.owned_signal, "owned signal", 0, 1)
        _number(row.synergy_signal, "synergy signal", 0, 1)
        if type(row.is_owned) is not bool:
            raise ValueError("is_owned must be boolean")
    return tuple(sorted(rows, key=lambda row: row.character_id))


def _offer_probabilities_validated(rows, selected, config):
    """Compute the next choice's probabilities given already issued choices.

    A proportion of whole-pool probability favors upgrade/synergy/discovery,
    not specific first/second/third positions. Positive neutral mass always
    keeps every remaining character possible.
    """
    catalog = {row.character_id: row for row in rows}
    try:
        chosen = tuple(selected)
    except TypeError as error:
        raise ValueError("selected must be an iterable of character IDs") from error
    for character_id in chosen:
        _id(character_id, "selected character ID")
    if len(chosen) != len(set(chosen)) or any(character_id not in catalog for character_id in chosen):
        raise ValueError("selected must contain distinct eligible character IDs")
    selected_set = set(chosen)
    remaining = [row for row in rows if row.character_id not in selected_set]
    if not remaining:
        return ()

    features = {row.character_id: _offer_features(row, config) for row in rows}
    coverage = [0.0, 0.0, 0.0]
    for character_id in chosen:
        upgrade, synergy, discovery = features[character_id]
        # One selected character counts as approximately one occupied
        # interest, without assigning it a hard bucket.
        total = upgrade + synergy + discovery
        if total:
            for index, signal in enumerate((upgrade, synergy, discovery)):
                coverage[index] += signal / total

    focus_shares = [
        config.upgrade_share / (1 + config.diversity_strength * coverage[0]),
        config.synergy_share / (1 + config.diversity_strength * coverage[1]),
        config.discovery_share / (1 + config.diversity_strength * coverage[2]),
    ]
    # If a preference cannot match any remaining candidate, its share
    # reverts to broadly neutral exploration rather than to a forced pick.
    values = [features[row.character_id] for row in remaining]
    bases = [row.probability for row in remaining]
    neutral = _normalize_positive(bases)
    distribution = [neutral]
    active = [config.neutral_share]
    for index, share in enumerate(focus_shares):
        weighted = [base * feature[index] for base, feature in zip(bases, values)]
        if share > 0 and sum(weighted) > 0:
            distribution.append(_normalize_positive(weighted))
            active.append(share)
        else:
            distribution.append(neutral)
            active.append(share)
    active_total = sum(active)
    shares = [share / active_total for share in active]
    scored = []
    for index, row in enumerate(remaining):
        parts = tuple(shares[channel] * distribution[channel][index]
                      for channel in range(4))
        scored.append(OfferScore(
            character_id=row.character_id,
            probability=sum(parts),
            neutral_contribution=parts[0],
            upgrade_contribution=parts[1],
            synergy_contribution=parts[2],
            discovery_contribution=parts[3],
        ))
    return tuple(scored)


def offer_probabilities(scores, *, selected=(), config=OfferBias()):
    """Explain next-offer probabilities after any distinct previous choices."""
    rows = _validated_offer_rows(scores, config)
    return _offer_probabilities_validated(rows, selected, config)


def draw_offer(scores, count, rng, *, config=OfferBias()):
    """Draw an ordered, distinct offer, with soft intra-offer diversity.

    Each iteration is stochastic, not a predetermined upgrade/synergy/new
    character slot. Any feasible composition retains nonzero probability.
    """
    rows = _validated_offer_rows(scores, config)
    if type(count) is not int or not 0 <= count <= len(rows):
        raise ValueError("count must be between 0 and the candidate pool size")
    if not hasattr(rng, "random") or not callable(rng.random):
        raise ValueError("An injected RNG with random() is required")
    selected = []
    for _ in range(count):
        probabilities = _offer_probabilities_validated(rows, selected, config)
        pick = rng.random()
        if type(pick) not in (int, float) or not isfinite(pick) or not 0 <= pick < 1:
            raise ValueError("rng.random() must return a number in [0, 1)")
        index = len(probabilities)-1
        for i, candidate in enumerate(probabilities):
            pick -= candidate.probability
            if pick < 0:
                index = i
                break
        selected.append(probabilities[index].character_id)
    return tuple(selected)

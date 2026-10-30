"""Neutral, reproducible character randomizer without MSF catalog dependencies."""

from dataclasses import FrozenInstanceError
from math import exp, inf, nan
from random import Random
import unittest

from game_server.character_randomizer import (
    BiasConfig, Candidate, Team, OfferBias, draw_offer, draw_one, draw_unique,
    offer_probabilities, score_candidates,
)


def score(candidates, teams=(), owned=None, level=1, level_min=1, level_max=101,
          config=BiasConfig()):
    return score_candidates(
        candidates, teams, owned or {},
        account_level=level, level_min=level_min, level_max=level_max,
        config=config,
    )


def by_id(rows):
    return {row.character_id: row for row in rows}


class RandomizerTests(unittest.TestCase):
    def test_account_level_favors_weak_early_and_strong_late_smoothly(self):
        candidates = [Candidate("weak"), Candidate("strong"), Candidate("neutral")]
        teams = [
            Team("old", ("weak", "old-friend"), 0.1),
            Team("modern", ("strong", "new-friend"), 0.9),
        ]
        low = by_id(score(candidates, teams))
        middle = by_id(score(candidates, teams, level=51))
        high = by_id(score(candidates, teams, level=101))
        self.assertGreater(low["weak"].probability, low["strong"].probability)
        self.assertGreater(high["strong"].probability, high["weak"].probability)
        self.assertAlmostEqual(middle["weak"].probability, middle["strong"].probability)
        self.assertAlmostEqual(middle["weak"].probability, middle["neutral"].probability)
        self.assertGreater(low["weak"].probability / low["strong"].probability, 1.5)
        self.assertGreater(high["strong"].probability / high["weak"].probability, 1.5)
        self.assertAlmostEqual(
            low["weak"].probability / low["strong"].probability,
            (0.75/3 + .25*exp(1.6)/(exp(1.6)+exp(-1.6)+1)) /
            (0.75/3 + .25*exp(-1.6)/(exp(1.6)+exp(-1.6)+1)),
        )
        self.assertEqual(low, by_id(score(candidates, teams, level=-100)))
        self.assertEqual(high, by_id(score(candidates, teams, level=100000)))

    def test_high_upgrade_synergy_works_with_unowned_teammates(self):
        candidates = [Candidate("partner"), Candidate("unrelated"), Candidate("captain")]
        teams = [Team("squad", ("captain", "partner", "third"), 0.5)]
        scores = by_id(score(candidates, teams, {"captain": 12345, "rookie": 12}))
        self.assertAlmostEqual(scores["partner"].synergy_signal, 1.0)
        self.assertEqual(scores["unrelated"].synergy_signal, 0)
        self.assertEqual(scores["captain"].synergy_signal, 0)  # no self-link
        self.assertEqual(scores["partner"].synergy_anchor_id, "captain")
        self.assertEqual(scores["partner"].synergy_team_id, "squad")
        self.assertIsNone(scores["unrelated"].synergy_team_id)
        self.assertGreater(scores["partner"].probability, scores["unrelated"].probability)
        self.assertGreater(scores["captain"].probability, scores["unrelated"].probability)

    def test_overlapping_team_memberships_and_multiple_anchors_do_not_stack(self):
        teams = [
            Team("one", ("captain", "partner", "other"), 0.5),
            Team("two", ("captain", "partner", "fourth"), 0.5),
            Team("three", ("second", "partner", "fifth"), 0.5),
        ]
        candidates = [Candidate("partner"), Candidate("unrelated")]
        rows = by_id(score(candidates, teams, {
            "captain": 100, "second": 90, "rookie": 1
        }))
        self.assertEqual(rows["partner"].synergy_signal, 1.0)
        self.assertEqual(rows["unrelated"].synergy_signal, 0.0)
        self.assertLess(rows["partner"].multiplier, 1.23)
        self.assertEqual(rows["partner"].synergy_anchor_id, "captain")
        self.assertEqual(rows["partner"].synergy_team_id, "one")

    def test_broad_faction_and_large_team_have_lower_synergy(self):
        heroes = ["anchor", "wide"] + [f"extra{i}" for i in range(48)]
        teams = [
            Team("umbrella", tuple(heroes), 0.95, "affiliation"),
            Team("small", ("anchor", "close", "other"), 0.5),
        ]
        rows = by_id(score(
            [Candidate("wide"), Candidate("close"), Candidate("unrelated")],
            teams, {"anchor": 90, "other-roster": 10}, level=51,
        ))
        self.assertGreater(rows["close"].synergy_signal, rows["wide"].synergy_signal)
        self.assertGreater(rows["wide"].synergy_signal, 0)
        self.assertLess(rows["wide"].synergy_signal, 0.1)
        self.assertEqual(rows["wide"].team_strength, 0.5)
        self.assertGreater(rows["wide"].probability, rows["unrelated"].probability)

    def test_owned_upgrade_bias_and_roster_scale_invariance(self):
        candidates = [Candidate("low"), Candidate("high"), Candidate("unowned")]
        owned = {"low": 40, "high": 500}
        a = by_id(score(candidates, owned=owned, level=51))
        b = by_id(score(candidates, owned={k: v * 1000 for k, v in owned.items()}, level=51))
        self.assertEqual(a, b)
        self.assertGreater(a["high"].probability, a["low"].probability)
        self.assertEqual(a["low"].probability, a["unowned"].probability)
        self.assertEqual(a["high"].owned_signal, 1.0)
        self.assertEqual(a["low"].owned_signal, 0.0)
        self.assertEqual(a["unowned"].owned_signal, 0.0)

    def test_equal_and_single_owner_do_not_invent_anchor(self):
        rows = [Candidate("owner"), Candidate("other")]
        teams = [Team("a", ("owner", "other"), 0.5)]
        for owned in ({"owner": 30}, {"owner": 30, "noncandidate": 30}):
            result = by_id(score(rows, teams, owned, level=51))
            self.assertEqual(result["other"].synergy_signal, 0)
            self.assertEqual(result["owner"].owned_signal, 0.5)
            self.assertGreater(result["owner"].probability, result["other"].probability)
        empty = by_id(score(rows, teams, {}, level=51))
        self.assertAlmostEqual(empty["other"].probability, 0.5)
        self.assertAlmostEqual(empty["owner"].probability, 0.5)

    def test_tied_midrank_formula_uses_unrescaled_percentiles(self):
        candidates = [Candidate("w1"), Candidate("w2"), Candidate("h1"), Candidate("h2"),
                      Candidate("partner")]
        owned = {"w1": 0, "w2": 0, "h1": 10, "h2": 10}
        teams = [Team("squad", ("h1", "partner", "third"), 0.5)]
        result = by_id(score(candidates, teams, owned, level=51))
        self.assertAlmostEqual(result["w1"].owned_signal, 1 / 6)
        self.assertAlmostEqual(result["w2"].owned_signal, 1 / 6)
        self.assertAlmostEqual(result["h1"].owned_signal, 5 / 6)
        self.assertAlmostEqual(result["h2"].owned_signal, 5 / 6)
        self.assertAlmostEqual(result["partner"].synergy_signal, 2 / 3)

    def test_more_than_five_members_and_multi_team_unbounded(self):
        teams = [Team(f"team-{i}", tuple(["hero", f"friend-{i}"] +
                       [f"member-{i}-{n}" for n in range(12)]), (i + 1) / 10)
                 for i in range(8)]
        result = score([Candidate("hero"), Candidate("outsider")], teams, level=101)
        self.assertGreater(result[0].team_strength, 0.7)
        self.assertGreater(result[0].probability, result[1].probability)

    def test_reproducible_weighted_draw_without_replacement(self):
        candidates = [Candidate(str(i), base_weight=i + 1) for i in range(12)]
        scores = score(candidates, level=1)
        self.assertAlmostEqual(sum(s.probability for s in scores), 1)
        self.assertTrue(all(s.probability > 0 for s in scores))
        self.assertEqual(draw_one(scores, Random(19)), draw_one(scores, Random(19)))
        a = draw_unique(scores, 8, Random(12345))
        b = draw_unique(scores, 8, Random(12345))
        self.assertEqual(a, b)
        self.assertEqual(len(a), len(set(a)))
        self.assertEqual(draw_unique(scores, 0, Random(1)), ())
        self.assertEqual(set(draw_unique(scores, len(scores), Random(1))),
                         {c.character_id for c in scores})
        self.assertEqual([c.character_id for c in scores],
                         sorted(str(i) for i in range(12)))
        self.assertEqual([c.character_id for c in candidates],
                         [str(i) for i in range(12)])
        self.assertEqual(
            score(list(reversed(candidates))),
            scores,
        )
        self.assertEqual(
            draw_unique(score(list(reversed(candidates))), 8, Random(12345)),
            a,
        )
        with self.assertRaises(FrozenInstanceError):
            scores[0].probability = 0

    def test_finite_bounded_coefficients_protect_base_weight(self):
        rows = by_id(score([Candidate("tiny", 1e-3), Candidate("large", 1e3)]))
        self.assertGreater(rows["tiny"].probability, 0)
        self.assertAlmostEqual(sum(row.probability for row in rows.values()), 1.0)
        self.assertGreater(rows["large"].probability, rows["tiny"].probability)
        self.assertTrue(all(0.8 <= row.multiplier <= 1.6 for row in rows.values()))

    def test_invalid_data_is_rejected(self):
        invalid_calls = [
            lambda: score([]),
            lambda: score([Candidate("a"), Candidate("a")]),
            lambda: score([Candidate("")]),
            lambda: score([Candidate(" spaces ")]),
            lambda: score([Candidate("a", 0)]),
            lambda: score([Candidate("a", nan)]),
            lambda: score([Candidate("a", inf)]),
            lambda: score([Candidate("a", 1e-4)]),
            lambda: score([Candidate("a", 1e4)]),
            lambda: score([Candidate("a", True)]),
            lambda: score([Candidate("a")], [Team("x", ("a", "a"), 0.5)]),
            lambda: score([Candidate("a")], [Team("x", ("a", []), 0.5)]),
            lambda: score([Candidate("a")], [Team("x", ("a",), 0.5)]),
            lambda: score([Candidate("a")], [Team("x", ("a", "b"), nan)]),
            lambda: score([Candidate("a")], [Team("x", ("a", "b"), 1.1)]),
            lambda: score([Candidate("a")], [Team("x", ("a", "b"), 0.5, "wrong")]),
            lambda: score([Candidate("a")], [Team("x", ("a", "b"), 0.5),
                                             Team("x", ("a", "c"), 0.5)]),
            lambda: score([Candidate("a")], owned={"a": -10}),
            lambda: score([Candidate("a")], owned={"a": inf}),
            lambda: score([Candidate("a")], level_min=1, level_max=1),
            lambda: score([Candidate("a")], level=True),
            lambda: score([Candidate("a")], config=BiasConfig(level_share=nan)),
            lambda: score([Candidate("a")], config=BiasConfig(max_anchors=0)),
            lambda: score([Candidate("a")], config=BiasConfig(synergy_share=0.5, owned_share=0.5)),
        ]
        for invalid_call in invalid_calls:
            with self.subTest(index=invalid_calls.index(invalid_call)):
                with self.assertRaises(ValueError):
                    invalid_call()

        scores = score([Candidate("a"), Candidate("b")])
        with self.assertRaises(ValueError):
            draw_unique(scores, 3, Random(1))
        with self.assertRaises(ValueError):
            draw_unique(scores, -1, Random(1))
        with self.assertRaises(ValueError):
            draw_unique(scores, 1, object())

    def test_population_scale_388_measurably_changes_outcomes(self):
        # Synthetic data, not imported/proprietary character data. Fifty-plus
        # independent choices were not enough to judge this in the first demo;
        # deterministic probability mass is the acceptance signal.
        candidates = [Candidate("id%03d" % i) for i in range(388)]
        teams = [
            Team("ancient", tuple("id%03d" % i for i in range(150)), .1),
            Team("modern", tuple("id%03d" % i for i in range(150, 300)), .9),
            Team("anchor-team", ("id300", "id301", "id302", "id303", "id304"), .5),
        ]
        baseline = 1 / 388
        neutral = by_id(score(candidates, teams, level=50,
                              config=BiasConfig(level_share=0, synergy_share=0, owned_share=0)))
        self.assertTrue(all(abs(x.probability - baseline) < 1e-12 for x in neutral.values()))

        low = by_id(score(candidates, teams, level=1))
        high = by_id(score(candidates, teams, level=101))
        low_mass = sum(low["id%03d" % i].probability for i in range(150))
        high_mass = sum(high["id%03d" % i].probability for i in range(150, 300))
        self.assertGreater(low_mass, 150/388 + 0.05)
        self.assertGreater(high_mass, 150/388 + 0.05)
        self.assertLess(low["id200"].probability, baseline)
        self.assertLess(high["id020"].probability, baseline)

        owned = {"id300": 5000, "id305": 400, "id306": 100,
                 "id307": 50, "id308": 10}
        weighted = by_id(score(candidates, teams, owned, level=51))
        specific = weighted["id301"]
        self.assertGreater(specific.probability, 4 * baseline)
        self.assertGreater(specific.probability, weighted["id309"].probability)
        self.assertEqual(specific.synergy_anchor_id, "id300")
        self.assertAlmostEqual(
            specific.probability,
            specific.neutral_probability + specific.level_probability_delta
            + specific.synergy_probability_delta + specific.owned_probability_delta,
            places=13,
        )
        self.assertGreater(weighted["id300"].probability, 4 * baseline)
        self.assertLessEqual(max(x.probability / x.neutral_probability
                                 for x in weighted.values()), 8 + 1e-9)
        self.assertGreater(min(x.probability for x in weighted.values()), 0)
        self.assertAlmostEqual(sum(x.probability for x in weighted.values()), 1.0)

    def test_single_owned_character_cannot_capture_entire_reserved_budget(self):
        candidates = [Candidate("id%03d" % i) for i in range(388)]
        weighted = by_id(score(candidates, owned={"id000": 100}, level=51))
        self.assertGreater(weighted["id000"].probability, 1/388)
        self.assertLessEqual(weighted["id000"].probability, 8/388 + 1e-12)
        self.assertGreater(weighted["id100"].probability, 0)
        self.assertAlmostEqual(sum(s.probability for s in weighted.values()), 1)

    def test_explicit_standalone_strength_overrides_missing_or_weak_team(self):
        rows = [
            Candidate("standalone", strength=0.99),
            Candidate("low", strength=0.01),
            Candidate("middle"),
        ]
        teams = [Team("legacy", ("standalone", "low", "other"), .1)]
        high = by_id(score(rows, teams, level=101))
        low = by_id(score(rows, teams, level=1))
        self.assertEqual(high["standalone"].team_strength, .99)
        self.assertEqual(high["middle"].team_strength, .5)
        self.assertGreater(high["standalone"].probability, high["low"].probability)
        self.assertGreater(low["low"].probability, low["standalone"].probability)
        with self.assertRaises(ValueError):
            score([Candidate("bad", strength=1.1)])

    def test_soft_offer_bias_spreads_probability_across_small_rare_groups(self):
        # Owned has 5/388 members; per-character weighting cannot generate
        # a useful upgrade option frequency without a population-level choice.
        candidates = [Candidate("id%03d" % i) for i in range(388)]
        teams = [
            Team("upgraded-teammates", tuple("id%03d" % i for i in range(12)), .5)
        ]
        owned = {"id000": 1000, "id001": 400, "id002": 200,
                 "id020": 100, "id021": 20}
        scores = score(candidates, teams, owned, level=51)
        offer = offer_probabilities(scores)
        by_name = {r.character_id: r for r in offer}
        self.assertEqual(len(offer), 388)
        self.assertAlmostEqual(sum(r.probability for r in offer), 1)
        self.assertTrue(all(r.probability > 0 for r in offer))
        upgrade_mass = sum(by_name[c].probability for c in owned)
        synergy_mass = sum(by_name["id%03d" % i].probability for i in range(3, 12))
        self.assertGreater(upgrade_mass, 0.25)
        self.assertGreater(synergy_mass, 0.25)
        self.assertGreater(sum(by_name["id%03d" % i].probability
                               for i in range(22, 388)), 0.25)
        # Changes each *whole group* probability, rather than adding
        # a few hundredths of one percent to a handful of characters.
        self.assertGreater(upgrade_mass, len(owned) / 388 * 15)
        self.assertGreater(synergy_mass, 9 / 388 * 10)

    def test_soft_offer_diversity_affects_probs_but_never_forces_categories(self):
        candidates = [Candidate("id%03d" % i) for i in range(388)]
        teams = [Team("squad", tuple("id%03d" % i for i in range(6)), .5)]
        owned = {"id000": 1000, "id010": 100, "id011": 50}
        scores = score(candidates, teams, owned, level=51)
        first = offer_probabilities(scores)
        after_upgrade = offer_probabilities(scores, selected=("id000",))
        after_new_synergy = offer_probabilities(scores, selected=("id003",))
        self.assertLess(
            sum(x.probability for x in after_upgrade if x.character_id in owned),
            sum(x.probability for x in first if x.character_id in owned),
        )
        self.assertLess(
            sum(x.probability for x in after_new_synergy
                if x.character_id in {"id004", "id005"}),
            sum(x.probability for x in first
                if x.character_id in {"id004", "id005"}),
        )
        self.assertTrue(all(x.probability > 0 for x in after_upgrade))
        self.assertTrue(all(x.probability > 0 for x in after_new_synergy))
        self.assertAlmostEqual(sum(x.probability for x in after_upgrade), 1)
        self.assertAlmostEqual(sum(x.probability for x in after_new_synergy), 1)

        # A counterexample to any hard "one of each" implementation: 3
        # unrelated characters, 3 upgrades, or 3 synergy picks must all
        # be possible despite lower probability than a varied offer.
        from collections import Counter
        counts = Counter()
        for seed in range(500):
            offer = draw_offer(scores, 3, Random(seed))
            self.assertEqual(len(set(offer)), 3)
            labels = tuple("owned" if c in owned else
                           "synergy" if c in {"id003", "id004", "id005"} else
                           "discovery" for c in offer)
            counts[labels] += 1
        self.assertGreater(sum(1 for labels in counts if len(set(labels)) == 3), 0)
        self.assertTrue(any(len(set(labels)) == 1 for labels in counts))

    def test_offer_order_independence_seed_reproducibility_and_fallback(self):
        candidates = [Candidate("id%03d" % i) for i in range(100)]
        teams = [Team("squad", ("id000", "id002", "id003"), .5)]
        owned = {"id000": 100, "id010": 10}
        scores = score(candidates, teams, owned, level=30)
        draws = draw_offer(scores, 3, Random(333))
        self.assertEqual(draw_offer(tuple(reversed(scores)), 3, Random(333)), draws)
        self.assertEqual(len(draws), 3)
        self.assertEqual(offer_probabilities(scores, selected=tuple(
            s.character_id for s in scores)), ())
        self.assertEqual(draw_offer(scores, 0, Random(333)), ())
        # Without an owned pool the upgrade preference is smoothly
        # redistributed; no division by zero or forced upgrades.
        unowned = score(candidates, teams, {}, level=30)
        result = draw_offer(unowned, 3, Random(333))
        self.assertEqual(len(set(result)), 3)
        self.assertAlmostEqual(sum(s.probability for s in offer_probabilities(unowned)), 1)

        with self.assertRaises(ValueError):
            draw_offer(scores, 101, Random(1))
        with self.assertRaises(ValueError):
            draw_offer(scores, True, Random(1))
        with self.assertRaises(ValueError):
            offer_probabilities(scores, selected=("missing",))
        with self.assertRaises(ValueError):
            offer_probabilities(scores, selected=("id000", "id000"))
        with self.assertRaises(ValueError):
            offer_probabilities(scores, selected=([],))
        with self.assertRaises(ValueError):
            offer_probabilities(scores, selected=None)
        with self.assertRaises(ValueError):
            offer_probabilities(scores, config=OfferBias(neutral_share=0.0))
        with self.assertRaises(ValueError):
            offer_probabilities(scores, config=OfferBias(upgrade_share=-1))

    def test_offer_degrades_gracefully_if_no_available_preferred_group(self):
        candidates = [Candidate("id%03d" % i) for i in range(8)]
        teams = [Team("squad", ("id000", "id001", "id002"), .5)]
        all_owned = {c.character_id: 100 for c in candidates}
        for roster in (all_owned, {}, {"id000": 100}):
            scores = score(candidates, teams, roster, level=51)
            offer = offer_probabilities(scores)
            self.assertAlmostEqual(sum(x.probability for x in offer), 1.0)
            self.assertTrue(all(x.probability > 0 for x in offer))
            picked = draw_offer(scores, 8, Random(123))
            self.assertEqual(set(picked), {c.character_id for c in candidates})
        # A prior owned choice must not make another owned option impossible
        # if the remaining pool still contains it.
        scores = score(candidates, teams, all_owned, level=51)
        next_choice = offer_probabilities(scores, selected=("id000",))
        self.assertTrue(all(x.probability > 0 for x in next_choice))
        self.assertAlmostEqual(sum(x.probability for x in next_choice), 1)

    def test_offer_probability_parts_add_exactly_and_are_nonnegative(self):
        candidates = [Candidate("id%03d" % i) for i in range(40)]
        teams = [Team("squad", ("id000", "id001", "id002", "id003"), .5)]
        scores = score(candidates, teams, {"id000": 100, "id030": 20}, level=40)
        for prior in ((), ("id000",), ("id002", "id030")):
            distribution = offer_probabilities(scores, selected=prior)
            self.assertAlmostEqual(sum(x.probability for x in distribution), 1)
            for option in distribution:
                parts = (
                    option.neutral_contribution,
                    option.upgrade_contribution,
                    option.synergy_contribution,
                    option.discovery_contribution,
                )
                self.assertTrue(all(value >= 0 for value in parts))
                self.assertAlmostEqual(sum(parts), option.probability)


if __name__ == "__main__":
    unittest.main()

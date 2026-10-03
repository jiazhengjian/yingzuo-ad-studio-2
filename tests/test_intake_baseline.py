"""Integrity checks for human acceptance fixtures, not a semantic quality evaluator.

Standard-library only: do not import the application, settings, DB, or providers.
Passing these checks proves references and coverage are reviewable, not that a
generated script is good or that any intake scenario has passed at runtime.
"""
import json
from pathlib import Path
import unittest


FIXTURES = Path(__file__).parent / "fixtures" / "intake_acceptance"


def read_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class IntakeBaselineIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = read_fixture("reference_samples.json")
        cls.intake = read_fixture("intake_cases.json")
        cls.regressions = read_fixture("regressions.json")
        cls.products = {p["id"]: p for p in cls.samples["products"]}

    def test_three_products_cover_each_ad_form_once(self):
        forms = {"brand_tvc", "scene_story", "presenter_pitch"}
        self.assertEqual(set(self.samples["ad_forms"]), forms)
        self.assertEqual(set(self.products), {"hairdryer", "cake", "reading_stand"})
        refs = self.samples["reference_samples"]
        self.assertEqual(len(refs), 9)
        self.assertEqual(len({r["id"] for r in refs}), len(refs))
        self.assertEqual(
            {(r["product_id"], r["ad_form"]) for r in refs},
            {(p, form) for p in self.products for form in forms},
        )
        self.assertEqual(self.products["reading_stand"]["partition"], "held_out")
        self.assertTrue(self.samples["held_out_policy"])

    def test_product_inputs_separate_synthetic_facts_and_unknowns(self):
        self.assertEqual(
            self.samples["origin"],
            "all_product_inputs_and_reference_scripts_are_synthetic",
        )
        for product in self.products.values():
            with self.subTest(product=product["id"]):
                for field in ("input", "assumptions", "unknowns", "forbidden_inferences"):
                    self.assertTrue(product[field])
                fact_ids = [f["id"] for f in product["facts"]]
                self.assertEqual(len(fact_ids), len(set(fact_ids)))
                self.assertTrue(fact_ids)
                for fact in product["facts"]:
                    self.assertEqual(fact["basis"], "synthetic_merchant_input")
                    self.assertTrue(fact["text"])

    def test_storyboard_references_and_planned_time_are_complete(self):
        for reference in self.samples["reference_samples"]:
            with self.subTest(reference=reference["id"]):
                product = self.products[reference["product_id"]]
                facts = {f["id"] for f in product["facts"]}
                top_refs = set(reference["evidence_ids"])
                self.assertTrue(top_refs)
                self.assertLessEqual(top_refs, facts)
                for field in ("core_message", "voice_decision", "why_this_form"):
                    self.assertTrue(reference[field])
                shots = reference["shots"]
                self.assertGreaterEqual(len(shots), 2)
                self.assertEqual(len({s["id"] for s in shots}), len(shots))
                end = 0
                used_facts = set()
                for shot in shots:
                    self.assertEqual(shot["start"], end)
                    self.assertGreater(shot["end"], shot["start"])
                    end = shot["end"]
                    self.assertLessEqual(set(shot["evidence_ids"]), facts)
                    used_facts.update(shot["evidence_ids"])
                    for field in ("visual", "speech_role", "continuity"):
                        self.assertTrue(shot[field])
                    self.assertIn(shot["speech_mode"], {"none", "voiceover", "character_dialogue"})
                    self.assertEqual(shot["speech"] == "", shot["speech_mode"] == "none")
                self.assertEqual(top_refs, used_facts)
                self.assertEqual(end, reference["duration_seconds"])
                self.assertGreaterEqual(end, 10)
                self.assertLessEqual(end, 60)

    def test_intake_cases_have_observable_expected_results(self):
        cases = self.intake["cases"]
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        allowed = {None, "auto", *self.samples["ad_forms"]}
        required_cases = {
            "explicit_tvc", "explicit_story", "explicit_presenter", "explicit_auto",
            "persist_card_choice", "inherit_during_other_edit", "explicit_change",
            "negation_is_not_selection", "question_not_choice",
            "conditional_not_choice", "image_only_no_parameters",
            "desired_claim_is_unverified", "conflicting_fact_correction",
        }
        self.assertLessEqual(required_cases, {c["id"] for c in cases})
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["product_id"], self.products)
                self.assertIn(case["initial_ad_form"], allowed)
                self.assertIn(case["expected_ad_form"], allowed)
                self.assertTrue(case["user_turns"] or case.get("ui_actions"))
                self.assertTrue(case["expected_behavior"])
                self.assertTrue(case["must_not"])
                self.assertEqual(case["scope"], "stage1")

    def test_regression_provenance_and_existing_fixture_link_resolve(self):
        sources = {s["id"]: s for s in self.regressions["sources"]}
        cases = self.regressions["cases"]
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["source_id"], sources)
                self.assertIn(case["product_id"], self.products)
                for field in ("origin", "observed_text", "visual_context", "expected_review", "checks"):
                    self.assertTrue(case[field])
        for source in sources.values():
            if source["kind"] == "existing_repository_fixture":
                path = Path(__file__).resolve().parents[1] / source["path"]
                existing = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn(source["entry_id"], {b["id"] for b in existing["baselines"]})

    def test_fixtures_do_not_claim_model_or_human_acceptance(self):
        self.assertEqual(self.samples["status"], "reference_candidates_pending_human_acceptance")
        self.assertEqual(self.intake["execution_status"], "not_run")
        self.assertEqual(self.regressions["execution_status"], "not_run")
        self.assertTrue(all(v is False for v in self.samples["runtime_validation"].values()))
        for reference in self.samples["reference_samples"]:
            self.assertEqual(reference["origin"], "author_written_reference")
            self.assertEqual(reference["review_status"], "pending_human_acceptance")


if __name__ == "__main__":
    unittest.main()

import unittest

from jobbot.integrations.form_matching import (
    best_field_label_match,
    best_submit_control_match,
    classify_form_submission,
    field_label_score,
    normalize_field_label,
    submit_action_score,
)


class FormMatchingTests(unittest.TestCase):
    def test_normalizes_unicode_whitespace_accents_and_punctuation(self):
        self.assertEqual(
            normalize_field_label("  Résumé\xa0(required) * "),
            "resume required",
        )

    def test_finds_resume_in_real_ashby_visible_questions(self):
        visible_questions = [
            "Name",
            "Email",
            "Resume",
            "In which country will you perform services for Clipboard?",
            "Are you authorized to work in your country of residence?",
            "Have you ever worked for Clipboard?",
            "How did you hear about the position?\xa0",
        ]
        self.assertEqual(
            best_field_label_match("Resume", visible_questions),
            2,
        )

    def test_matches_provider_presentation_wording_without_hardcoded_aliases(self):
        self.assertGreaterEqual(
            field_label_score("Résumé", "Please upload your resume *"),
            0.90,
        )
        self.assertEqual(
            best_field_label_match(
                "Résumé",
                ["Email address", "Please upload your resume *"],
            ),
            1,
        )

    def test_rejects_ambiguous_partial_matches(self):
        self.assertIsNone(
            best_field_label_match(
                "Name",
                ["Legal name", "Preferred name"],
            )
        )

    def test_rejects_unrelated_fields(self):
        self.assertIsNone(
            best_field_label_match(
                "Resume",
                ["Email address", "Phone number"],
            )
        )


    def test_finds_generic_submit_control_from_real_ashby_markup(self):
        labels = ["Save for later", "Submit Application"]
        self.assertEqual(best_submit_control_match(labels), 1)
        self.assertGreater(submit_action_score("Submit Application"), 0)

    def test_accepts_bare_submit_label(self):
        self.assertEqual(best_submit_control_match(["Cancel", "Submit"]), 1)

    def test_rejects_ambiguous_submit_controls(self):
        self.assertIsNone(best_submit_control_match(["Submit form", "Submit application"]))

    def test_classifies_structural_success_with_custom_message(self):
        outcome = classify_form_submission(
            success_present=True,
            success_text="We have it — watch your inbox.",
        )
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, "submitted")
        self.assertEqual(outcome.detail, "We have it — watch your inbox.")

    def test_classifies_ashby_default_success_text(self):
        outcome = classify_form_submission(
            success_present=True,
            success_text=(
                "Success\nYour application was successfully submitted. "
                "We'll contact you if there are next steps."
            ),
        )
        self.assertEqual(outcome.status, "submitted")

    def test_explicit_success_wins_over_stale_alert_or_challenge(self):
        outcome = classify_form_submission(
            success_present=True,
            failure_present=True,
            failure_text="Old validation error",
            challenge_present=True,
        )
        self.assertEqual(outcome.status, "submitted")

    def test_classifies_failure_challenge_and_pending(self):
        failed = classify_form_submission(
            failure_present=True,
            failure_text="We couldn't submit your application",
        )
        manual = classify_form_submission(challenge_present=True)
        pending = classify_form_submission()
        self.assertEqual(failed.status, "failed")
        self.assertEqual(manual.status, "manual_required")
        self.assertIsNone(pending)


if __name__ == "__main__":
    unittest.main()

import unittest

from jobbot.integrations.form_matching import (
    best_field_label_match,
    field_label_score,
    normalize_field_label,
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


if __name__ == "__main__":
    unittest.main()

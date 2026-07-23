import json
import tempfile
import unittest
from pathlib import Path

from jobbot.form_answers import (
    FormQuestion,
    classify_question,
    create_answer_batch,
    deduplicate_questions,
    forget_fact_by_token,
    fact_token,
    get_fact,
    get_pending_batches,
    migrate_profile_json,
    parse_numbered_answers,
    profile_document,
    save_fact,
    set_batch_message_id,
)


class FormAnswerMatchingTests(unittest.TestCase):
    def test_sponsorship_polarity_and_time_are_distinct(self):
        context = {"job_id": "job-1", "job_country": "United States"}
        require = classify_question(
            "ashby", "a", "Will you require visa sponsorship?", "Boolean",
            context=context,
        )
        without = classify_question(
            "greenhouse", "b", "Can you work without visa sponsorship?", "Boolean",
            context=context,
        )
        future = classify_question(
            "ashby", "c", "Will you require sponsorship in the future?", "Boolean",
            context=context,
        )
        authorized = classify_question(
            "ashby", "d", "Are you legally authorized to work in the United States?",
            "Boolean", context=context,
        )
        self.assertEqual(require.canonical_fact, "work.requires_sponsorship_now")
        self.assertEqual(require.answer_key, without.answer_key)
        self.assertFalse(require.invert_boolean)
        self.assertTrue(without.invert_boolean)
        self.assertEqual(future.canonical_fact, "work.requires_sponsorship_future")
        self.assertEqual(authorized.canonical_fact, "work.authorized")
        self.assertNotEqual(require.answer_key, future.answer_key)
        self.assertNotEqual(require.answer_key, authorized.answer_key)

    def test_country_and_company_scopes_do_not_leak(self):
        france = classify_question(
            "ashby", "a", "Are you authorized to work in France?", "Boolean",
            context={"job_id": "1"},
        )
        canada = classify_question(
            "ashby", "b", "Are you authorized to work in Canada?", "Boolean",
            context={"job_id": "2"},
        )
        first_company = classify_question(
            "ashby", "c", "Were you previously employed by this company?", "Boolean",
            context={"job_id": "1", "company": "First Corp"},
        )
        second_company = classify_question(
            "ashby", "d", "Were you previously employed by this company?", "Boolean",
            context={"job_id": "2", "company": "Second Corp"},
        )
        self.assertNotEqual(france.answer_key, canada.answer_key)
        self.assertNotEqual(first_company.answer_key, second_company.answer_key)

    def test_numbered_batch_accepts_options_and_reports_only_bad_lines(self):
        questions = (
            FormQuestion(
                "test", "a", "Need sponsorship?", "Boolean",
                canonical_fact="work.requires_sponsorship_now",
            ),
            FormQuestion(
                "test", "b", "Source", "Select", ("LinkedIn", "Indeed"),
                canonical_fact="application.source",
            ),
        )
        parsed = parse_numbered_answers("1. No\n2. 1", questions)
        self.assertEqual(parsed.answers, {1: False, 2: "LinkedIn"})
        self.assertEqual(parsed.errors, ())
        invalid = parse_numbered_answers("1. Maybe\n2. Reddit", questions)
        self.assertEqual(invalid.answers, {})
        self.assertEqual(len(invalid.errors), 2)

    def test_deduplication_uses_fact_and_scope_not_raw_wording(self):
        questions = [
            classify_question(
                "a", "one", "Will you require sponsorship?", "Boolean",
                context={"job_id": "same"},
            ),
            classify_question(
                "b", "two", "Can you work without sponsorship?", "Boolean",
                context={"job_id": "same"},
            ),
        ]
        self.assertEqual(len(deduplicate_questions(questions)), 1)


class FormAnswerRepositoryTests(unittest.TestCase):
    def test_profile_json_migrates_once_and_database_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "jobs.db"
            profile_path = root / "applicant.json"
            profile_path.write_text(json.dumps({
                "first_name": "Ada",
                "email": "ada@example.com",
                "location": {"country": "France"},
            }), encoding="utf-8")
            migrate_profile_json(db_path, profile_path)
            profile_path.write_text(json.dumps({"first_name": "Changed"}), encoding="utf-8")
            migrate_profile_json(db_path, profile_path)
            document = profile_document(db_path)
            self.assertEqual(document["first_name"], "Ada")
            self.assertEqual(document["email"], "ada@example.com")
            self.assertEqual(document["location"]["country"], "France")

    def test_fact_reuse_polarity_and_forget_token(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "jobs.db"
            direct = classify_question(
                "ashby", "a", "Will you require sponsorship?", "Boolean",
                context={"job_id": "same"},
            )
            inverted = classify_question(
                "greenhouse", "b", "Can you work without sponsorship?", "Boolean",
                context={"job_id": "same"},
            )
            save_fact(db_path, direct, False, source="test")
            resolution = get_fact(db_path, inverted)
            self.assertIsNotNone(resolution)
            self.assertTrue(resolution.form_value)
            self.assertTrue(forget_fact_by_token(db_path, fact_token(direct)))
            self.assertIsNone(get_fact(db_path, direct))

    def test_pending_batch_survives_repository_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "jobs.db"
            question = FormQuestion(
                "test", "a", "Email", "text",
                canonical_fact="profile.email",
            )
            batch_id = create_answer_batch(db_path, "job", 42, (question,))
            set_batch_message_id(db_path, batch_id, 99)
            batches = get_pending_batches(db_path, 42)
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0]["bot_message_id"], 99)
            self.assertEqual(batches[0]["questions"], (question,))


if __name__ == "__main__":
    unittest.main()

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from web_application import ApplicantProfile, WebApplicationError, build_form_payload, submit_application

FORM_HTML = """
<html><body><form method="post">
<p>Do you live in Poland?</p>
<input type="radio" name="q1" value="1" required><input type="radio" name="q1" value="0">
<p>Do you have a sole proprietorship?</p>
<input type="radio" name="q2" value="1" required><input type="radio" name="q2" value="0">
<p>Are you fluent in English?</p>
<input type="radio" name="q3" value="1" required><input type="radio" name="q3" value="0">
<input type="text" name="prenom" placeholder="First name" required>
<input type="text" name="nom" placeholder="Last name" required>
<input type="email" name="email" required>
<input type="tel" name="tel" required>
<input type="hidden" name="MAX_SIZE" value="2124000">
<input type="file" name="cv" required>
<textarea name="motiv" placeholder="Describe your motivations"></textarea>
<input type="submit" value="Apply">
</form></body></html>
"""


class WebApplicationTests(unittest.TestCase):
    def test_maps_profile_resume_and_screening_answers(self):
        profile = ApplicantProfile("Ekaterina", "Tuganova", "me@example.com", "+995 555 123", {"q1": "0", "q2": "0", "q3": "1"})
        action, data, file_field = build_form_payload(FORM_HTML, "https://jobs.example/42", profile)
        self.assertEqual(action, "https://jobs.example/42")
        self.assertEqual(file_field, "cv")
        self.assertEqual(data["prenom"], "Ekaterina")
        self.assertEqual(data["nom"], "Tuganova")
        self.assertEqual(data["q1"], "0")
        self.assertEqual(data["q3"], "1")

    def test_refuses_to_guess_required_screening_answers(self):
        profile = ApplicantProfile("Ekaterina", "Tuganova", "me@example.com", "+995 555 123", {})
        with self.assertRaisesRegex(WebApplicationError, "q1, q2, q3"):
            build_form_payload(FORM_HTML, "https://jobs.example/42", profile)

    def test_submits_multipart_only_after_complete_preflight(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, text=FORM_HTML, request=request)
            self.assertIn(b'name="cv"', request.content)
            self.assertIn(b'name="prenom"', request.content)
            self.assertIn(b"Ekaterina", request.content)
            return httpx.Response(200, text="<h1>Votre candidature a été envoyée</h1>", request=request)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "resume.pdf"
            resume.write_bytes(b"pdf")
            profile = root / "applicant.json"
            profile.write_text(json.dumps({
                "first_name": "Ekaterina", "last_name": "Tuganova",
                "email": "me@example.com", "phone": "+995555123",
                "answers": {"q1": "0", "q2": "0", "q3": "1"},
            }), encoding="utf-8")
            with patch("web_application.validate_public_url", new=AsyncMock()), \
                 patch("web_application.extract_resume_text", return_value=""):
                result = asyncio.run(submit_application(
                    "https://jobs.example/42", resume, profile,
                    transport=httpx.MockTransport(handler),
                ))
        self.assertEqual(result, "https://jobs.example/42")
        self.assertEqual([request.method for request in requests], ["GET", "POST"])


if __name__ == "__main__":
    unittest.main()
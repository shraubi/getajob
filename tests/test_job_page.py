import asyncio
import unittest
from unittest.mock import patch

from job_page import UnsafeUrlError, extract_first_url, parse_job_html, validate_public_url

MESSAGE = """[Staff Data Engineer (Azure)](https://hirify.me/jobs/711417-staff-data-engineer-azure-python?utm_source=subscription) (https://hirify.me/jobs/711417-staff-data-engineer-azure-python?utm_source=subscription) in NDA"""

JSON_LD_HTML = """
<html><head>
<script type="application/ld+json">{
  "@context": "https://schema.org", "@type": "JobPosting",
  "title": "Staff Data Engineer (Azure)",
  "hiringOrganization": {"@type": "Organization", "name": "Acme Data"},
  "description": "Build Python and SQL data pipelines on Azure with Databricks and Apache Spark. Own ETL and data warehouse design."
}</script></head><body>
<main><h1>Staff Data Engineer (Azure)</h1>
<a href="/applications/start">Apply now</a></main>
</body></html>
"""

FORM_HTML = """
<html><head><title>Backend Engineer</title><meta name="description" content="Join our backend team to build Python APIs and reliable distributed services for customers worldwide."></head>
<body><main><h1>Backend Engineer</h1><form action="/contact"><input name="resume" type="file"><button>Contact us</button></form></main></body></html>
"""


class JobPageTests(unittest.TestCase):
    def test_extracts_first_markdown_url_without_trailing_parenthesis(self):
        self.assertEqual(
            extract_first_url(MESSAGE),
            "https://hirify.me/jobs/711417-staff-data-engineer-azure-python?utm_source=subscription",
        )

    def test_parses_json_ld_and_generic_apply_link(self):
        parsed = parse_job_html(JSON_LD_HTML, "https://example.com/jobs/42")
        self.assertEqual(parsed.vacancy.title, "Staff Data Engineer (Azure)")
        self.assertEqual(parsed.vacancy.company, "Acme Data")
        self.assertEqual(parsed.source_category, "structured_job_page")
        self.assertEqual(parsed.apply_url, "https://example.com/applications/start")

    def test_detects_generic_application_form(self):
        parsed = parse_job_html(FORM_HTML, "https://careers.example.com/jobs/backend")
        self.assertEqual(parsed.source_category, "application_form")
        self.assertEqual(parsed.apply_url, "https://careers.example.com/contact")

    @patch("job_page._resolved_ips", return_value={__import__("ipaddress").ip_address("127.0.0.1")})
    def test_rejects_private_destinations(self, _resolve):
        with self.assertRaises(UnsafeUrlError):
            asyncio.run(validate_public_url("https://example.com/jobs/1"))


if __name__ == "__main__":
    unittest.main()

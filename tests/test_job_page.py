import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from jobbot.integrations.job_page import JobPageError, UnsafeUrlError, extract_first_url, fetch_html, parse_job_html, resolve_application_url, validate_public_url

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

    def test_detects_russian_javascript_apply_button(self):
        html = """<html><head><meta name="description" content="Python backend role with FastAPI and PostgreSQL for a distributed services team."></head>
        <body><main><h1>Python-разработчик</h1><button>Отправить резюме</button></main></body></html>"""
        parsed = parse_job_html(html, "https://example.com/vacancy/282")
        self.assertEqual(parsed.apply_url, "https://example.com/vacancy/282")

    @patch("jobbot.integrations.job_page.validate_public_url", new_callable=AsyncMock)
    def test_follows_html_short_link_to_application_form(self, _validate):
        pages = {
            "/short": '<html><body><main><h1>Redirect</h1><p>Continue to the vacancy application below.</p><a href="https://apply.example/form">Apply</a></main></body></html>',
            "/form": '<html><body><main><h1>Apply</h1><p>Submit your resume for this engineering vacancy.</p><form><input type="file" name="resume"></form></main></body></html>',
        }
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, text=pages[request.url.path], headers={"content-type": "text/html"}, request=request
        ))
        result = asyncio.run(resolve_application_url("https://lnkd.in/short", transport=transport))
        self.assertEqual(result, "https://apply.example/form")

    @patch("jobbot.integrations.job_page._resolved_ips", return_value={__import__("ipaddress").ip_address("127.0.0.1")})
    def test_rejects_private_destinations(self, _resolve):
        with self.assertRaises(UnsafeUrlError):
            asyncio.run(validate_public_url("https://example.com/jobs/1"))

    @patch("jobbot.integrations.job_page.validate_public_url", new_callable=AsyncMock)
    def test_rejects_declared_oversize_before_reading_body(self, _validate):
        class ExplodingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise AssertionError("oversize body should not be read")

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": "2000001"},
                stream=ExplodingStream(),
                request=request,
            )
        )
        with self.assertRaisesRegex(JobPageError, "too large"):
            asyncio.run(fetch_html("https://example.com/jobs/1", transport=transport))


if __name__ == "__main__":
    unittest.main()

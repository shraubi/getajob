"""Tests for the Ralph loop module."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from ralph.loop import run_loop, LoopConfig, get_known_urls, get_pending_failures
from ralph.rating import RatingReport, StageRating


class RalphLoopTests(unittest.TestCase):
    def test_loop_config_defaults(self):
        config = LoopConfig()
        self.assertEqual(config.filter_source, "hirify")
        self.assertEqual(config.feed_url, "https://hirify.me/")
        self.assertEqual(config.limit, 10)
        self.assertEqual(config.db_path, Path("storage/ralph.db"))
        self.assertFalse(config.dry_run)
        self.assertFalse(config.quiet)

    def test_get_known_urls_empty_db(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.db"
            urls = get_known_urls(db_path)
            self.assertEqual(urls, set())

    def test_get_known_urls_with_data(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.db"
            # Create a test database with some runs
            import sqlite3
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "CREATE TABLE ralph_runs (id TEXT PRIMARY KEY, url TEXT NOT NULL, domain TEXT NOT NULL, score INTEGER NOT NULL, status TEXT NOT NULL, report_json TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO ralph_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("run1", "https://example.com/job1", "example.com", 100, "passed", "{}", "2024-01-01"),
                )
                connection.execute(
                    "INSERT INTO ralph_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("run2", "https://example.com/job2", "example.com", 80, "failed", "{}", "2024-01-02"),
                )
                connection.commit()
            finally:
                connection.close()
            
            urls = get_known_urls(db_path)
            self.assertEqual(urls, {"https://example.com/job1", "https://example.com/job2"})

    def test_get_pending_failures_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.db"
            failures = get_pending_failures(db_path)
            self.assertEqual(failures, [])

    def test_loop_discovery_with_mock(self):
        async def test_discovery():
            # Create a mock transport for httpx
            async def handler(request):
                if request.method == "GET":
                    # Return a simple HTML page with job links
                    html = '<a href="/jobs/1">Job 1</a><a href="/jobs/2">Job 2</a>'
                    return httpx.Response(200, text=html, request=request)
                return httpx.Response(404, request=request)
            
            config = LoopConfig(
                filter_source="hirify",
                feed_url="https://hirify.me/",
                limit=10,
                db_path=Path(tempfile.mkdtemp()) / "test.db",
                dry_run=True,
                quiet=True,
            )
            
            # Mock the fetch function
            import ralph.loop as loop_module
            original_fetch = loop_module.fetch_hirify_feed
            
            async def mock_fetch(feed_url, limit):
                return ["https://hirify.me/jobs/1", "https://hirify.me/jobs/2"]
            
            loop_module.fetch_hirify_feed = mock_fetch
            
            try:
                result = await run_loop(config)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["discovered"], 2)
                self.assertEqual(result["new"], 2)
                self.assertEqual(result["processed"], 2)
            finally:
                loop_module.fetch_hirify_feed = original_fetch
        
        asyncio.run(test_discovery())

    def test_loop_skips_known_urls(self):
        async def test_skip():
            with tempfile.TemporaryDirectory() as directory:
                db_path = Path(directory) / "test.db"
                
                # Pre-populate with a known URL
                import sqlite3
                connection = sqlite3.connect(db_path)
                try:
                    connection.execute(
                        "CREATE TABLE ralph_runs (id TEXT PRIMARY KEY, url TEXT NOT NULL, domain TEXT NOT NULL, score INTEGER NOT NULL, status TEXT NOT NULL, report_json TEXT NOT NULL, created_at TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO ralph_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("run1", "https://hirify.me/jobs/1", "hirify.me", 100, "passed", "{}", "2024-01-01"),
                    )
                    connection.commit()
                finally:
                    connection.close()
                
                config = LoopConfig(
                    filter_source="hirify",
                    feed_url="https://hirify.me/",
                    limit=10,
                    db_path=db_path,
                    dry_run=True,
                    quiet=True,
                )
                
                import ralph.loop as loop_module
                original_fetch = loop_module.fetch_hirify_feed
                
                async def mock_fetch(feed_url, limit):
                    return ["https://hirify.me/jobs/1", "https://hirify.me/jobs/2"]
                
                loop_module.fetch_hirify_feed = mock_fetch
                
                try:
                    result = await run_loop(config)
                    self.assertEqual(result["discovered"], 2)
                    self.assertEqual(result["known"], 1)
                    self.assertEqual(result["new"], 1)  # Only job 2 is new
                    self.assertEqual(result["processed"], 1)
                finally:
                    loop_module.fetch_hirify_feed = original_fetch
        
        asyncio.run(test_skip())


if __name__ == "__main__":
    unittest.main()

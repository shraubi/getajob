import os
import subprocess
import sys
import unittest


class TelegramAuthScriptTests(unittest.TestCase):
    def test_direct_script_can_import_repository_modules(self):
        env = os.environ.copy()
        env.update({"TELEGRAM_BOT_TOKEN": "test", "YOUR_CHAT_ID": "1"})
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import runpy; runpy.run_path('scripts/auth_telegram_sender.py', run_name='import_check')",
            ],
            cwd=".",
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

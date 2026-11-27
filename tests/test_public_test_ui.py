import unittest
from pathlib import Path

import main


ROOT = Path(__file__).resolve().parents[1]


class PublicTestUiTests(unittest.TestCase):
    def test_public_test_ui_uses_anonymous_auth_and_v2_contract(self):
        html = (ROOT / "test_ui.html").read_text(encoding="utf-8")

        for required in [
            "/auth/v1/signup",
            "Authorization:\"Bearer \"+current.access_token",
            "contract_version:2",
            "client_now:new Date().toISOString()",
            "current_location:",
            "confirmCreate",
            'api("/api/events"',
            "clearEvents",
        ]:
            self.assertIn(required, html)
        self.assertNotIn("demo-user-001", html)

    def test_public_test_ui_disables_browser_cache(self):
        coroutine = main.test_ui()
        try:
            coroutine.send(None)
        except StopIteration as completed:
            response = completed.value
        else:
            self.fail("test_ui unexpectedly suspended")

        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(Path(response.path).name, "test_ui.html")


if __name__ == "__main__":
    unittest.main()

"""test web"""

import json
import unittest

from module.web import get_flask_app


class WebTestCase(unittest.TestCase):
    def test_healthz(self):
        app = get_flask_app()
        app.config["TESTING"] = True
        app.config["LOGIN_DISABLED"] = True

        with app.test_client() as client:
            resp = client.get("/healthz")

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertIn("web", data)

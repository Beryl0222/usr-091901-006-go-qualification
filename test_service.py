"""验证基础服务与领域契约保持一致（/health、/contract、未知路由 404）。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

from api import api_handler
from service import build_service, health_payload, load_contract, SERVICE_ID


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = build_service(":memory:")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), api_handler(cls.service))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)

    def read_json(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            return json.load(response)

    def test_health_identity(self):
        self.assertEqual(self.read_json("/health"), health_payload())

    def test_contract_identity_and_rules(self):
        contract = self.read_json("/contract")
        self.assertEqual(contract, load_contract())
        self.assertEqual(contract["service_id"], SERVICE_ID)
        self.assertGreaterEqual(len(contract["invariants"]), 8)
        self.assertIn("仅追加", "".join(contract["invariants"]))

    def test_unknown_route_404(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_write_requires_identity_headers(self):
        import urllib.request
        req = urllib.request.Request(
            f"{self.base_url}/tournament",
            data=b'{"name":"x"}', method="POST",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()

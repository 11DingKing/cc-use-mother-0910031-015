"""HTTP 接口集成测试（真实起服务，随机端口，内存外的临时库）。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from growth_ledger.api import make_server


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        cls.httpd = make_server("127.0.0.1", 0, cls.tmp.name)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls._call("POST", "/rules", {
            "bands": [
                {"level": 1, "name": "见习", "min_points": 0,
                 "protection_days": 0, "benefits": []},
                {"level": 2, "name": "初级", "min_points": 30,
                 "protection_days": 30, "benefits": ["培训优先"]},
            ],
            "effective_from": "2026-01-01", "published_by": "admin"})
        cls._call("POST", "/guides", {"id": "G0", "name": "种子用户"})

    @classmethod
    def _call(cls, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{cls.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.store.close()
        Path(cls.tmp.name).unlink(missing_ok=True)

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        return self._call(method, path, payload)

    def test_full_lifecycle_over_http(self) -> None:
        self.assertEqual(self.call("POST", "/guides",
                                   {"id": "G1", "name": "小林"})[0], 200)
        self.assertEqual(self.call("POST", "/guides/G1/events", {
            "event_type": "SERVICE", "amount": 20,
            "occurred_on": "2026-01-05", "source_ref": "SVC-1"})[0], 200)
        self.assertEqual(self.call("POST", "/guides/G1/events", {
            "event_type": "RATING", "amount": 40,
            "occurred_on": "2026-01-06", "source_ref": "RATE-1"})[0], 200)

        status, run = self.call("POST", "/promotions/run", {"run_date": "2026-01-10"})
        self.assertEqual((status, run["promoted"], run["rerun"]), (200, 1, False))
        status, rerun = self.call("POST", "/promotions/run", {"run_date": "2026-01-10"})
        self.assertTrue(rerun["rerun"])

        status, events = self.call("GET", "/guides/G1/events")
        eid = next(e["id"] for e in events if e["source_ref"] == "RATE-1")
        status, review = self.call("POST", "/guides/G1/reviews", {
            "kind": "RATING_CORRECTION", "reason": "撤回", "opened_by": "ops",
            "event_id": eid,
            "payload": {"corrected_amount": 0, "effective_on": "2026-02-01"}})
        self.assertEqual(status, 200)
        status, decision = self.call(
            "POST", f"/reviews/{review['id']}/decision",
            {"approved": True, "decided_by": "manager"})
        self.assertEqual(decision["status"], "APPROVED")

        status, state = self.call("GET", "/guides/G1/evaluate?as_of=2026-02-01")
        self.assertEqual((state["level"], state["candidate_level"]), (2, 1))

        status, audit = self.call("GET", "/audit/conservation")
        self.assertEqual(status, 200)
        self.assertTrue(audit["balanced"])

    def test_errors_are_structured(self) -> None:
        status, err = self.call("GET", "/guides/NOPE/timeline")
        self.assertEqual(status, 404)
        self.assertIn("error", err)
        status, err = self.call("POST", "/guides", {"id": "G0", "name": "重复"})
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()

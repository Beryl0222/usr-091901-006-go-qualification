"""HTTP API 集成测试：鉴权、赛区隔离、幂等键、状态码、审计接口。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from api import api_handler
from service import build_service

ORG = {"X-Actor": "zu", "X-Role": "organizer"}
REF_E = {"X-Actor": "cai", "X-Role": "referee", "X-Station": "east"}
REF_W = {"X-Actor": "cai2", "X-Role": "referee", "X-Station": "west"}
ADM_E = {"X-Actor": "guan", "X-Role": "station_admin", "X-Station": "east"}
AUD = {"X-Actor": "shen", "X-Role": "auditor"}
JURY = {"X-Actor": "zhong", "X-Role": "jury"}
G = quote("名手组")


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.svc = build_service(":memory:")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api_handler(self.svc))
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = Request(self.base + path, data=data, method=method,
                      headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(req, timeout=3) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def boot(self):
        self.call("POST", "/tournament", {"name": "大赛"}, ORG)
        self.call("POST", "/stations", {"station_id": "east", "name": "华东"}, ORG)


class AuthTest(ApiTestBase):
    def test_missing_headers_401(self):
        s, b = self.call("POST", "/tournament", {"name": "x"})
        self.assertEqual((s, b["error"]), (401, "unauthorized"))

    def test_role_forbidden_403(self):
        s, b = self.call("POST", "/tournament", {"name": "x"}, REF_E)
        self.assertEqual((s, b["error"]), (403, "forbidden"))

    def test_station_isolation_403(self):
        self.boot()
        s, b = self.call("POST", f"/stations/east/groups/{G}/registrations",
                         {"name": "甲", "cert_type": "idcard", "cert_no": "110101199001010001"}, REF_W)
        self.assertEqual((s, b["error"]), (403, "forbidden"))

    def test_auditor_cannot_submit_results(self):
        self.boot()
        s, b = self.call("POST", f"/stations/east/groups/{G}/rules:freeze",
                         {"rounds": 1, "quota": 1}, AUD)
        self.assertEqual(s, 403)


class IdempotencyTest(ApiTestBase):
    def test_same_idempotency_key_returns_first_event(self):
        self.boot()
        body = {"name": "甲", "cert_type": "idcard", "cert_no": "110101199001010001"}
        h1 = {**REF_E, "Idempotency-Key": "reg-1"}
        s1, b1 = self.call("POST", f"/stations/east/groups/{G}/registrations", body, h1)
        s2, b2 = self.call("POST", f"/stations/east/groups/{G}/registrations",
                           {**body, "cert_no": "999999999999999999"}, h1)
        self.assertEqual(s1, s2, 201)
        self.assertEqual(b1["person_id"], b2["person_id"])
        self.assertEqual(b2["masked_cert"], b1["masked_cert"])
        s, lst = self.call("GET", f"/stations/east/groups/{G}/registrations", headers=REF_E)
        self.assertEqual(len(lst["registrations"]), 1)

    def test_redeliver_without_key_is_duplicate_fact_409(self):
        self.boot()
        self.call("POST", f"/stations/east/groups/{G}/registrations",
                  {"name": "甲", "cert_type": "idcard", "cert_no": "110101199001010001"}, REF_E)
        self.call("POST", f"/stations/east/groups/{G}/registrations",
                  {"name": "乙", "cert_type": "idcard", "cert_no": "110101199001010002"}, REF_E)
        self.call("POST", f"/stations/east/groups/{G}/rules:freeze",
                  {"rounds": 1, "quota": 1}, REF_E)
        self.call("POST", f"/stations/east/groups/{G}/rounds:freeze", {}, REF_E)
        self.call("POST", f"/stations/east/groups/{G}/rounds/1/pairings:freeze",
                  {"boards": [{"board": "B1", "white": self._pid(0), "black": self._pid(1)}]}, REF_E)
        payload = {"winner": "white", "record_hash": "sha256:1",
                   "signature_source": "signed:cai"}
        s1, _ = self.call("POST", f"/stations/east/groups/{G}/rounds/1/boards/B1/results",
                          payload, REF_E)
        self.assertEqual(s1, 201)
        s2, b2 = self.call("POST", f"/stations/east/groups/{G}/rounds/1/boards/B1/results",
                           payload, REF_E)
        self.assertEqual((s2, b2["error"]), (409, "duplicate_fact"))

    def _pid(self, idx):
        s, lst = self.call("GET", f"/stations/east/groups/{G}/registrations", headers=REF_E)
        return lst["registrations"][idx]["person_id"]


class AuditApiTest(ApiTestBase):
    def test_seal_unseal_leaves_audit_trail(self):
        self.boot()
        s, b = self.call("POST", f"/stations/east/groups/{G}/registrations",
                         {"name": "王伟", "cert_type": "idcard", "cert_no": "110101199001011234",
                          "phonetic": "wangwei"}, REF_E)
        self.assertEqual(s, 201)
        self.assertEqual(s, 201)
        reg = [e for e in self.svc.event_log("east", "名手组")
               if e["type"] == "player_registered"][0]
        seal_id = reg["payload"]["seal_id"]
        # 裁判不能拆封
        s, b = self.call("POST", f"/seals/{seal_id}/unseal", {"reason": "好奇"}, REF_E)
        self.assertEqual(s, 403)
        s, b = self.call("POST", f"/seals/{seal_id}/unseal",
                         {"reason": "总决赛资格复核"}, AUD)
        self.assertEqual(s, 200)
        self.assertEqual(b["identity"]["cert_no"], "110101199001011234")
        s, log = self.call("GET", "/audit/seals", headers=AUD)
        self.assertEqual(log["entries"][0]["reason"], "总决赛资格复核")

    def test_events_and_verify_endpoints(self):
        self.boot()
        s, v = self.call("GET", "/verify", headers=AUD)
        self.assertTrue(v["ok"])
        s, evs = self.call("GET", "/events?" + urlencode({"station": "east"}), headers=ORG)
        self.assertTrue(all(e["payload"].get("station_id") == "east" for e in evs["events"]))


class FullHttpFlowTest(ApiTestBase):
    def test_play_confirm_explain_lock_flow(self):
        self.boot()
        # 第二赛区稍后使用
        # 报名 4 人
        pids = []
        for i in range(4):
            s, b = self.call("POST", f"/stations/east/groups/{G}/registrations",
                             {"name": f"棋{i}", "cert_type": "idcard",
                              "cert_no": f"11010119900101000{i+1}", "phonetic": f"q{i}"},
                             {**REF_E, "Idempotency-Key": f"r{i}"})
            self.assertEqual(s, 201, b)
            pids.append(b["person_id"])
        self.call("POST", f"/stations/east/groups/{G}/rules:freeze",
                  {"rounds": 2, "quota": 1}, REF_E)
        self.call("POST", f"/stations/east/groups/{G}/rounds:freeze", {}, REF_E)
        # R1: p0 对 p1、p2 对 p3；R2: p0 对 p2、p1 对 p3（显式冻结，颜色确定）
        self.call("POST", f"/stations/east/groups/{G}/rounds/1/pairings:freeze",
                  {"boards": [{"board": "B1", "white": pids[0], "black": pids[1]},
                              {"board": "B2", "white": pids[2], "black": pids[3]}]}, REF_E)
        self.call("POST", f"/stations/east/groups/{G}/rounds/2/pairings:freeze",
                  {"boards": [{"board": "B1", "white": pids[0], "black": pids[2]},
                              {"board": "B2", "white": pids[1], "black": pids[3]}]}, REF_E)
        # 原判：p0 连胜（R1 胜 p1、R2 胜 p2），p3 R2 胜 p1；p0 为冠军
        win_payload = {
            "record_hash": "sha256:r", "signature_source": "signed:裁判cai"}
        self.call("POST", f"/stations/east/groups/{G}/rounds/1/boards/B1/results",
                  {"winner": "white", **win_payload}, REF_E)
        self.call("POST", f"/stations/east/groups/{G}/rounds/1/boards/B2/results",
                  {"winner": "white", **win_payload}, REF_E)
        self.call("POST", f"/stations/east/groups/{G}/rounds/2/boards/B1/results",
                  {"winner": "white", **win_payload}, REF_E)
        self.call("POST", f"/stations/east/groups/{G}/rounds/2/boards/B2/results",
                  {"winner": "black", **win_payload}, REF_E)
        # 裁判不能确认，赛区管理员可以
        s, b = self.call("POST", f"/stations/east/groups/{G}/confirmation", headers=REF_E)
        self.assertEqual(s, 403)
        s, conf = self.call("POST", f"/stations/east/groups/{G}/confirmation", headers=ADM_E)
        self.assertEqual(s, 201)
        # 第七轮更正（这里用第一轮演示）
        s, cor = self.call("POST",
                           f"/stations/east/groups/{G}/rounds/1/boards/B1/results:correct",
                           {"winner": "black", "reason": "签字表复核更正",
                            "record_hash": "sha256:new", "signature_source": "paper:v2"}, REF_E)
        self.assertEqual(s, 201)
        group_events = self.svc.event_log("east", "名手组")
        r1b1 = [e for e in group_events
                if e["type"] == "result_submitted" and e["payload"]["board"] == "B1"
                and e["payload"]["round_no"] == 1][0]
        self.assertEqual(cor["payload"]["old_event_id"], r1b1["event_id"])
        # 解释
        s, st = self.call("GET", f"/stations/east/groups/{G}/standings", headers=ORG)
        top = st["rows"][0]
        s, ex = self.call("GET",
                          f"/stations/east/groups/{G}/players/{top['player_id']}/explanation",
                          headers=ORG)
        self.assertEqual(s, 200)
        self.assertIn("rule_version", ex)
        self.assertTrue(any(c["decides"] for c in ex["tiebreak_path"]))
        # 通知
        s, notes = self.call("GET", "/notifications?station=east", headers=ORG)
        kinds = {n["kind"] for n in notes["notifications"]}
        self.assertIn("promoted", kinds)
        # 申诉流程（对第二轮 B1 提起，维持原判）
        s, ap = self.call("POST",
                          f"/stations/east/groups/{G}/rounds/2/boards/B1/appeals",
                          {"claim": "申诉"}, JURY)
        self.assertEqual(s, 201)
        s, dec = self.call("POST",
                           f"/stations/east/groups/{G}/appeals/{ap['payload']['appeal_id']}/decision",
                           {"decision": "uphold", "reason": "维持", "panel": "仲裁组"}, JURY)
        self.assertEqual(s, 201)


if __name__ == "__main__":
    unittest.main()

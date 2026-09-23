"""赛事资格与核算 HTTP API。

鉴权（演示用请求头，生产应替换为网关签名）：
- X-Actor  操作者标识（写入事件 actor，留痕）
- X-Role   organizer(组委会) / station_admin(赛区管理员) / referee(裁判) /
            jury(仲裁) / auditor(资格审核员)
- X-Station 赛区角色的所属赛区；操作其他赛区一律 403

写接口支持 Idempotency-Key：同一键重放返回首次事件，不产生第二条事实。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

from tournament.domain import TournamentService, DomainError

STATION_RE = r"(?P<station>[A-Za-z0-9_\-]+)"
GROUP_RE = r"(?P<group>[^/]+)"
ROUND_RE = r"(?P<round>\d+)"
BOARD_RE = r"(?P<board>[^/]+)"
PERSON_RE = r"(?P<pid>[A-Za-z0-9_\-]+)"

# code → HTTP 状态
STATUS = {
    "bad_result": 400, "bad_rules": 400, "bad_schedule": 400, "bad_round": 400,
    "bad_board": 400, "bad_decision": 400, "bad_merge": 400, "bad_merge2": 400,
    "missing_signature": 422, "missing_record_hash": 422,
    "no_players": 400, "invalid_json": 400,
    "not_found": 404, "not_initialized": 404, "not_locked": 404,
    "unauthorized": 401, "forbidden": 403,
}


def api_handler(service: TournamentService):
    class ApiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---- 基础设施 ----
        def log_message(self, *_a):
            return

        def _send(self, code: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_empty(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise DomainError("invalid_json", "请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise DomainError("invalid_json", "请求体必须是 JSON 对象")
            return data

        def _actor(self, roles, *, station_bound: str | None = None):
            actor = self.headers.get("X-Actor")
            role = self.headers.get("X-Role")
            if not actor or not role:
                raise DomainError("unauthorized", "缺少 X-Actor / X-Role")
            if role not in roles:
                raise DomainError("forbidden", f"角色 {role} 无权执行该操作，需要 {sorted(roles)}")
            if station_bound is not None and role in ("station_admin", "referee"):
                bound = self.headers.get("X-Station")
                if bound != station_bound:
                    raise DomainError("forbidden",
                                      f"赛区角色只能操作所属赛区（X-Station={station_bound}）")
            return f"{role}:{actor}"

        def _idem(self) -> str | None:
            return self.headers.get("Idempotency-Key")

        def _handle(self, method: str):
            from service import health_payload, load_contract
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            try:
                if method == "GET" and path == "/health":
                    return self._send(200, health_payload())
                if method == "GET" and path == "/contract":
                    return self._send(200, load_contract())
                if method not in ("GET", "POST"):
                    return self._send_empty(405)
                return self._route(method, path, query)
            except DomainError as e:
                return self._send(STATUS.get(e.code, 409), {"error": e.code, "message": e.message})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        # ---- 路由 ----
        def _route(self, method, path, query):
            svc = service
            S = service  # 简写

            def match(pattern):
                m = re.fullmatch(pattern, path)
                return m.groupdict() if m else None

            # 赛事 / 赛区
            if path == "/tournament" and method == "POST":
                actor = self._actor({"organizer"})
                d = self._read_json()
                ev = svc.create_tournament(d.get("name", "全国业余围棋大赛"), actor=actor)
                return self._send(201, ev)
            if path == "/stations" and method == "POST":
                actor = self._actor({"organizer"})
                d = self._read_json()
                ev = svc.open_station(d["station_id"], d["name"], actor=actor)
                return self._send(201, ev)
            if path == "/stations" and method == "GET":
                self._actor({"organizer", "station_admin", "referee", "auditor", "jury"})
                return self._send(200, {"stations": list(S._load().stations.values())})

            # 身份审核
            if path == "/review/flags" and method == "GET":
                self._actor({"auditor", "organizer"})
                include = query.get("include_closed", ["0"])[0] in ("1", "true")
                return self._send(200, {"flags": svc.review_flags(include_closed=include)})
            if path == "/identity/persons" and method == "GET":
                self._actor({"auditor"})
                return self._send(200, {"persons": svc.person_index()})
            g = match(rf"/review/flags/{PERSON_RE}/resolve")
            if g and method == "POST":
                actor = self._actor({"auditor"})
                d = self._read_json()
                ev = svc.resolve_similar(g["pid"], d["decision"], actor=actor)
                return self._send(201, ev)
            if path == "/identity/merge" and method == "POST":
                actor = self._actor({"auditor"})
                d = self._read_json()
                ev = svc.merge_persons(d["from_person"], d["into_person"],
                                       d.get("reason", ""), actor=actor)
                return self._send(201, ev)
            g = match(rf"/seals/{PERSON_RE}/unseal")
            if g and method == "POST":
                actor = self._actor({"auditor"})
                d = self._read_json()
                plain = svc.unseal(g["pid"], actor=actor, reason=d.get("reason", ""))
                return self._send(200, {"seal_id": g["pid"], "identity": plain})
            if path == "/audit/seals" and method == "GET":
                self._actor({"auditor", "organizer"})
                return self._send(200, {"entries": svc.seal_audit_log()})

            # 选手轨迹（跨站最小化视图）
            g = match(rf"/persons/{PERSON_RE}/trace")
            if g and method == "GET":
                viewer = query.get("viewer_station", [None])[0]
                # 任何登录角色都可查；跨站字段由领域层裁剪
                self._actor({"organizer", "station_admin", "referee", "auditor", "jury"})
                return self._send(200, svc.trace(g["pid"], viewer_station=viewer))

            # 通知
            if path == "/notifications" and method == "GET":
                self._actor({"organizer", "station_admin", "referee", "auditor"})
                station = query.get("station", [None])[0]
                return self._send(200, {"notifications": svc.notifications(station)})

            # 事件日志 / 链校验
            if path == "/events" and method == "GET":
                self._actor({"auditor", "organizer"})
                station = query.get("station", [None])[0]
                group = query.get("group", [None])[0]
                return self._send(200, {"events": svc.event_log(station, group)})
            if path == "/verify" and method == "GET":
                self._actor({"auditor", "organizer"})
                return self._send(200, svc.verify())

            # 总决赛
            if path == "/finals/lock" and method == "POST":
                actor = self._actor({"organizer"})
                d = self._read_json()
                ev = svc.lock_finals(d.get("name", "总决赛"), actor=actor)
                return self._send(201, ev)
            if path == "/finals" and method == "GET":
                self._actor({"organizer", "station_admin", "referee", "auditor", "jury"})
                return self._send(200, svc.finals_roster())

            # ---- 赛区/组别资源 ----
            base = rf"/stations/{STATION_RE}/groups/{GROUP_RE}"

            g = match(rf"{base}$")
            if g and method == "GET":
                self._actor({"organizer", "station_admin", "referee", "auditor", "jury"},
                            station_bound=g["station"])
                gs = S._load().groups.get((g["station"], g["group"]))
                if gs is None:
                    raise DomainError("not_found", "赛区/组别不存在")
                return self._send(200, {
                    "station_id": gs.station_id, "group_id": gs.group_id,
                    "rule_version": gs.rule_version, "rules": gs.rules,
                    "rounds": gs.rules["rounds"] if gs.rules else None,
                    "confirmed": gs.confirmed, "finals_locked": gs.finals_locked,
                    "players": len(gs.registrations),
                })

            g = match(rf"{base}/registrations$")
            if g:
                sid, gid = g["station"], g["group"]
                if method == "POST":
                    actor = self._actor({"referee", "station_admin"}, station_bound=sid)
                    d = self._read_json()
                    ev = svc.register(
                        sid, gid, name=d["name"], cert_type=d["cert_type"], cert_no=d["cert_no"],
                        phonetic=d.get("phonetic"), birth_year=d.get("birth_year"),
                        actor=actor, request_key=self._idem(),
                    )
                    return self._send(201, _public_registration(ev))
                actor = self._actor({"referee", "station_admin", "auditor"}, station_bound=sid)
                gs = S._group(S._load(), sid, gid)
                return self._send(200, {"registrations": [
                    {"person_id": r.person_id, "name": r.name,
                     "cert_type": r.cert_type, "masked_cert": r.masked_cert,
                     "withdrawn_from": r.withdrawn_from}
                    for r in gs.registrations.values()
                ]})

            g = match(rf"{base}/rules:freeze$")
            if g and method == "POST":
                sid, gid = g["station"], g["group"]
                actor = self._actor({"referee", "station_admin"}, station_bound=sid)
                ev = svc.freeze_rules(sid, gid, self._read_json(), actor=actor)
                return self._send(201, ev)

            g = match(rf"{base}/rounds:freeze$")
            if g and method == "POST":
                sid, gid = g["station"], g["group"]
                actor = self._actor({"referee", "station_admin"}, station_bound=sid)
                d = self._read_json()
                ev = svc.freeze_rounds(sid, gid, d.get("schedule"), actor=actor)
                return self._send(201, ev)

            g = match(rf"{base}/rounds/{ROUND_RE}/pairings/next$")
            if g and method == "GET":
                sid, gid, rnd = g["station"], g["group"], int(g["round"])
                self._actor({"referee", "station_admin"}, station_bound=sid)
                return self._send(200, {"boards": svc.suggest_next_pairings(sid, gid, rnd)})

            g = match(rf"{base}/rounds/{ROUND_RE}/pairings:freeze$")
            if g and method == "POST":
                sid, gid, rnd = g["station"], g["group"], int(g["round"])
                actor = self._actor({"referee", "station_admin"}, station_bound=sid)
                d = self._read_json()
                ev = svc.freeze_pairings(sid, gid, rnd, d["boards"], actor=actor)
                return self._send(201, ev)

            g = match(rf"{base}/rounds/{ROUND_RE}/boards/{BOARD_RE}/results$")
            if g and method == "POST":
                sid, gid = g["station"], g["group"]
                rnd, board = int(g["round"]), g["board"]
                actor = self._actor({"referee", "station_admin"}, station_bound=sid)
                d = self._read_json()
                late = bool(d.get("late", False))
                ev = svc.submit_result(
                    sid, gid, rnd, board, d["winner"],
                    record_hash=d.get("record_hash"),
                    signature_source=d.get("signature_source"),
                    late=late, actor=actor, request_key=self._idem(),
                )
                return self._send(201, ev)

            g = match(rf"{base}/rounds/{ROUND_RE}/boards/{BOARD_RE}/forfeits$")
            if g and method == "POST":
                sid, gid = g["station"], g["group"]
                rnd, board = int(g["round"]), g["board"]
                actor = self._actor({"referee", "station_admin"}, station_bound=sid)
                d = self._read_json()
                ev = svc.submit_forfeit(
                    sid, gid, rnd, board, d["winner"], d.get("reason", ""),
                    signature_source=d.get("signature_source"),
                    actor=actor, request_key=self._idem(),
                )
                return self._send(201, ev)

            g = match(rf"{base}/rounds/{ROUND_RE}/boards/{BOARD_RE}/results:correct$")
            if g and method == "POST":
                sid, gid = g["station"], g["group"]
                rnd, board = int(g["round"]), g["board"]
                actor = self._actor({"referee", "station_admin"}, station_bound=sid)
                d = self._read_json()
                ev = svc.correct_result(
                    sid, gid, rnd, board, d["winner"], d.get("reason", ""),
                    record_hash=d.get("record_hash"),
                    signature_source=d.get("signature_source"), actor=actor,
                )
                return self._send(201, ev)

            g = match(rf"{base}/rounds/{ROUND_RE}/boards/{BOARD_RE}/appeals$")
            if g and method == "POST":
                sid, gid = g["station"], g["group"]
                rnd, board = int(g["round"]), g["board"]
                actor = self._actor({"referee", "station_admin", "jury"}, station_bound=sid)
                d = self._read_json()
                ev = svc.file_appeal(sid, gid, rnd, board, d["claim"], actor=actor)
                return self._send(201, ev)

            g = match(rf"{base}/appeals/{PERSON_RE}/decision$")
            if g and method == "POST":
                sid, gid, appeal_id = g["station"], g["group"], g["pid"]
                actor = self._actor({"jury", "organizer"})
                d = self._read_json()
                ev = svc.decide_appeal(
                    sid, gid, appeal_id, d["decision"], d.get("winner"),
                    d.get("reason", ""), panel=d.get("panel", actor),
                    record_hash=d.get("record_hash"),
                    signature_source=d.get("signature_source"), actor=actor,
                )
                return self._send(201, ev)

            g = match(rf"{base}/players/{PERSON_RE}/withdrawal$")
            if g and method == "POST":
                sid, gid, pid = g["station"], g["group"], g["pid"]
                actor = self._actor({"referee", "station_admin"}, station_bound=sid)
                d = self._read_json()
                ev = svc.withdraw(sid, gid, pid, int(d["from_round"]),
                                  d.get("reason", ""), actor=actor)
                return self._send(201, ev)

            g = match(rf"{base}/confirmation$")
            if g and method == "POST":
                sid, gid = g["station"], g["group"]
                actor = self._actor({"station_admin", "organizer"}, station_bound=sid)
                ev = svc.confirm_group(sid, gid, actor=actor)
                return self._send(201, ev)

            g = match(rf"{base}/reconfirmation$")
            if g and method == "POST":
                sid, gid = g["station"], g["group"]
                actor = self._actor({"organizer"})
                ev = svc.reconfirm_group(sid, gid, actor=actor)
                return self._send(201, ev)

            g = match(rf"{base}/standings$")
            if g and method == "GET":
                sid, gid = g["station"], g["group"]
                self._actor({"organizer", "station_admin", "referee", "auditor", "jury"})
                return self._send(200, svc.standings(sid, gid))

            g = match(rf"{base}/players/{PERSON_RE}/explanation$")
            if g and method == "GET":
                sid, gid, pid = g["station"], g["group"], g["pid"]
                self._actor({"organizer", "station_admin", "referee", "auditor", "jury"})
                return self._send(200, svc.explain_rank(sid, gid, pid))

            return self._send_empty(404)

    return ApiHandler


def _public_registration(ev: dict) -> dict:
    """报名响应只回脱敏身份。"""
    p = ev["payload"]
    return {
        "event_id": ev["event_id"], "person_id": p["person_id"],
        "station_id": p["station_id"], "group_id": p["group_id"],
        "name": p["name"], "cert_type": p["cert_type"],
        "masked_cert": p["masked_cert"],
    }

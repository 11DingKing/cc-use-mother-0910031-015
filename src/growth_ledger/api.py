"""HTTP 接口（标准库实现，零外部依赖）。

所有响应均为 JSON；写接口要求 Content-Type: application/json。
日期字段统一 ISO 格式 YYYY-MM-DD。
"""
from __future__ import annotations

import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .errors import DomainError
from .service import GrowthService
from .store import Store, _default


def _parse_date(value: str | None, field: str = "date") -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise DomainError(f"{field} 必须是 YYYY-MM-DD 格式")


class GrowthHandler(BaseHTTPRequestHandler):
    service: GrowthService  # 由 make_server 注入到类上

    server_version = "GrowthLedger/1.0"

    # ---------- 基础 ----------

    def _send(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2,
                          default=_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise DomainError("请求体不是合法 JSON")
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlsplit(self.path).query)
        return {k: v[-1] for k, v in parsed.items()}

    def log_message(self, fmt: str, *args: object) -> None:  # 静默
        return

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            q = self._query()
            body = self._body() if method == "POST" else {}
            svc = self.service
            gid = r"[^/]+"

            routes = [
                ("GET", r"^/rules$", lambda: _send_rules(svc)),
                ("POST", r"^/rules$", lambda: _publish_rule(svc, body)),
                ("GET", r"^/guides$", lambda: svc.list_guides()),
                ("POST", r"^/guides$", lambda: _register(svc, body)),
                ("GET", r"^/audit/conservation$", lambda: svc.conservation_report()),
                ("GET", r"^/reviews$", lambda: svc.list_reviews(q.get("guide_id"))),
                ("POST", r"^/reviews$", lambda: _open_review(svc, body)),
                ("POST", r"^/promotions/run$",
                 lambda: _run_promotions(svc, body, q)),
                ("POST", r"^/reviews/([^/]+)/decision$",
                 lambda rid: _decide(svc, rid, body)),
                ("GET", rf"^/guides/({gid})/timeline$",
                 lambda g: svc.timeline(g)),
                ("GET", rf"^/guides/({gid})/evaluate$",
                 lambda g: svc.evaluate(g, _parse_date(q.get("as_of"), "as_of"))),
                ("GET", rf"^/guides/({gid})/snapshots$",
                 lambda g: svc.list_snapshots(g)),
                ("GET", rf"^/guides/({gid})/events$",
                 lambda g: svc.list_events(g)),
                ("POST", rf"^/guides/({gid})/events$",
                 lambda g: _post_event(svc, g, body)),
                ("GET", rf"^/guides/({gid})/entries$",
                 lambda g: svc.list_entries(g)),
                ("POST", rf"^/guides/({gid})/reviews$",
                 lambda g: _open_review(svc, {**body, "guide_id": g})),
            ]
            for verb, pattern, handler in routes:
                if verb != method:
                    continue
                match = re.match(pattern + r"$", path)
                if match:
                    result = handler(*match.groups())
                    self._send(200, result)
                    return
            self._send(404, {"error": "not_found", "message": f"无此路由：{method} {path}"})
        except DomainError as exc:
            self._send(exc.status, {"error": exc.code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal", "message": repr(exc)})


# ---------- 请求适配 ----------

def _require(body: dict, key: str) -> object:
    if key not in body:
        raise DomainError(f"缺少字段：{key}")
    return body[key]


def _send_rules(svc: GrowthService) -> dict:
    return {"rules": svc.list_rules()}


def _publish_rule(svc: GrowthService, body: dict) -> dict:
    return svc.publish_rule(
        bands=_require(body, "bands"),
        effective_from=_parse_date(_require(body, "effective_from"), "effective_from"),  # type: ignore[arg-type]
        published_by=body.get("published_by", "anonymous"),
        note=body.get("note", ""),
        version=body.get("version"),
    )


def _register(svc: GrowthService, body: dict) -> dict:
    return svc.register_guide(_require(body, "id"), _require(body, "name"))


def _post_event(svc: GrowthService, guide_id: str, body: dict) -> dict:
    return svc.post_event(
        guide_id,
        event_type=str(_require(body, "event_type")),
        amount=int(_require(body, "amount")),
        occurred_on=_parse_date(body.get("occurred_on"), "occurred_on"),
        source_ref=body.get("source_ref"),
        note=body.get("note", ""),
        idempotency_key=body.get("idempotency_key"),
    )


def _open_review(svc: GrowthService, body: dict) -> dict:
    return svc.open_review(
        guide_id=str(_require(body, "guide_id")),
        kind=str(_require(body, "kind")),
        reason=str(body.get("reason", "")),
        opened_by=body.get("opened_by", "anonymous"),
        event_id=body.get("event_id"),
        payload=body.get("payload"),
    )


def _decide(svc: GrowthService, review_id: str, body: dict) -> dict:
    return svc.decide_review(
        review_id,
        approved=bool(_require(body, "approved")),
        decided_by=body.get("decided_by", "anonymous"),
        decision_note=body.get("decision_note", ""),
    )


def _run_promotions(svc: GrowthService, body: dict, query: dict) -> dict:
    rule_version = body.get("rule_version") or query.get("rule_version")
    return svc.run_promotions(
        run_date=_parse_date(body.get("run_date") or query.get("run_date"), "run_date"),
        rule_version=int(rule_version) if rule_version is not None else None,
    )


def make_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    store = Store(db_path)
    service = GrowthService(store)

    class _Handler(GrowthHandler):
        pass

    _Handler.service = service
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.store = store  # type: ignore[attr-defined]
    return httpd

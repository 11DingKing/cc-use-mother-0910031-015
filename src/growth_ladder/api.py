"""HTTP 接口（仅依赖标准库 ``http.server``）。

每个升降级与权益变化都能通过 ``/evaluations/{id}`` 追溯到：
触发方式、指标快照、规则版本、逐条理由（reasons）与权益效果（effects），
权益记录还带有授予/收回评定单 id，形成双向来源链。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import GrowthError
from .service import GrowthService


def _make_handler(service: GrowthService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "GrowthLadder/1.0"

        def log_message(self, fmt, *args):  # 安静输出，便于测试与脚本调用
            return

        # ------------------------------------------------------------ plumbing
        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise GrowthError("请求体不是合法 JSON", code="invalid_json", status=400)
            if not isinstance(data, dict):
                raise GrowthError("请求体必须是 JSON 对象", code="invalid_json", status=400)
            return data

        def _query_as_of(self) -> str | None:
            qs = parse_qs(urlparse(self.path).query)
            return qs.get("as_of", [None])[0]

        def _handle(self, method: str):
            try:
                # 所有数据库访问经同一把连接锁串行化（含只读请求）。
                with service.store.lock:
                    self._route(method)
            except GrowthError as exc:
                self._send(exc.status, exc.to_dto())
            except (KeyError, ValueError, TypeError) as exc:
                self._send(400, {"error": "validation_error", "message": f"参数错误：{exc}"})
            except Exception as exc:  # pragma: no cover - 兜底
                self._send(500, {"error": "internal_error", "message": str(exc)})

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        # -------------------------------------------------------------- routes
        def _route(self, method: str) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            body = self._read_json() if method == "POST" else {}
            svc = service
            gid = r"(\d+)"

            # 系统
            if method == "GET" and path == "/health":
                return self._send(200, {"status": "ok"})
            if method == "GET" and path == "/integrity":
                return self._send(200, svc.integrity())
            if method == "GET" and path == "/runs":
                return self._send(200, {"runs": svc.runs()})
            if method == "POST" and path == "/cycles/run":
                return self._send(201, svc.run_cycle(body))

            # 规则版本
            if method == "GET" and path == "/rules":
                return self._send(200, {"versions": svc.list_rule_versions()})
            if method == "POST" and path == "/rules":
                return self._send(201, svc.publish_rule(body))

            # 讲解员
            if method == "GET" and path == "/guides":
                return self._send(200, {"guides": svc.list_guides()})
            if method == "POST" and path == "/guides":
                return self._send(201, svc.create_guide(body["guide_no"], body["name"]))

            if m := re.fullmatch(rf"/guides/{gid}", path):
                if method == "GET":
                    return self._send(200, svc.get_guide(int(m.group(1))))
            if m := re.fullmatch(rf"/guides/{gid}/explain", path):
                if method == "GET":
                    return self._send(200, svc.explain(int(m.group(1))))
            if m := re.fullmatch(rf"/guides/{gid}/balance", path):
                if method == "GET":
                    return self._send(200, svc.balance(int(m.group(1)), self._query_as_of()))
            if m := re.fullmatch(rf"/guides/{gid}/metrics", path):
                if method == "GET":
                    return self._send(200, svc.metrics(int(m.group(1)), self._query_as_of()))
            if m := re.fullmatch(rf"/guides/{gid}/statement", path):
                if method == "GET":
                    return self._send(200, svc.statement(int(m.group(1)), self._query_as_of()))
            if m := re.fullmatch(rf"/guides/{gid}/snapshots", path):
                if method == "GET":
                    return self._send(200, {"snapshots": svc.snapshots(int(m.group(1)))})
                if method == "POST":
                    return self._send(201, svc.take_snapshot(int(m.group(1)), body))
            if m := re.fullmatch(rf"/guides/{gid}/snapshots/verify", path):
                if method == "GET":
                    return self._send(200, svc.verify_snapshot_chain(int(m.group(1))))
            if m := re.fullmatch(rf"/guides/{gid}/evaluations", path):
                if method == "GET":
                    return self._send(200, {"evaluations": svc.evaluations(int(m.group(1)))})
                if method == "POST":
                    return self._send(201, svc.evaluate(int(m.group(1)), body))
            if (m := re.fullmatch(rf"/guides/{gid}/assignments", path)) and method == "GET":
                return self._send(200, {"assignments": svc.assignments(int(m.group(1)))})
            if (m := re.fullmatch(rf"/guides/{gid}/benefits", path)) and method == "GET":
                return self._send(200, {"grants": svc.grants(int(m.group(1)))})
            if (m := re.fullmatch(rf"/guides/{gid}/services", path)) and method == "POST":
                return self._send(201, svc.record_service(int(m.group(1)), body))
            if (m := re.fullmatch(rf"/guides/{gid}/reviews", path)) and method == "POST":
                return self._send(201, svc.record_review(int(m.group(1)), body))
            if (m := re.fullmatch(rf"/guides/{gid}/adjustments", path)) and method == "POST":
                return self._send(201, svc.manual_adjust(int(m.group(1)), body))
            if m := re.fullmatch(rf"/guides/{gid}/holds", path):
                if method == "POST":
                    return self._send(201, svc.request_hold(int(m.group(1)), body))
                if method == "GET":
                    return self._send(200, {"holds": svc.list_holds(body.get("status") if body else None)})

            # 评价/场次的撤回与更正（以源 uid 寻址）
            if (m := re.fullmatch(r"/reviews/([^/]+)/withdraw", path)) and method == "POST":
                return self._send(201, svc.withdraw_review(m.group(1), body))
            if (m := re.fullmatch(r"/reviews/([^/]+)/correct", path)) and method == "POST":
                return self._send(201, svc.correct_review(m.group(1), body))
            if (m := re.fullmatch(r"/sessions/([^/]+)/cancel", path)) and method == "POST":
                return self._send(201, svc.cancel_session(m.group(1), body))

            # 评定单与复核单
            if (m := re.fullmatch(r"/evaluations/(\d+)", path)) and method == "GET":
                return self._send(200, svc.evaluation_detail(int(m.group(1))))
            if method == "GET" and path == "/holds":
                status = parse_qs(urlparse(self.path).query).get("status", [None])[0]
                return self._send(200, {"holds": svc.list_holds(status)})
            if (m := re.fullmatch(r"/holds/(\d+)/resolve", path)) and method == "POST":
                return self._send(201, svc.resolve_hold(int(m.group(1)), body))

            self._send(404, {"error": "not_found", "message": f"无此接口：{method} {path}"})

    return Handler


def create_server(host: str = "127.0.0.1", port: int = 8080,
                  db_path: str = ":memory:") -> ThreadingHTTPServer:
    service = GrowthService(db_path)
    handler = _make_handler(service)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.service = service  # 暴露给调用方做数据准备/关闭
    return httpd


def main(argv: list[str] | None = None) -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="讲解员成长等级后端 HTTP 服务")
    parser.add_argument("--host", default=os.environ.get("GROWTH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GROWTH_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("GROWTH_DB", "growth.db"))
    args = parser.parse_args(argv)

    httpd = create_server(args.host, args.port, args.db)
    print(f"成长等级服务已启动：http://{args.host}:{args.port}  (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.service.close()
        httpd.server_close()


if __name__ == "__main__":
    main()

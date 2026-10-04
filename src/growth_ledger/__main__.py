"""命令行入口：python -m growth_ledger [--host 127.0.0.1] [--port 8080] [--db growth.db]"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from growth_ledger.api import make_server


def main() -> None:
    parser = argparse.ArgumentParser(description="讲解员成长等级后端")
    parser.add_argument("--host", default=os.environ.get("GROWTH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("GROWTH_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("GROWTH_DB", "growth.db"))
    args = parser.parse_args()
    httpd = make_server(args.host, args.port, args.db)
    print(f"成长等级服务已启动：http://{args.host}:{args.port}  数据库：{args.db}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.store.close()


if __name__ == "__main__":
    main()

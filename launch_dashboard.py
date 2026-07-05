from __future__ import annotations

import argparse
import json
import os

from build_dashboard import HTML_TEMPLATE, build_payload, write_output_assets
from quant_guardian import DEFAULT_CONFIG, load_config, resolve_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Quant Guardian QG-Core 대시보드 생성 및 열기")
    parser.add_argument("--refresh", action="store_true", help="무료 가격 데이터를 새로 받아 생성")
    parser.add_argument("--no-open", action="store_true", help="파일만 만들고 브라우저는 열지 않기")
    args = parser.parse_args()

    cfg = load_config(DEFAULT_CONFIG)
    paths = resolve_paths(cfg)
    print("QG-Core 대시보드를 생성하는 중입니다...")
    payload = build_payload(refresh=args.refresh)
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False, allow_nan=False))
    out = paths.output / "dashboard.html"
    out.write_text(html, encoding="utf-8-sig")
    write_output_assets(paths, payload)
    print(f"생성 완료: {out}")

    if not args.no_open:
        if os.name == "nt":
            os.startfile(out)  # type: ignore[attr-defined]
        else:
            import webbrowser

            webbrowser.open(out.resolve().as_uri())
        print("브라우저로 대시보드를 열었습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

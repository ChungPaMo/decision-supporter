"""CLI: python -m engine.forecast --input req.json --output res.json"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engine.forecast import run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m engine.forecast", description="월별 수요예측 (P50/P80/P90)")
    ap.add_argument("--input", required=True, type=Path, help="forecast-input-0.1-draft JSON")
    ap.add_argument("--output", type=Path, help="결과 JSON 경로 (없으면 표준 출력)")
    args = ap.parse_args(argv)

    result = run(json.loads(args.input.read_text(encoding="utf-8")))
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print(f"status={result['status']} forecasts={len(result['forecasts'])} skipped={len(result['skipped'])}",
          file=sys.stderr)
    return 0 if result["status"] in ("OK", "PARTIAL") else 1


if __name__ == "__main__":
    raise SystemExit(main())

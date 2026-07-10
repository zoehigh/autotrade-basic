import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

_OUTPUT_DIR = Path(__file__).parent.parent / "output"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def dump_json(data: dict, prefix: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = _OUTPUT_DIR / f"{prefix}_{ts}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def print_separator(title: str = ""):
    print()
    print("=" * 70)
    if title:
        print(title)
        print("=" * 70)

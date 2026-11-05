"""从 app.contract 的 Pydantic 模型导出 JSON Schema。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.contract import (  # noqa: E402
    Capabilities,
    PolicyDocument,
    UsageBatch,
    UsageEvent,
)

SCHEMAS = {
    "capabilities.schema.json": Capabilities,
    "policy.schema.json": PolicyDocument,
    "usage-event.schema.json": UsageEvent,
    "usage-batch.schema.json": UsageBatch,
}


def main() -> int:
    target = ROOT / "schemas"
    target.mkdir(exist_ok=True)
    for name, model in SCHEMAS.items():
        schema = model.model_json_schema(by_alias=True)
        schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
        schema["$id"] = f"https://tongtian.local/schemas/{name}"
        path = target / name
        path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote schemas/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

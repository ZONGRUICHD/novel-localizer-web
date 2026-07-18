from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "src"))

from shiori.app import create_app  # noqa: E402
from shiori.config import Settings  # noqa: E402


def main() -> None:
    output = Path(__file__).resolve().parents[2] / "openapi" / "openapi.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        environment="test",
        auth_mode="test",
        database_url="sqlite://",
        storage_root=Path(os.devnull).parent,
        public_origin="https://translate.example.invalid",
        owner_email="owner@example.invalid",
        csrf_secret="openapi-generation-secret-32-bytes-minimum",
        auto_create_tables=False,
    )
    schema = create_app(settings).openapi()
    output.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

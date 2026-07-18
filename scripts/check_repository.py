from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = {".epub", ".pdf", ".mobi", ".azw", ".azw3"}
FORBIDDEN_PATH_PARTS = {".agents", "corpus", "private-fixtures", "credentials"}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Cloudflare service token": re.compile(r"\bCF-Access-Client-Secret\s*[:=]\s*[^$<{\s][^\s]+", re.I),
}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> None:
    violations: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        lowered_parts = {part.lower() for part in relative.parts}
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            violations.append(f"private document extension: {relative}")
        if path.name.lower() == "skill.md" or lowered_parts.intersection(FORBIDDEN_PATH_PARTS):
            violations.append(f"forbidden Skill/private-data path: {relative}")
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"possible {label}: {relative}")
    if violations:
        raise SystemExit("Repository policy violations:\n- " + "\n- ".join(violations))
    print("repository policy: ok")


if __name__ == "__main__":
    main()

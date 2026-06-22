"""Pre-commit guard: fail if any staged file contains a known secret string.

Patterns are read from two gitignored sources, so the secrets themselves never
get committed:
  - .env                -> the value of ST_API_KEY
  - .forbidden-strings  -> one literal string per line ('#' lines are comments)

pre-commit passes the staged filenames as arguments.
"""

import sys
from pathlib import Path

# Ignore very short patterns to avoid accidental false positives.
MIN_LEN = 6
# Which .env values to treat as secrets (avoids matching paths like EXPORT_PATH).
SECRET_ENV_KEYS = ("ST_API_KEY",)


def _load_patterns() -> set[str]:
    patterns: set[str] = set()

    env = Path(".env")
    if env.exists():
        for raw in env.read_text().splitlines():
            line = raw.strip()
            if "=" not in line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key.strip() in SECRET_ENV_KEYS and len(value) >= MIN_LEN:
                patterns.add(value)

    blocklist = Path(".forbidden-strings")
    if blocklist.exists():
        for raw in blocklist.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and len(line) >= MIN_LEN:
                patterns.add(line)

    return patterns


def main(filenames: list[str]) -> int:
    patterns = _load_patterns()
    if not patterns:
        return 0

    found = False
    for filename in filenames:
        try:
            content = Path(filename).read_text(errors="ignore")
        except OSError:
            continue
        if any(pattern in content for pattern in patterns):
            print(f"ERROR: blocked secret string found in {filename}")
            found = True

    if found:
        print("Remove the secret before committing, and rotate it if it has leaked.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

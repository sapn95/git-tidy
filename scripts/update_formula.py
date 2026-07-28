#!/usr/bin/env python3
"""Point Formula/git-tidy.rb at a released version and its published checksums.

Called by the release workflow once the assets exist. Kept as a script rather
than an inline heredoc so it can be read, and tested, on its own.

Usage: update_formula.py <version> <formula-path> <asset>=<sha256> ...
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SHA256 = re.compile(r"^[0-9a-f]{64}$")


def update(text: str, version: str, digests: dict[str, str]) -> str:
    text = re.sub(r'version "[^"]+"', f'version "{version}"', text, count=1)
    text = re.sub(r"/download/v[^/]+/", f"/download/v{version}/", text)
    for asset, digest in digests.items():
        pattern = re.compile(
            rf'(git-tidy-{re.escape(asset)}\.tar\.gz"\s*\n\s*sha256 ")[0-9a-f]{{64}}'
        )
        text, count = pattern.subn(rf"\g<1>{digest}", text)
        # Exactly one occurrence, or the formula has drifted from what this
        # script expects and a silent no-op would ship the previous checksum.
        if count != 1:
            raise SystemExit(f"error: {asset} appears {count} times in the formula, expected 1")
    return text


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        raise SystemExit(__doc__)
    version, path = argv[0], Path(argv[1])
    digests: dict[str, str] = {}
    for pair in argv[2:]:
        asset, _, digest = pair.partition("=")
        if not SHA256.match(digest):
            raise SystemExit(f"error: {asset} has no valid sha256: {digest!r}")
        digests[asset] = digest
    path.write_text(update(path.read_text(encoding="utf-8"), version, digests), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

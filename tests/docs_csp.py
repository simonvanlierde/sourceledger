"""Apply the shared docs hash-CSP policy to the rendered Zensical pages."""

import base64
import hashlib
import json
import re
from pathlib import Path

INLINE = re.compile(r"<script(?![^>]*\ssrc=)([^>]*)>([\s\S]*?)</script>")
DATA = re.compile(r'\stype="(?:application/json|application/ld\+json)"')


def script_hashes(html: str) -> list[str]:
    """Return the SHA-256 sources for executable inline scripts."""
    return sorted(
        {
            "'sha256-" + base64.b64encode(hashlib.sha256(code.encode()).digest()).decode() + "'"
            for attrs, code in INLINE.findall(html)
            if not DATA.search(attrs)
        }
    )


def finish() -> None:
    """Reject unreviewed scripts, then put each page's policy before its scripts."""
    approved = set(json.loads(Path("docs/script-hashes.json").read_text(encoding="utf-8")))
    pages = {path: path.read_text(encoding="utf-8") for path in Path("_docs").rglob("*.html")}
    actual = {value for html in pages.values() for value in script_hashes(html)}
    if actual != approved:
        message = f"Inline docs scripts changed: added {actual - approved}, removed {approved - actual}"
        raise ValueError(message)
    for path, html in pages.items():
        policy = "; ".join(
            [
                "default-src 'self'",
                "script-src 'self' " + " ".join(script_hashes(html)),
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' data:",
                "font-src 'self'",
                "connect-src 'self'",
                "worker-src 'self' blob:",
                "object-src 'none'",
                "base-uri 'self'",
                "form-action 'none'",
            ]
        )
        tag = f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
        clean = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', "", html)
        secured, count = re.subn(r'(<meta charset="utf-8"\s*/?>)', r"\1" + tag, clean, count=1)
        if count != 1:
            message = f"{path}: missing UTF-8 charset before script policy"
            raise ValueError(message)
        path.write_text(secured, encoding="utf-8")


if __name__ == "__main__":
    finish()

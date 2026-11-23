"""Apply the shared docs hash-CSP policy to the rendered Zensical pages."""

import base64
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hashes: set[str] = set()
        self.code: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            attributes: dict[str, str | None] = {}
            for name, value in attrs:
                attributes.setdefault(name, value)
            data = (attributes.get("type") or "").strip().lower() in {"application/json", "application/ld+json"}
            self.code = "" if "src" not in attributes and not data else None

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            message = "Self-closing script tags are not supported; use an explicit end tag"
            raise ValueError(message)
        super().handle_startendtag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.code is not None:
            self.code += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.code is not None:
            digest = base64.b64encode(hashlib.sha256(self.code.encode()).digest()).decode()
            self.hashes.add("'sha256-" + digest + "'")
            self.code = None


def script_hashes(html: str) -> list[str]:
    """Hash inline script contents verbatim, excluding external scripts and JSON data.

    Raises:
        ValueError: If a script tag is self-closing.
    """
    parser = _Scripts()
    parser.feed(html)
    parser.close()
    return sorted(parser.hashes)


def finish() -> None:
    """Reject unreviewed scripts, then put each page's policy before its scripts."""
    approved = set(json.loads(Path("docs/script-hashes.json").read_text(encoding="utf-8")))
    pages = []
    for path in Path("_docs").rglob("*.html"):
        html = path.read_text(encoding="utf-8")
        pages.append((path, html, script_hashes(html)))
    actual = {value for _, _, hashes in pages for value in hashes}
    if actual != approved:
        message = f"Inline docs scripts changed: added {actual - approved}, removed {approved - actual}"
        raise ValueError(message)
    for path, html, hashes in pages:
        policy = "; ".join(
            [
                "default-src 'self'",
                "script-src 'self' " + " ".join(hashes),
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

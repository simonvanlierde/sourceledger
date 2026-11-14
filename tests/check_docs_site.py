"""Check the generated pages after `just docs` builds the real site."""

from html.parser import HTMLParser
from pathlib import Path


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.title_tags = []
        self.meta = {}
        self.text = ""
        self.links = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self.in_title:
            self.title_tags.append(tag)
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            self.meta[attributes.get("property") or attributes.get("name") or attributes.get("http-equiv")] = (
                attributes.get("content")
            )
        elif tag == "a":
            self.links.append(attributes.get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        self.text += data


def check_page(path: Path, title: str):
    page = _Page()
    page.feed(path.read_text(encoding="utf-8"))
    assert page.title == title, (path, page.title)
    assert not page.title_tags, (path, page.title_tags)
    assert page.meta["og:title"] == title, (path, page.meta["og:title"])
    assert "may include unreleased features" in page.text, path
    assert "https://github.com/simonvanlierde/sourceledger/blob/main/CHANGELOG.md" in page.links, path
    assert all(text in page.meta["og:image:alt"] for text in ("419.31", "419.32", "SHA-256", "0 problems")), path
    assert "object-src 'none'" in page.meta["Content-Security-Policy"], path
    assert page.meta["og:image"].startswith("https://simonvanlierde.com/sourceledger/assets/og.png"), path
    assert (page.meta["og:image:width"], page.meta["og:image:height"]) == ("1200", "630"), path
    assert page.meta["og:description"], path
    assert page.meta["twitter:card"] == "summary_large_image", path


if __name__ == "__main__":
    for name, title in (
        ("index", "sourceledger"),
        ("getting-started", "Follow a publisher revision - sourceledger"),
        ("api", "Python API - sourceledger"),
    ):
        check_page(Path("_docs") / f"{name}.html", title)

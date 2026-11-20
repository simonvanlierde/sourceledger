import base64
import hashlib
import json
from pathlib import Path

import pytest
from docs_csp import finish, script_hashes
from hypothesis import given
from hypothesis import strategies as st


def test_policy_precedes_scripts_and_rejects_changed_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    Path("docs").mkdir()
    Path("_docs").mkdir()
    path = Path("_docs/index.html")
    html = '<meta charset="utf-8"><script>window.ready = true;</script>'
    Path("docs/script-hashes.json").write_text(json.dumps(script_hashes(html)), encoding="utf-8")
    path.write_text(html, encoding="utf-8")
    finish()
    secured = path.read_text(encoding="utf-8")
    assert secured.index("Content-Security-Policy") < secured.index("<script")
    assert script_hashes(html)[0] in secured
    assert "'unsafe-inline'" not in secured.split("style-src")[0]
    finish()
    assert path.read_text(encoding="utf-8") == secured
    changed = html.replace("true", "false")
    path.write_text(changed, encoding="utf-8")
    with pytest.raises(ValueError, match="Inline docs scripts changed"):
        finish()
    assert path.read_text(encoding="utf-8") == changed


def test_script_hashes_ignore_data_and_external_scripts():
    assert script_hashes('<script type="application/json">{}</script><script src="local.js"></script>') == []
    assert script_hashes('<script type="importmap">{}</script>')
    assert len(script_hashes("<script>one()</script><script>one()</script>")) == 1


@pytest.mark.parametrize(
    "html",
    [
        "<SCRIPT>window.ready = true;</SCRIPT>",
        '<sCrIpT TyPe="module">window.ready = true;</ScRiPt >',
        '<SCRIPT data-note=">">window.ready = true;</sCrIpT>',
    ],
)
def test_script_hashes_preserve_code_across_html_tag_case(html: str):
    expected = script_hashes("<script>window.ready = true;</script>")
    assert expected
    assert script_hashes(html) == expected


@pytest.mark.parametrize(
    "html",
    [
        '<SCRIPT SRC = "local.js">fallback()</SCRIPT>',
        "<sCrIpT sRc='local.js'></ScRiPt>",
        "<SCRIPT TyPe = 'APPLICATION/JSON'>{}</SCRIPT>",
        "<sCrIpT TYPE=application/ld+json>{}</ScRiPt>",
        "<!-- <SCRIPT>commented()</SCRIPT> -->",
    ],
)
def test_script_hashes_ignore_nonexecuting_html_variants(html: str):
    assert script_hashes(html) == []


def test_policy_rejects_unreviewed_mixed_case_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    Path("docs").mkdir()
    Path("_docs").mkdir()
    Path("docs/script-hashes.json").write_text("[]", encoding="utf-8")
    path = Path("_docs/index.html")
    html = '<meta charset="utf-8"><ScRiPt TyPe="module">window.unreviewed = true;</sCrIpT>'
    path.write_text(html, encoding="utf-8")
    with pytest.raises(ValueError, match="Inline docs scripts changed"):
        finish()
    assert path.read_text(encoding="utf-8") == html


@given(
    value=st.text(alphabet="abcABC012 é<&>\n", max_size=80),
    tag=st.sampled_from(["script", "SCRIPT", "sCrIpT"]),
    attribute=st.sampled_from(["type", "TYPE", "TyPe"]),
    quote=st.sampled_from(["'", '"']),
)
def test_script_hashes_preserve_exact_body(value: str, tag: str, attribute: str, quote: str):
    code = "const value = " + json.dumps(value, ensure_ascii=False) + ";\n"
    html = f"<{tag} {attribute} = {quote}module{quote} data-note='>'>{code}</{tag} >"
    expected = base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()
    assert script_hashes(html) == ["'sha256-" + expected + "'"]


def test_script_hashes_use_first_duplicate_type_attribute():
    html = '<script type="module" TYPE="application/json">window.ready = true;</script>'
    assert script_hashes(html) == script_hashes("<script>window.ready = true;</script>")
    assert script_hashes('<script type="application/json" TYPE="module">{}</script>') == []


def test_script_hashes_reject_self_closing_script():
    with pytest.raises(ValueError, match="Self-closing script tags"):
        script_hashes("<script/>window.unreviewed = true;</script>")

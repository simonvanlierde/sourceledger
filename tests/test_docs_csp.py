import json
from pathlib import Path

import pytest
from docs_csp import finish, script_hashes


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

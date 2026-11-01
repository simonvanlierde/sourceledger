"""Release metadata must not carry identifiers from an earlier Zenodo archive."""

import runpy
from pathlib import Path


def test_zenodo_takes_version_date_and_doi_from_the_new_release() -> None:
    helpers = runpy.run_path(str(Path(__file__).parents[1] / ".github/scripts/citation.py"))
    old = {
        "version": "0.1.0",
        "publication_date": "2020-01-01",
        "doi": "10.5555/example",
        "communities": [{"identifier": "example"}],
    }
    owned = helpers["zenodo"]({"title": "Example package", "authors": []}, "MIT")
    result = helpers["merge"](old, owned)
    assert not {"version", "publication_date", "doi"} & result.keys()
    assert result["communities"] == old["communities"]

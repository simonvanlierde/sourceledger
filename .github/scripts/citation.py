#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Write .zenodo.json and codemeta.json from CITATION.cff, the one file you edit.

Run from the repo root: `just citation` writes both files, and `just citation --check`
writes nothing and fails when either disagrees with CITATION.cff. Only the fields
CITATION.cff holds are written: title, description, authors, licence and repository
into both, and version, release date and DOI into codemeta.json only. Zenodo takes the
version and date from the release itself. Other top-level fields you add to the JSON
files, such as Zenodo communities or CodeMeta keywords, stay as they are; the author
lists are rewritten whole.
"""

import importlib
import json
import sys
from pathlib import Path

# Imported by name so the type checker skips it: uv and prek install pyyaml from the
# header above, and the project's own environment may not have it.
yaml = importlib.import_module("yaml")

ORCID = "https://orcid.org/"


def drop_none(d: dict) -> dict:
    """Return d without the keys whose value is None."""
    return {k: v for k, v in d.items() if v is not None}


def family_name(a: dict) -> str | None:
    """Return the family name with its particle, such as "van Lierde", or None."""
    return " ".join(filter(None, [a.get("name-particle"), a.get("family-names")])) or None


def zenodo(cff: dict, lic: str | None) -> dict:
    """Return the .zenodo.json fields CITATION.cff holds; None means drop the key."""
    creators = []
    for a in cff["authors"]:
        given, orcid = a.get("given-names"), a.get("orcid")
        creators.append(
            drop_none(
                {
                    "name": a.get("name") or ", ".join(filter(None, [family_name(a), given])),
                    "orcid": orcid and orcid.removeprefix(ORCID),
                    "affiliation": a.get("affiliation"),
                }
            )
        )
    return {
        "upload_type": "software",
        "title": cff["title"],
        "description": cff.get("abstract") or cff["title"],
        # Zenodo takes these from the new release, even when an old file declared them.
        "version": None,
        "publication_date": None,
        "doi": None,
        "creators": creators,
        # Without a licence Zenodo would publish the files as CC-BY-4.0. `closed`
        # publishes the metadata and DOI, but not the files.
        "access_right": "open" if lic else "closed",
        "license": lic and lic.lower(),
    }


def codemeta(cff: dict, lic: str | None) -> dict:
    """Return the codemeta.json fields CITATION.cff holds; None means drop the key."""
    authors = []
    for a in cff["authors"]:
        # A CFF entity (an organisation) has `name`; a person has given and family names.
        person = {"@type": "Person", "@id": a.get("orcid"), "givenName": a.get("given-names")}
        person["familyName"] = family_name(a)
        p = {"@type": "Organization", "name": a["name"]} if a.get("name") else person
        p["email"] = a.get("email")
        aff = a.get("affiliation")
        p["affiliation"] = aff and {"@type": "Organization", "name": aff}
        authors.append(drop_none(p))
    doi, date = cff.get("doi"), cff.get("date-released")
    return {
        "@context": "https://w3id.org/codemeta/3.0",
        "@type": "SoftwareSourceCode",
        "name": cff["title"],
        "description": cff.get("abstract"),
        "version": cff.get("version"),
        # YAML reads an unquoted date as a date; JSON holds it as text.
        "datePublished": date and str(date),
        "identifier": doi and f"https://doi.org/{doi}",
        "author": authors,
        "codeRepository": cff.get("repository-code"),
        "license": lic and f"https://spdx.org/licenses/{lic}",
    }


def merge(old: dict, owned: dict) -> dict:
    """Old with the owned fields replaced, and dropped where CITATION.cff has none."""
    new = dict(old)
    for k, v in owned.items():
        if v is None:
            new.pop(k, None)
        else:
            new[k] = v
    return new


def main() -> int:
    """Write the two files, or with --check report the stale ones and return 1."""
    check = sys.argv[1:] == ["--check"]
    if sys.argv[1:] not in ([], ["--check"]):
        sys.exit("usage: citation.py [--check]")
    cff = yaml.safe_load(Path("CITATION.cff").read_text(encoding="utf-8"))
    # YAML reads an unquoted 1.10 as the number 1.1, so the text is already lost.
    if not isinstance(cff.get("version"), (str, type(None))):
        sys.exit('CITATION.cff: quote the version, e.g. version: "1.10"')
    lic = cff.get("license")
    if isinstance(lic, list):
        sys.exit("CITATION.cff: give one licence, not a list; Zenodo takes one")
    stale = []
    for name, build in ((".zenodo.json", zenodo), ("codemeta.json", codemeta)):
        path = Path(name)
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        new = merge(old, build(cff, lic))
        if new == old:
            continue
        stale.append(name)
        if not check:
            path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if stale and check:
        sys.stderr.write(f"out of date with CITATION.cff: {', '.join(stale)}. Run `just citation` to rewrite.\n")
        return 1
    for name in stale:
        sys.stdout.write(f"wrote {name}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

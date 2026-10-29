#!/usr/bin/env python3
"""Turn one PDF or a folder of PDFs into a reference list for zotero_docx_import.py.

Each PDF is matched to a verified PMID or DOI: identifiers printed in the PDF first,
then PubMed and CrossRef title searches. A match is kept only when its title agrees
with the PDF, so a wrong or truncated DOI in a PDF is caught. The list is written as
.docx (or .txt), one numbered reference per line, unmatched PDFs last.

Importing the list creates citation records without uploading the PDF files: about
one Zotero request per reference instead of about five, and no Zotero storage used.
No Zotero API key is needed for this step.
"""

from __future__ import annotations

import argparse
import difflib
import html
import re
import time
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import requests

import zotero_api_import as api

# Pages saved from a browser carry the site in the file name: "Title - PubMed.pdf".
SITE_SUFFIX = re.compile(r"\s+[-_]\s+[^-_]*?(?:PubMed|PMC|ScienceDirect|Wiley Online Library|Springer Nature Link|"
                         r"ACS Publications|Journal|Oncology|Nature|BMC)[^-_]*$", re.I)
# CrossRef also indexes commentary on a paper under nearly the same title.
NOT_THE_PAPER = re.compile(r"^(?:comment on|reply|response to|correction|erratum|faculty opinions|supplement)", re.I)
# Journal-level and supplementary-file DOIs are never the article itself.
NOT_AN_ARTICLE_DOI = re.compile(r"\(issn\)|/supp", re.I)
# A title search returns look-alikes, so its matches must be near-exact, unlike an identifier printed in the PDF.
SEARCH_SIMILARITY = 0.85
HEADERS = {"User-Agent": "ZoteroReferenceImport/1.0"}


def similar(source: str, found: str) -> float:
    return difflib.SequenceMatcher(None, api.normalized_title(source), api.normalized_title(found)).ratio()


def title_from_filename(path: Path) -> str:
    title = path.stem.strip()
    if " " not in title:  # publisher downloads: "wang-et-al-2024-global-incidence-of-..."
        title = title.replace("-", " ")
    # Wiley: "Title - Author - 2021 - Journal - Wiley Online Library"
    title = re.sub(r"\s+-\s+[^-]+\s+-\s+(?:19|20)\d{2}\s+-\s+.*$", "", title)
    # " _ " separates the site in browser-saved names ("Title _ Nature Cell Biology")
    title = re.sub(r"\s+_\s+.*$", "", title)
    for _ in range(4):
        shorter = SITE_SUFFIX.sub("", title).strip()
        if shorter == title or len(shorter.split()) < 4:
            break
        title = shorter
    # "_ " stands for a colon the file system did not allow ("Cancer_ a review").
    return re.sub(r"_\s", ": ", title).strip(" _")


def from_pubmed(pmid: str, title: str) -> dict[str, Any] | None:
    item = api.pubmed_item(pmid, "LIST_ONLY", title)
    time.sleep(0.35)
    if not item:
        return None
    return dict(title=item["title"], journal=item.get("journalAbbreviation") or item.get("publicationTitle", ""),
                year=item.get("date", ""), pmid=pmid, doi=item.get("DOI") or None)


def from_crossref(doi: str, title: str) -> dict[str, Any] | None:
    if NOT_AN_ARTICLE_DOI.search(doi):
        return None
    item = api.crossref_item(doi, "LIST_ONLY", title)
    time.sleep(0.2)
    if not item or NOT_THE_PAPER.match(item["title"]):
        return None
    return dict(title=item["title"], journal=item.get("publicationTitle", ""),
                year=item.get("date", "").split("-")[0], pmid=None, doi=doi)


def pubmed_title_search(title: str) -> dict[str, Any] | None:
    stopwords = {"the", "and", "for", "with", "from", "into", "its", "are", "not"}
    words = [word for word in re.sub(r"[^A-Za-z0-9+ ]", " ", title).split()
             if len(word) > 2 and word.casefold() not in stopwords][:12]
    if len(words) < 4:
        return None
    for pmid in api.pubmed_search_ids(" AND ".join(f"{word}[Title]" for word in words), maximum=5):
        if (match := from_pubmed(pmid, title)) and similar(title, match["title"]) >= SEARCH_SIMILARITY:
            return match
    return None


def crossref_title_search(title: str, filename: str) -> dict[str, Any] | None:
    response = requests.get("https://api.crossref.org/works", headers=HEADERS, timeout=30,
                            params={"query.bibliographic": title, "rows": 10})
    if response.status_code != 200:
        return None
    candidates = [
        item for item in response.json().get("message", {}).get("items", [])
        if similar(title, (item.get("title") or [""])[0]) >= SEARCH_SIMILARITY
        and not NOT_AN_ARTICLE_DOI.search(item["DOI"])
    ]
    # Several records can share a title (an abstract and the full paper, a report and
    # the journal article). Prefer the journal named in the file name, then the most-cited.
    def journal_in_filename(item: dict[str, Any]) -> bool:
        journal = api.normalized_title((item.get("container-title") or [""])[0])
        return bool(journal) and journal in api.normalized_title(filename)

    candidates.sort(key=lambda item: (journal_in_filename(item), item.get("is-referenced-by-count", 0)), reverse=True)
    for candidate in candidates:
        if match := from_crossref(candidate["DOI"], title):
            others = len({item["DOI"].lower() for item in candidates}) - 1
            if others:
                match["note"] = f"{others} other record(s) share this title; check it is the right one"
            return match
    return None


def resolve(record: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    title = title_from_filename(Path(record["path"]))
    steps = []
    if record.get("pmid"):
        steps.append(("PMID in PDF", lambda: from_pubmed(record["pmid"], title)))
    if record.get("doi"):
        steps.append(("DOI in PDF", lambda: from_crossref(record["doi"], title)))
        steps.append(("PubMed DOI lookup", lambda: next(
            (m for pmid in api.pubmed_search_ids(f'{record["doi"]}[AID]', maximum=3) if (m := from_pubmed(pmid, title))),
            None)))
    steps.append(("PubMed title search", lambda: pubmed_title_search(title)))
    steps.append(("CrossRef title search", lambda: crossref_title_search(title, Path(record["path"]).stem)))
    for method, step in steps:
        try:
            if match := step():
                return match, method
        except requests.RequestException as exc:
            print(f"  lookup error ({method}): {exc}", flush=True)
    return None, "no verified PMID or DOI"


def citation(match: dict[str, Any] | None, title: str) -> str:
    if not match:
        return title.rstrip(".") + "."
    parts = [match["title"].rstrip(".")] + [part for part in (match["journal"], match["year"]) if part]
    identifier = f"PMID: {match['pmid']}" if match["pmid"] else f"doi:{match['doi']}"
    return html.unescape(". ".join(parts)) + ". " + identifier  # CrossRef sends "&amp;" in some titles


def write_docx(path: Path, lines: list[str]) -> None:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in lines)
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="word/document.xml" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/></Relationships>',
        "word/_rels/document.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        "word/document.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, required=True, help="Folder of PDFs (scanned recursively) or one PDF")
    parser.add_argument("--output", type=Path, help="Reference list to write, .docx or .txt "
                        "(default: <work-dir>/<folder name>_references.docx)")
    parser.add_argument("--work-dir", type=Path, default=Path.cwd() / "zotero-imports", help="Where to write output")
    parser.add_argument("--skip-uploaded", type=Path, metavar="STATE_JSON",
                        help="state.json of a PDF upload: leave out PDFs it already uploaded")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if not (source.is_dir() or (source.is_file() and source.suffix.lower() == ".pdf")):
        raise SystemExit(f"Source must be a folder or a .pdf file: {source}")
    output = args.output or args.work_dir / f"{source.stem}_references.docx"
    if output.suffix.lower() not in (".docx", ".txt"):
        raise SystemExit("--output must end in .docx or .txt")
    uploaded: set[str] = set()
    if args.skip_uploaded:
        state = api.load_json(args.skip_uploaded, {})
        uploaded = {digest for digest, entry in state.get("completed", {}).items() if entry.get("status") == "uploaded"}

    records = [r for r in api.inventory(source)["records"] if r["extension"] == ".pdf" and r["sha256"] not in uploaded]
    matched, unmatched, report, seen = [], [], [], set()
    for index, record in enumerate(records, start=1):
        title = title_from_filename(Path(record["path"]))
        match, method = resolve(record)
        identifier = match and (match["pmid"] or match["doi"])
        duplicate = bool(identifier) and identifier in seen
        if identifier and not duplicate:
            seen.add(identifier)
            matched.append(citation(match, title))
        elif not match:
            unmatched.append(citation(None, title))
        label = "duplicate of an earlier PDF" if duplicate else method
        note = (match or {}).get("note", "")
        report.append(dict(file=record["relative_path"], method=label, pmid=match and match["pmid"],
                           doi=match and match["doi"], title=match["title"] if match else title, note=note))
        print(f"[{index}/{len(records)}] {label}: {record['filename']}" + (f"  ({note})" if note else ""), flush=True)

    lines = [f"{number}. {line}" for number, line in enumerate(matched + unmatched, start=1)]
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".docx":
        write_docx(output, lines)
    else:
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report_path = output.with_suffix(".report.json")
    api.write_json(report_path, dict(source=str(source), skipped_uploaded=len(uploaded), pdfs=report))

    print(f"Reference list: {output.resolve()}")
    print(f"{len(matched)} PDFs with a verified PMID/DOI; {len(unmatched)} without (listed last, for manual entry); "
          f"{len(records) - len(matched) - len(unmatched)} duplicates left out.")
    flagged = [entry["file"] for entry in report if entry["note"]]
    if flagged:
        print(f"Check {len(flagged)} match(es) where several records share a title: " + "; ".join(flagged))
    print(f"Per-file report: {report_path.resolve()}")
    print("Next: import the list with zotero_docx_import.py (preview first, then --upload).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

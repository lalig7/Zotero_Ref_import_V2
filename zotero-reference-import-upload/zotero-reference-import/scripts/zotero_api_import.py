#!/usr/bin/env python3
"""API importer for PMID PDFs (one file or a whole folder) into a chosen Zotero collection.

The API key is read from a hidden prompt or ZOTERO_API_KEY. It is never written
to the plan, state file, logs, or command line.
"""

from __future__ import annotations

import argparse
import difflib
import getpass
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests
from pypdf import PdfReader
from pyzotero import zotero, zotero_errors


API_ROOT = "https://api.zotero.org"
RATE_LIMIT_RETRIES = 5
# Zotero rate-limits bursts (seen after ~28 uploads), so pause after every batch of writes.
BATCH_SIZE = 20
BATCH_PAUSE_SECONDS = 180
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
PMID_PATTERNS = (
    re.compile(r"\bPMID\s*[:=]?\s*(\d{6,9})\b", re.I),
    re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{6,9})", re.I),
    re.compile(r"/pubmed/(\d{6,9})\b", re.I),
)
PMCID_RE = re.compile(r"\bPMC\d{5,9}\b", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
EXCLUDED_NAMES = {".DS_Store"}
logging.getLogger("pypdf").setLevel(logging.ERROR)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_doi(raw: str) -> str:
    doi = raw.strip().rstrip(".,;:")
    while doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi[:-1]
    while doi.endswith("]") and doi.count("[") < doi.count("]"):
        doi = doi[:-1]
    return doi.lower()


def pdf_identifiers(path: Path) -> tuple[str | None, str, str | None, str]:
    """Return conservative PMID and DOI candidates with evidence levels."""
    try:
        reader = PdfReader(str(path))
        metadata_text = " ".join(str(value) for value in (reader.metadata or {}).values())
        for pattern in PMID_PATTERNS:
            match = pattern.search(metadata_text)
            if match:
                return match.group(1), "pdf-metadata", None, "not-needed"
        metadata_hits = [clean_doi(hit) for hit in DOI_RE.findall(metadata_text)]

        pages = []
        for page in reader.pages[:3]:
            pages.append(page.extract_text() or "")
        text = "\n".join(pages)
        for pattern in PMID_PATTERNS:
            match = pattern.search(text)
            if match:
                doi = metadata_hits[0] if metadata_hits else None
                return match.group(1), "explicit-first-pages", doi, "pdf-metadata" if doi else "not-found"
        explicit = re.search(
            r"(?:https?://(?:dx\.)?doi\.org/|\bdoi\s*:\s*)(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
            text,
            flags=re.I,
        )
        if explicit:
            return None, "not-found", clean_doi(explicit.group(1)), "explicit-first-pages"

        if metadata_hits:
            return None, "not-found", metadata_hits[0], "pdf-metadata"

        hits = [clean_doi(hit) for hit in DOI_RE.findall(text[:8000])]
        if len(set(hits)) == 1:
            return None, "not-found", hits[0], "single-first-pages"
    except Exception:
        return None, "unreadable", None, "unreadable-or-no-doi"
    return None, "not-found", None, "no-confident-doi"


def clean_url(raw: str) -> str | None:
    url = raw.strip().rstrip(".,;:)]}>")
    if not url.startswith(("http://", "https://")):
        return None
    blocked = ("doi.org/", "google.com/", "googleusercontent.com/", "zotero.org/")
    if any(domain in url.casefold() for domain in blocked):
        return None
    return url


def pdf_source_url(path: Path) -> tuple[str | None, str]:
    try:
        reader = PdfReader(str(path))
        candidates: list[tuple[str, str]] = []
        metadata_text = " ".join(str(value) for value in (reader.metadata or {}).values())
        for raw in URL_RE.findall(metadata_text):
            if url := clean_url(raw):
                candidates.append((url, "pdf-metadata"))
        for page in reader.pages[:3]:
            annotations = page.get("/Annots") or []
            for reference in annotations:
                try:
                    annotation = reference.get_object()
                    action = annotation.get("/A")
                    uri = action.get("/URI") if action else None
                    if uri and (url := clean_url(str(uri))):
                        candidates.append((url, "pdf-link"))
                except Exception:
                    continue
            for raw in URL_RE.findall(page.extract_text() or ""):
                if url := clean_url(raw):
                    candidates.append((url, "first-pages"))
        if candidates:
            return candidates[0]
    except Exception:
        pass
    return None, "not-found"


def inventory(source: Path) -> dict[str, Any]:
    # A single PDF is treated as a one-file folder.
    root = source.parent if source.is_file() else source
    candidates = [source] if source.is_file() else source.rglob("*")
    files = sorted(
        (
            path
            for path in candidates
            if path.is_file()
            and not path.is_symlink()
            and path.name not in EXCLUDED_NAMES
            and not path.name.startswith(".")
            and not path.name.startswith("~$")
        ),
        key=lambda value: str(value).casefold(),
    )
    records: list[dict[str, Any]] = []
    by_hash: dict[str, list[Path]] = defaultdict(list)
    counts: Counter[str] = Counter()
    for index, path in enumerate(files, start=1):
        digest = sha256(path)
        by_hash[digest].append(path)
        extension = path.suffix.lower() or "[no extension]"
        if extension == ".pdf":
            pmid, pmid_evidence, doi, doi_evidence = pdf_identifiers(path)
        else:
            pmid, pmid_evidence, doi, doi_evidence = None, "not-pdf", None, "not-pdf"
        records.append(
            {
                "path": str(path.resolve()),
                "relative_path": str(path.relative_to(root)),
                "filename": path.name,
                "extension": extension,
                "size_bytes": path.stat().st_size,
                "sha256": digest,
                "pmid": pmid,
                "pmid_evidence": pmid_evidence,
                "doi": doi,
                "doi_evidence": doi_evidence,
            }
        )
        counts[extension] += 1
        print(f"Planning {index}/{len(files)}: {path.name}", flush=True)

    duplicates = [[str(path.resolve()) for path in paths] for paths in by_hash.values() if len(paths) > 1]
    duplicate_paths = {path for group in duplicates for path in group[1:]}
    unique = [record for record in records if record["path"] not in duplicate_paths]
    return {
        "source": str(source.resolve()),
        "total_files": len(records),
        "unique_file_count": len(unique),
        "counts_by_extension": dict(sorted(counts.items())),
        "exact_duplicate_groups": duplicates,
        "records": unique,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def backfill_urls(plan: dict[str, Any], plan_path: Path) -> None:
    pdf_records = [record for record in plan["records"] if record["extension"] == ".pdf"]
    for index, record in enumerate(pdf_records, start=1):
        if "url" not in record:
            url, evidence = pdf_source_url(Path(record["path"]))
            record["url"] = url
            record["url_evidence"] = evidence
            print(f"URL scan {index}/{len(pdf_records)}: {record['filename']}", flush=True)
            write_json(plan_path, plan)


def pace_batch(sent: int) -> None:
    if sent and sent % BATCH_SIZE == 0:
        print(f"{sent} items sent; pausing {BATCH_PAUSE_SECONDS}s so Zotero does not rate-limit", flush=True)
        time.sleep(BATCH_PAUSE_SECONDS)


def authenticate(api_key: str, permissions: tuple[str, ...] = ("library", "write", "files")) -> dict[str, Any]:
    for attempt in range(1, RATE_LIMIT_RETRIES + 2):
        response = requests.get(
            f"{API_ROOT}/keys/current",
            headers={"Zotero-API-Key": api_key, "Zotero-API-Version": "3"},
            timeout=30,
        )
        if response.status_code != 429 or attempt > RATE_LIMIT_RETRIES:
            break
        retry_after = response.headers.get("Retry-After", "")
        wait = int(retry_after) if retry_after.isdigit() else 60 * attempt
        print(f"Zotero rate limit; waiting {wait}s before retrying the key check", flush=True)
        time.sleep(wait)
    response.raise_for_status()
    identity = response.json()
    access = identity.get("access", {}).get("user", {})
    missing = [permission for permission in permissions if not access.get(permission)]
    if missing:
        raise RuntimeError(f"API key lacks required personal-library permissions: {', '.join(missing)}")
    return identity


def find_collection(client: zotero.Zotero, name: str) -> dict[str, Any]:
    collections = client.everything(client.collections())
    matches = [
        collection
        for collection in collections
        if collection.get("data", {}).get("name", "").casefold() == name.casefold()
    ]
    if len(matches) != 1:
        names = [collection.get("data", {}).get("name", "") for collection in collections]
        close = difflib.get_close_matches(name, names, n=5, cutoff=0.4)
        hint = f" Closest names: {close}." if close else f" Your library has {len(names)} collection(s)."
        raise RuntimeError(
            f"Expected one Zotero collection named {name!r}; found {len(matches)}.{hint} "
            "Create it in My Library and sync Zotero desktop, or use the exact name."
        )
    return matches[0]


def existing_items(
    client: zotero.Zotero, collection_key: str
) -> tuple[dict[str, str], dict[str, str], dict[str, str], set[str]]:
    doi_to_key: dict[str, str] = {}
    pmid_to_key: dict[str, str] = {}
    url_to_key: dict[str, str] = {}
    filenames: set[str] = set()
    # One paged listing (children included) instead of a request per item, which
    # used up Zotero's rate limit before any upload started.
    for item in client.everything(client.collection_items(collection_key, limit=100)):
        data = item.get("data", {})
        if data.get("filename"):
            filenames.add(data["filename"].casefold())
        if data.get("parentItem"):
            continue
        doi = str(data.get("DOI", "")).strip().lower()
        if doi:
            doi_to_key[doi] = item["key"]
        extra = str(data.get("extra", ""))
        if match := re.search(r"\bPMID\s*:\s*(\d{1,9})\b", extra, flags=re.I):
            pmid_to_key[match.group(1)] = item["key"]
        if data.get("url"):
            url_to_key[str(data["url"]).strip()] = item["key"]
    return doi_to_key, pmid_to_key, url_to_key, filenames


def element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def pubmed_item(pmid: str, collection_key: str, source_stem: str) -> dict[str, Any] | None:
    response = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={
            "db": "pubmed",
            "id": pmid,
            "retmode": "xml",
            "tool": "ZoteroReferenceImport",
        },
        headers={"User-Agent": "ZoteroReferenceImport/1.0"},
        timeout=30,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    article = root.find(".//PubmedArticle")
    if article is None:
        return None
    title = element_text(article.find(".//ArticleTitle"))
    if not titles_agree(source_stem, title):
        return None
    journal = element_text(article.find(".//Journal/Title"))
    abbreviation = element_text(article.find(".//ISOAbbreviation"))
    pubdate = article.find(".//JournalIssue/PubDate")
    year = element_text(pubdate.find("Year")) if pubdate is not None else ""
    if not year and pubdate is not None:
        year = element_text(pubdate.find("MedlineDate"))
    volume = element_text(article.find(".//JournalIssue/Volume"))
    issue = element_text(article.find(".//JournalIssue/Issue"))
    pages = element_text(article.find(".//Pagination/MedlinePgn"))
    doi = ""
    for article_id in article.findall(".//ArticleId"):
        if article_id.attrib.get("IdType") == "doi":
            doi = element_text(article_id)
            break
    creators = []
    for author in article.findall(".//AuthorList/Author"):
        collective = element_text(author.find("CollectiveName"))
        first = element_text(author.find("ForeName"))
        last = element_text(author.find("LastName"))
        if first or last:
            creators.append({"creatorType": "author", "firstName": first, "lastName": last})
        elif collective:
            creators.append({"creatorType": "author", "name": collective})
    return {
        "itemType": "journalArticle",
        "title": title,
        "creators": creators,
        "DOI": doi,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "date": year,
        "publicationTitle": journal,
        "journalAbbreviation": abbreviation,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "extra": f"PMID: {pmid}",
        "collections": [collection_key],
        "tags": [],
        "relations": {},
    }


def pubmed_search_ids(term: str, maximum: int = 5) -> list[str]:
    response = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": term,
            "retmode": "json",
            "retmax": maximum,
            "tool": "ZoteroReferenceImport",
        },
        headers={"User-Agent": "ZoteroReferenceImport/1.0"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("esearchresult", {}).get("idlist", [])


def resolve_missing_pmids(plan: dict[str, Any], plan_path: Path) -> None:
    records = plan["records"]
    missing = [record for record in records if record["extension"] == ".pdf" and not record.get("pmid")]
    for index, record in enumerate(missing, start=1):
        try:
            candidates: list[str] = []
            method = ""
            if record.get("doi"):
                candidates = pubmed_search_ids(f'{record["doi"]}[AID]', maximum=3)
                method = "pubmed-doi-lookup"
            if not candidates:
                title_query = normalized_title(Path(record["filename"]).stem)
                words = title_query.split()[:14]
                if len(words) >= 4:
                    candidates = pubmed_search_ids(f'"{" ".join(words)}"[Title]', maximum=5)
                    method = "pubmed-title-search"
            resolved = None
            for candidate in candidates:
                if pubmed_item(candidate, "VALIDATION_ONLY", Path(record["filename"]).stem):
                    resolved = candidate
                    break
                time.sleep(0.35)
            if resolved:
                record["pmid"] = resolved
                record["pmid_evidence"] = method
                print(f"PMID resolved {index}/{len(missing)}: {record['filename']} -> {resolved}", flush=True)
            else:
                print(f"No PMID {index}/{len(missing)}: {record['filename']}", flush=True)
        except Exception as exc:
            print(f"PMID lookup error {index}/{len(missing)}: {record['filename']}: {exc}", flush=True)
        write_json(plan_path, plan)
        time.sleep(0.35)


def normalized_title(value: str) -> str:
    value = re.sub(r"\b(?:pmc|pubmed|sciencedirect|pdf|springer nature link)\b", " ", value, flags=re.I)
    value = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return " ".join(value.split())


def titles_agree(source: str, resolved: str) -> bool:
    source_norm = normalized_title(source)
    resolved_norm = normalized_title(resolved)
    if not source_norm or not resolved_norm:
        return False
    sequence = difflib.SequenceMatcher(None, source_norm, resolved_norm).ratio()
    source_tokens = set(source_norm.split())
    resolved_tokens = set(resolved_norm.split())
    containment = len(source_tokens & resolved_tokens) / max(1, min(len(source_tokens), len(resolved_tokens)))
    if min(len(source_tokens), len(resolved_tokens)) < 4:
        # A one-word title such as a journal's ("Cancer") is contained in almost anything.
        return sequence >= 0.8
    return sequence >= 0.55 or containment >= 0.68


def crossref_item(doi: str, collection_key: str, source_stem: str) -> dict[str, Any] | None:
    response = requests.get(
        f"https://api.crossref.org/works/{requests.utils.quote(doi, safe='')}",
        headers={"User-Agent": "ZoteroReferenceImport/1.0"},
        timeout=30,
    )
    if response.status_code != 200:
        return None
    data = response.json().get("message", {})
    creators = []
    for author in data.get("author", []):
        first = str(author.get("given", "")).strip()
        last = str(author.get("family", "")).strip()
        name = str(author.get("name", "")).strip()
        if first or last:
            creators.append({"creatorType": "author", "firstName": first, "lastName": last})
        elif name:
            creators.append({"creatorType": "author", "name": name})
    date_parts = (data.get("published") or data.get("published-print") or data.get("published-online") or {}).get(
        "date-parts", [[]]
    )[0]
    type_map = {
        "journal-article": "journalArticle",
        "proceedings-article": "conferencePaper",
        "book-chapter": "bookSection",
        "book": "book",
        "posted-content": "preprint",
        "report": "report",
    }
    titles = data.get("title") or [""]
    if not titles_agree(source_stem, titles[0]):
        return None
    containers = data.get("container-title") or [""]
    return {
        "itemType": type_map.get(data.get("type"), "journalArticle"),
        "title": titles[0],
        "creators": creators,
        "DOI": doi,
        "url": f"https://doi.org/{doi}",
        "date": "-".join(str(part) for part in date_parts),
        "publicationTitle": containers[0],
        "volume": str(data.get("volume", "")),
        "issue": str(data.get("issue", "")),
        "pages": str(data.get("page", "")),
        "ISSN": (data.get("ISSN") or [""])[0],
        "collections": [collection_key],
        "tags": [],
        "relations": {},
    }


def webpage_item(url: str, title: str, collection_key: str) -> dict[str, Any]:
    return {
        "itemType": "webpage",
        "title": title,
        "creators": [],
        "url": url,
        "accessDate": time.strftime("%Y-%m-%d"),
        "websiteTitle": "",
        "date": "",
        "collections": [collection_key],
        "tags": [],
        "relations": {},
    }


def created_key(result: dict[str, Any]) -> str:
    successful = result.get("successful", {})
    if successful:
        first = successful[sorted(successful, key=int)[0]]
        return first.get("key") or first.get("data", {}).get("key")
    success = result.get("success", {})
    if success:
        return success[sorted(success, key=int)[0]]
    raise RuntimeError(f"Zotero did not create the parent item: {result}")


def upload_attachment(
    client: zotero.Zotero, path: Path, collection_key: str, parent_key: str | None
) -> tuple[str, dict[str, Any]]:
    attachment = {
        "itemType": "attachment",
        "linkMode": "imported_file",
        "title": path.name,
        "filename": str(path),
        "contentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "charset": "",
        "note": "",
        "tags": [],
        "relations": {},
    }
    if not parent_key:
        attachment["collections"] = [collection_key]
    result = client.upload_attachments([attachment], parentid=parent_key)
    if not result.get("success") and not result.get("unchanged"):
        raise RuntimeError(f"Attachment upload failed: {result.get('failure')}")
    saved = (result.get("success") or result.get("unchanged"))[0]
    return saved.get("key", ""), result


def run_upload(plan: dict[str, Any], collection_name: str, state_path: Path, visible_key: bool = False) -> int:
    if visible_key:
        api_key = input("Zotero API key (visible): ").strip()
    else:
        api_key = (os.environ.get("ZOTERO_API_KEY") or getpass.getpass("Zotero API key (hidden): ")).strip()
    if not api_key:
        raise SystemExit("No API key entered. Run the command again and paste the key before pressing Return.")
    identity = authenticate(api_key)
    user_id = str(identity["userID"])
    client = zotero.Zotero(user_id, "user", api_key, upload_timeout=180)
    collection = find_collection(client, collection_name)
    collection_key = collection["key"]
    doi_to_key, pmid_to_key, url_to_key, existing_filenames = existing_items(client, collection_key)
    state = load_json(state_path, {"collection": collection_name, "completed": {}, "failed": {}})
    context = {"source": plan["source"], "user_id": user_id, "collection_key": collection_key}
    if state.get("context") != context and (state.get("completed") or state.get("failed")):
        raise RuntimeError("Progress file belongs to another import or lacks library identity. Choose a new --state path.")
    state["context"] = context
    completed: dict[str, Any] = state.setdefault("completed", {})
    failed: dict[str, Any] = state.setdefault("failed", {})

    print(f"Authenticated as {identity.get('username', user_id)}")
    print(f"Target collection: {collection_name} ({collection_key})")
    print(f"Unique source files: {len(plan['records'])}")
    print("Files will now be uploaded to Zotero storage. Press Ctrl-C to stop safely.", flush=True)

    sent = 0
    for index, record in enumerate(plan["records"], start=1):
        digest = record["sha256"]
        path = Path(record["path"])
        if digest in completed:
            print(f"[{index}/{len(plan['records'])}] already completed: {path.name}")
            continue
        if path.name.casefold() in existing_filenames:
            completed[digest] = {"path": str(path), "status": "existing-filename"}
            write_json(state_path, state)
            print(f"[{index}/{len(plan['records'])}] already present: {path.name}")
            continue

        attempt = 0
        while True:
            parent_key = None
            created_parent = False
            try:
                pmid = record.get("pmid")
                doi = record.get("doi")
                url = record.get("url")
                if pmid:
                    parent_key = pmid_to_key.get(pmid)
                    metadata = None if parent_key else pubmed_item(pmid, collection_key, path.stem)
                    if plan.get("pmid_only") and not parent_key and not metadata:
                        raise RuntimeError("PMID title could not be verified; left for review")
                    if metadata and not parent_key:
                        resolved_doi = str(metadata.get("DOI", "")).lower()
                        parent_key = doi_to_key.get(resolved_doi) if resolved_doi else None
                        if not parent_key:
                            parent_key = created_key(client.create_items([metadata]))
                            created_parent = True
                            if resolved_doi:
                                doi_to_key[resolved_doi] = parent_key
                        pmid_to_key[pmid] = parent_key
                elif doi and not plan.get("pmid_only"):
                    parent_key = doi_to_key.get(doi)
                    if not parent_key:
                        metadata = crossref_item(doi, collection_key, path.stem)
                        if metadata:
                            parent_key = created_key(client.create_items([metadata]))
                            doi_to_key[doi] = parent_key
                            created_parent = True
                if not parent_key and url and not plan.get("pmid_only"):
                    parent_key = url_to_key.get(url)
                    if not parent_key:
                        parent_key = created_key(client.create_items([webpage_item(url, path.stem, collection_key)]))
                        url_to_key[url] = parent_key
                        created_parent = True
                attachment_key, _result = upload_attachment(client, path, collection_key, parent_key)
                completed[digest] = {
                    "path": str(path),
                    "status": "uploaded",
                    "attachment_key": attachment_key,
                    "parent_key": parent_key,
                    "created_parent": created_parent,
                    "pmid": pmid,
                    "doi": doi,
                    "url": url,
                }
                failed.pop(digest, None)
                existing_filenames.add(path.name.casefold())
                write_json(state_path, state)
                print(f"[{index}/{len(plan['records'])}] uploaded: {path.name}", flush=True)
                time.sleep(1)
                sent += 1
                pace_batch(sent)
                break
            except KeyboardInterrupt:
                write_json(state_path, state)
                print("\nStopped safely. Re-run the same command to resume.")
                return 130
            except (zotero_errors.TooManyRetriesError, zotero_errors.TooManyRequestsError) as exc:
                attempt += 1
                if attempt <= RATE_LIMIT_RETRIES:
                    wait = 60 * attempt
                    print(f"[{index}/{len(plan['records'])}] Zotero rate limit; waiting {wait}s, then retrying", flush=True)
                    time.sleep(wait)
                    continue
                failed[digest] = {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
                write_json(state_path, state)
                print(f"[{index}/{len(plan['records'])}] FAILED: {path.name}: {exc}", file=sys.stderr, flush=True)
                break
            except Exception as exc:
                failed[digest] = {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
                write_json(state_path, state)
                print(f"[{index}/{len(plan['records'])}] FAILED: {path.name}: {exc}", file=sys.stderr, flush=True)
                break

    uploaded = sum(1 for value in completed.values() if value.get("status") == "uploaded")
    existing = sum(1 for value in completed.values() if value.get("status") == "existing-filename")
    print(f"Done. Uploaded: {uploaded}; already present: {existing}; failed: {len(failed)}")
    print(f"State: {state_path.resolve()}")
    return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Research folder to scan recursively, or a single PDF")
    parser.add_argument("--collection", required=True, help="Exact existing Zotero collection name")
    parser.add_argument("--plan", type=Path, help="Optional custom plan path")
    parser.add_argument("--state", type=Path, help="Optional custom progress path")
    parser.add_argument("--work-dir", type=Path, default=Path.cwd() / "zotero-imports", help="Directory for separate per-project plans and progress")
    parser.add_argument("--rebuild-plan", action="store_true", help="Re-read PDFs and replace the plan")
    parser.add_argument("--resolve-pmids", action="store_true", help="Resolve missing PMIDs via NCBI")
    parser.add_argument("--extract-urls", action="store_true", help="Extract source URLs from PDFs")
    parser.add_argument("--upload", action="store_true", help="Prompt for a key and perform the uploads")
    parser.add_argument("--visible-key", action="store_true", help="Show the API key while entering it in Terminal")
    parser.add_argument(
        "--pmid-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Default: only PDFs with a PMID extracted directly from the file. "
        "--no-pmid-only also imports PDFs by DOI or URL, and attaches the rest without metadata",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.source = args.source.expanduser().resolve()
    project_id = hashlib.sha256((str(args.source) + "\n" + args.collection).encode()).hexdigest()[:20]
    args.plan = args.plan or args.work_dir / project_id / "plan.json"
    args.state = args.state or args.work_dir / project_id / "state.json"
    if not (args.source.is_dir() or (args.source.is_file() and args.source.suffix.lower() == ".pdf")):
        raise SystemExit(f"Source must be a folder or a .pdf file: {args.source}")
    if args.plan.exists() and not args.rebuild_plan:
        plan = load_json(args.plan, {})
        if plan.get("source") != str(args.source):
            raise SystemExit("Plan source does not match --source. Choose a different --plan or rebuild it.")
    else:
        plan = inventory(args.source)
        write_json(args.plan, plan)

    if args.resolve_pmids:
        resolve_missing_pmids(plan, args.plan)
    if args.extract_urls:
        backfill_urls(plan, args.plan)

    if args.pmid_only:
        selected = [
            dict(record, doi=None, url=None)
            for record in plan["records"]
            if record.get("extension") == ".pdf"
            and record.get("pmid")
            and record.get("pmid_evidence") in {"pdf-metadata", "explicit-first-pages"}
        ]
        plan = dict(plan, records=selected, unique_file_count=len(selected), pmid_only=True)
        print("Scope: PDFs with PMIDs present in the file. Unverified PMID matches are left for review.")
    else:
        selected = [record for record in plan["records"] if record.get("extension") == ".pdf"]
        plan = dict(plan, records=selected, unique_file_count=len(selected))
        print("Scope: all PDFs, by PMID, then DOI, then URL; others are attached without metadata.")

    pmid_count = sum(1 for record in plan["records"] if record.get("pmid"))
    doi_fallback_count = sum(1 for record in plan["records"] if not record.get("pmid") and record.get("doi"))
    url_fallback_count = sum(
        1
        for record in plan["records"]
        if not record.get("pmid") and not record.get("doi") and record.get("url")
    )
    print(
        f"Plan: {plan['unique_file_count']} unique files; "
        f"{pmid_count} with a confident PMID; "
        f"{doi_fallback_count} with DOI fallback; "
        f"{url_fallback_count} with URL fallback; "
        f"{len(plan['exact_duplicate_groups'])} exact duplicate group(s)."
    )
    print(f"Plan file: {args.plan.resolve()}")
    if not args.upload:
        print("Dry run only. Add --upload to authenticate and upload.")
        return 0
    return run_upload(plan, args.collection, args.state, visible_key=args.visible_key)


if __name__ == "__main__":
    raise SystemExit(main())

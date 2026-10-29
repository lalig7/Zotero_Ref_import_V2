#!/usr/bin/env python3
"""Import bibliography records from .docx, .txt or .md: PMID, then DOI, then URL. Preview by default."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import zotero_api_import as api

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def paragraphs(path):
    if path.suffix.lower() != '.docx':
        # Plain-text reference lists: one reference per line (e.g. a literature-search shortlist).
        yield from (line.strip() for line in path.read_text(encoding='utf-8').splitlines())
        return
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read('word/document.xml'))
        rels = ET.fromstring(archive.read('word/_rels/document.xml.rels'))
        links = {r.get('Id'): r.get('Target', '') for r in rels
                 if r.get('TargetMode') == 'External'}
        for p in root.iter(W + 'p'):
            text = ''.join(n.text or '' for n in p.iter(W + 't')).strip()
            urls = [links.get(h.get(R + 'id'), '') for h in p.iter(W + 'hyperlink')]
            text += ' ' + ' '.join(u for u in urls if u.startswith(('http://', 'https://')) and u not in text)
            yield text.strip()


def clean_url(value):
    parts = urlsplit(value.rstrip('.,;'))
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith('utm_')])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def extract(text):
    pmids = set(m.group(1) for pattern in api.PMID_PATTERNS for m in pattern.finditer(text))
    dois = set(api.clean_doi(x) for x in api.DOI_RE.findall(text))
    urls = set(clean_url(x) for x in api.URL_RE.findall(text)
               if not re.match(r'https?://(?:dx\.)?doi\.org/', x, re.I))
    return pmids, dois, urls


def plan_document(path):
    rows = []
    for paragraph in paragraphs(path):
        if not paragraph or paragraph.casefold() in ('references', 'bibliography'):
            continue
        # A lower-case continuation after a broken paragraph is not a new citation.
        if rows and re.match(r'^[a-z]', paragraph):
            rows[-1] += ' ' + paragraph
        else:
            rows.append(paragraph)
    records, review, seen = [], [], set()
    duplicates = 0
    for text in rows:
        pmids, dois, urls = extract(text)
        if len(pmids) > 1 or len(dois) > 1 or (not pmids and not dois and len(urls) > 1):
            review.append({'citation': text, 'reason': 'Multiple identifiers: check reference boundaries'})
            continue
        mode = 'pmid' if pmids else 'doi' if dois else 'url' if urls else None
        if not mode:
            review.append({'citation': text, 'reason': 'No PMID, DOI or URL'})
            continue
        record = dict(citation=text, pmid=next(iter(pmids), None), doi=next(iter(dois), None), url=next(iter(urls), None), mode=mode)
        tokens = {(k, record[k]) for k in ('pmid', 'doi', 'url') if record[k]}
        if tokens & seen:
            duplicates += 1
            continue
        seen.update(tokens)
        record['id'] = hashlib.sha256((mode + ':' + record[mode]).encode()).hexdigest()
        records.append(record)
    return dict(source=str(path), records=records, review=review, duplicates=duplicates)


def upload(plan, collection, state_path, visible):
    key = os.environ.get('ZOTERO_API_KEY') or (input('Zotero API key (visible): ') if visible else getpass.getpass('Zotero API key (hidden): '))
    identity = api.authenticate(key.strip(), ('library', 'write'))
    client = api.zotero.Zotero(identity['userID'], 'user', key.strip())
    destination = api.find_collection(client, collection)['key']
    context = dict(source=plan['source'], user_id=identity['userID'], collection_key=destination)
    state = api.load_json(state_path, dict(context=context, completed={}, failed={}))
    if state.get('context') != context:
        raise RuntimeError('State belongs to a different document, account or collection')
    indices = {k: {} for k in ('pmid', 'doi', 'url')}
    for item in client.everything(client.collection_items_top(destination)):
        data = item['data']
        for k in ('doi', 'url'):
            value = data.get('DOI' if k == 'doi' else k)
            if value:
                indices[k][value.lower() if k == 'doi' else value] = item['key']
        for match in re.finditer(r'PMID:\s*(\d+)', data.get('extra', ''), re.I):
            indices['pmid'][match.group(1)] = item['key']
    pending = list(plan['records'])
    rate_limit_waits = {}
    sent = 0
    while pending:
        record = pending.pop(0)
        rid = record['id']
        if rid in state['completed']:
            continue
        try:
            existing = next((indices[k][record[k]] for k in indices if record.get(k) in indices[k]), None)
            metadata = None
            if not existing:
                if record['pmid']:
                    metadata = api.pubmed_item(record['pmid'], destination, record['citation'])
                    if not metadata:
                        raise ValueError('PMID title not verified; review required')
                elif record['doi']:
                    metadata = api.crossref_item(record['doi'], destination, record['citation'])
                    if not metadata:
                        raise ValueError('DOI metadata/title not verified; review required')
                else:
                    metadata = api.webpage_item(record['url'], record['citation'], destination)
                resolved_doi = metadata.get('DOI', '').lower()
                existing = indices['doi'].get(resolved_doi) if resolved_doi else None
                if not existing:
                    template = client.item_template(metadata['itemType'])
                    metadata = {k: v for k, v in metadata.items() if k in template}
                    metadata['extra'] = (metadata.get('extra', '') + '\nSource citation: ' + record['citation']).strip()
                    existing = api.created_key(client.create_items([metadata]))
                    if resolved_doi:
                        indices['doi'][resolved_doi] = existing
                    status = 'created'
                else:
                    status = 'existing'
            else:
                status = 'existing'
            for k in indices:
                if record.get(k):
                    indices[k][record[k]] = existing
            state['completed'][rid] = dict(key=existing, status=status, citation=record['citation'])
            state['failed'].pop(rid, None)
            api.write_json(state_path, state)
            print(status + ': ' + record['citation'][:100], flush=True)
            time.sleep(0.4)
            if status == 'created':
                sent += 1
                api.pace_batch(sent)
        except ValueError as exc:
            state['failed'][rid] = dict(citation=record['citation'], error=str(exc))
            api.write_json(state_path, state)
            print('Review: ' + str(exc) + ' — ' + record['citation'][:80])
        except (api.zotero_errors.TooManyRetriesError, api.zotero_errors.TooManyRequestsError):
            # Rate limited: wait, then retry this record; progress so far is saved.
            api.write_json(state_path, state)
            waits = rate_limit_waits.get(rid, 0) + 1
            rate_limit_waits[rid] = waits
            if waits > api.RATE_LIMIT_RETRIES:
                raise
            print(f'Zotero rate limit; waiting {60 * waits}s, then retrying', flush=True)
            time.sleep(60 * waits)
            pending.insert(0, record)
        except Exception:
            # Stop on network/API failures: reconcile remote items before a retry.
            api.write_json(state_path, state)
            raise
    print(f"Completed records: {len(state['completed'])}; failed verification: {len(state['failed'])}; manual review: {len(plan['review'])}")
    print('State:', state_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--collection', required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--upload', action='store_true')
    parser.add_argument('--visible-key', action='store_true')
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in ('.docx', '.txt', '.md'):
        raise SystemExit(f'Source must be a .docx, .txt or .md file: {source}')
    plan = plan_document(source)
    project = hashlib.sha256((str(source) + '\n' + args.collection).encode()).hexdigest()[:20]
    folder = args.work_dir / ('docx-' + project)
    api.write_json(folder / 'plan.json', plan)
    counts = {mode: sum(r['mode'] == mode for r in plan['records']) for mode in ('pmid', 'doi', 'url')}
    print('Unique candidates:', len(plan['records']), counts, 'Duplicates:', plan['duplicates'], 'Manual review:', len(plan['review']))
    print('Plan:', folder / 'plan.json')
    for entry in plan['review']:
        print('Review:', entry['reason'], '—', entry['citation'])
    if args.upload:
        upload(plan, args.collection, folder / 'state.json', args.visible_key)
    else:
        print('Preview only; no Zotero changes. Add --upload to import.')


if __name__ == '__main__':
    main()

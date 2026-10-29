# Zotero Reference Import: an AI agent skill

Get your references into Zotero without typing them in one by one. This skill works in **Claude** (Claude desktop, Claude Code, Claude.ai) and **OpenAI Codex**. It imports references into the Zotero collection you choose, from:

| You have… | What happens |
|---|---|
| A **Word bibliography** (`.docx`) | Each reference is resolved by **PMID → DOI → URL** and created as a full Zotero record (authors, journal, year, volume, pages) |
| A **plain-text reference list** (`.txt` / `.md`), e.g. a literature-search shortlist | Same as Word, one reference per line |
| A **single research PDF** | A PubMed- or CrossRef-verified record is created and the PDF is attached |
| A **folder of research PDFs** (subfolders included) | Either every PDF is uploaded and attached to its record, **or** (faster, for citations only) the PDFs are turned into a verified reference list that is imported like a Word bibliography |

Metadata comes from **PubMed** and **CrossRef**. Each match is checked against the paper's title before anything is created, so a typo or a wrong DOI printed in a PDF can't silently import the wrong paper.

## Safety by design

- **Preview first.** Nothing is written to Zotero unless you add `--upload`.
- **Your API key is never stored.** It is typed at a terminal prompt (or read from `ZOTERO_API_KEY`) and is never written to scripts, logs or progress files.
- **Resumable.** Progress is saved after every item, so you can re-run the same command after an interruption. References already in the collection (matched by PMID, DOI or URL) are skipped, not duplicated.
- **Gentle on Zotero.** Uploads pause between batches and wait out Zotero's rate limit instead of failing.
- **Nothing is deleted or merged.** Mismatches and references with no identifier are listed for you to review by hand.

---

## Step by step (tested from start to finish)

### 1. Download this repository

Click **Code → Download ZIP** on GitHub and unzip it (or `git clone` it). You now have a folder containing `zotero-reference-import/`, the skill itself.

Keep your references in a folder of their own, for example:

```
My_References/
├── References.docx          ← a Word bibliography (optional)
└── PDFs/                    ← a subfolder of research PDFs (optional)
```

### 2. Create the Zotero collection on the **web library**

Go to <https://www.zotero.org/mylibrary> and create the collection (folder) there, for example `Thesis_Ch2`.

> **Why the web?** The importer talks to Zotero's **web** library, not the desktop app. A collection you create in Zotero desktop only becomes visible once desktop has synced, which is not instant. If you created the collection in Zotero desktop, click the green **sync** arrow and check that it appears at zotero.org/mylibrary before you import.

The collection must be in **My Library** (group libraries are not supported yet). Capital letters don't matter, but the spelling must match.

### 3. Create your Zotero API key

1. Sign in at <https://www.zotero.org> (the same account your Zotero desktop app syncs with).
2. Go to **Settings → Security**, and under **Applications** click **Create new private key** (direct link: <https://www.zotero.org/settings/keys>).
3. Name it, e.g. `Zotero Reference Import`.
4. Under **Personal Library**, tick **Allow library access** and **Allow write access**, and **Allow file access** if it is offered (needed to upload PDFs).
5. Leave *Default Group Permissions* as **None** and click **Save Key**.
6. **Copy the key now and keep it somewhere private.** Zotero shows a key only once; after you leave that page it can't be shown again. If you lose it, just create a new key (and delete the old one); deleting a key never affects your library.

Good places to keep it: a password manager, or the macOS Keychain:

```bash
security add-generic-password -a "$USER" -s zotero-api-key -w
```

(It asks for the key twice; nothing appears as you paste.)

Everyone uses their own key. This repository contains no API key.

### 4. Add the skill to Claude

**Claude desktop (Chat / Cowork):** open **Customize → Skills**, and add the skill: upload the `zotero-reference-import` folder as a zip, or its `SKILL.md`, as the app offers.

**Claude Code (the Code tab in Claude desktop, or the terminal):** add **both folders** to the session:

1. the downloaded repository folder (which contains `zotero-reference-import/`), and
2. the folder containing your references.

Claude can then read the skill and your files in the same session. (Alternatively, copy `zotero-reference-import` to `~/.claude/skills/zotero-reference-import/` so it is available in every session.)

**OpenAI Codex:** copy `zotero-reference-import` to `~/.codex/skills/zotero-reference-import/`.

You need **Python 3.10+**. The agent creates a virtual environment and installs the requirements for you. To do it yourself:

```bash
python3 -m venv .venv && .venv/bin/pip install -r zotero-reference-import/scripts/requirements.txt
```

### 5. Ask Claude to import

Name the folder or file and the exact collection, for example:

> Upload all the references in `/Users/me/My_References` (the Word document and the PDFs in its subfolder) into my Zotero collection **Thesis_Ch2**.

Claude first runs a **preview** and shows you what it found: how many references resolve by PMID or DOI, duplicates, and anything that needs manual entry. Nothing is written to Zotero yet.

### 6. Paste your API key in the terminal

For the upload, Claude opens a terminal (in Claude Code, a tab in the Terminal panel) and runs the upload command. The script asks:

```
Zotero API key (hidden):
```

Paste your key there and press **Return**. Nothing appears as you paste; that is normal. If you'd rather see it, the command can use `--visible-key`. **Paste the key only at this terminal prompt, never into the chat with Claude.**

The upload then starts and prints one line per item.

### 7. If Zotero says "rate limit", let it wait

Zotero limits how many requests one API key may send in a short time. In testing, uploads started being refused after about **28 PDFs**. Each PDF upload takes about five requests (create the record, create the attachment, ask to upload, send the file, confirm it).

The scripts handle this for you:

- they pause **3 minutes after every 20 items**, and
- if Zotero still refuses, they print `Zotero rate limit; waiting 60s, then retrying` and wait 1, 2, 3, 4, then 5 minutes before giving up on that item.

**Leave it running.** If it gives up anyway, wait about an hour and run the same command again: finished items are skipped. Restarting straight away doesn't help, because the limit is on Zotero's side and every new attempt counts as another request.

### 8. Many PDFs? Import them as a reference list instead

If you only need the **citations** (for citing and reference management, not the PDF files in Zotero), turn the PDF folder into a verified reference list and import that. It is much faster, uses about one Zotero request per reference instead of about five, and uses **no Zotero storage**.

Ask Claude:

> Make a reference list from the PDFs in `/Users/me/My_References/PDFs` and import it into **Thesis_Ch2**.

or run it yourself (no API key needed for this step):

```bash
.venv/bin/python zotero-reference-import/scripts/zotero_pdf_reference_list.py \
  --source "/Users/me/My_References/PDFs" --work-dir "./zotero-imports"
```

For each PDF it finds the PMID or DOI printed in the file. If there is none, or it is wrong, it searches PubMed and CrossRef by title, and keeps a match only when the title agrees. It writes `PDFs_references.docx` (one numbered reference per line, unmatched PDFs last) and a per-file report. Then import the list like any Word bibliography (step 5, or **Running the scripts directly** below).

If a PDF upload already ran part-way, add `--skip-uploaded <work-dir>/<plan-folder>/state.json` so the list contains only the PDFs that did not upload.

You can attach PDFs later in Zotero desktop if you want them: drag a PDF onto its record, or right-click the record → **Add Attachment → Linked File** (stays on your computer, uses no Zotero storage).

### 9. Finish up

- **Sync Zotero desktop** to see everything.
- Add the references Claude listed as **manual review** by hand (they had no PMID, DOI or URL: e.g. web articles, guidelines, conference posters).
- Look over anything flagged *"several records share this title"* (for example a conference abstract and the full paper).
- If your key was ever shown on screen, pasted somewhere it shouldn't be, or in a screenshot, delete it at zotero.org/settings/keys and create a new one.

---

## Running the scripts directly

All commands preview unless `--upload` is added. Use absolute paths.

```bash
# Word / text bibliography: preview, then upload
.venv/bin/python zotero-reference-import/scripts/zotero_docx_import.py \
  --source "/path/References.docx" --collection "My_Collection" --work-dir "./zotero-imports"
.venv/bin/python zotero-reference-import/scripts/zotero_docx_import.py \
  --source "/path/References.docx" --collection "My_Collection" --work-dir "./zotero-imports" --upload

# PDFs → verified reference list (no key needed), then import it as above
.venv/bin/python zotero-reference-import/scripts/zotero_pdf_reference_list.py \
  --source "/path/Research PDFs" --work-dir "./zotero-imports"

# Upload the PDFs themselves, attached to their records: preview, then upload
.venv/bin/python zotero-reference-import/scripts/zotero_api_import.py \
  --source "/path/Research PDFs" --collection "My_Collection" --work-dir "./zotero-imports" \
  --rebuild-plan --resolve-pmids --no-pmid-only
.venv/bin/python zotero-reference-import/scripts/zotero_api_import.py \
  --source "/path/Research PDFs" --collection "My_Collection" --work-dir "./zotero-imports" \
  --no-pmid-only --upload
```

PDF upload options:

| Option | What it does |
|---|---|
| *(default)* | Only PDFs with a PMID printed in the file. Safest, but in testing that was only 13 of 57 PDFs |
| `--resolve-pmids` | Look up PMIDs for the other PDFs on PubMed (by DOI, then title); saved into the plan |
| `--no-pmid-only` | Import every PDF: by PMID, then DOI; any PDF still unmatched is attached without metadata (use Zotero desktop's *Retrieve Metadata for PDF* on it). Non-PDF files in the folder are ignored |
| `--rebuild-plan` | Re-read the folder after adding or removing files |
| `--visible-key` | Show the key as you paste it |

Avoid `--extract-urls` for browser-saved PDFs: the URLs printed in them are usually journal home pages, not the article.

## Troubleshooting

| Message or symptom | Cause and fix |
|---|---|
| `403 Forbidden` at `/keys/current` | Zotero rejected the key. Usually the paste was incomplete; try `--visible-key` to see what you paste (a key is 24 characters). Otherwise create a new key |
| `Expected one Zotero collection named …; found 0` | The collection isn't in the **web** library yet. Create it at zotero.org/mylibrary, or sync Zotero desktop and check it appears there. The message lists the closest collection names |
| `429 Too Many Requests` / `Zotero rate limit; waiting …` | Zotero's rate limit. Leave it running; if it gives up, wait about an hour and run the same command again. For big PDF folders use the reference-list route (step 8) |
| `API key lacks required … permissions` | Edit the key at zotero.org/settings/keys and enable write (and file) access |
| Only some PDFs were selected (e.g. 13 of 57) | By default only PDFs with a PMID printed in them are imported. Add `--resolve-pmids --no-pmid-only`, or use the reference-list route |
| `Review: DOI metadata/title not verified` | The DOI points to a different paper (e.g. a wrong or truncated DOI printed in the PDF). Run `zotero_pdf_reference_list.py`, which finds the correct one by title, or fix the DOI in your list |
| PDFs fail with a storage or quota error | Your Zotero storage is full (the free plan is 300 MB; see zotero.org/settings/storage). Upgrade, or import citations only (step 8) |
| `already present` for a PDF you don't see attached | An earlier interrupted run may have created the attachment entry without the file. Check that item in Zotero; delete the empty attachment and re-run if needed |
| The API key is no longer shown on zotero.org | Zotero shows a key only once. Create a new key, and save it (step 3) |
| `externally-managed-environment` | Install inside a virtual environment (step 4) |

If you ever share a screenshot or a file that shows your API key, revoke the key and create a new one.

## Limitations

- The destination collection must already exist in your **personal** web library. Group libraries are not supported yet.
- Word and reference-list imports create bibliographic records; they don't attach PDFs.
- URL-only references are saved as web pages titled with your citation text, without independent metadata checks.
- Title matching is a heuristic. Review the "needs review" and "several records share this title" lists.
- Zotero's rate limits aren't published; the batch size and pauses are based on testing and may need adjusting for very large imports.

## What's new in this version

From a full end-to-end test run (a Word bibliography plus a folder of 57 PDFs):

- **New `zotero_pdf_reference_list.py`:** turns a PDF folder into a verified Word/text reference list. Finds missing or wrong DOIs by PubMed/CrossRef title search, skips duplicate PDFs, rejects look-alikes (comments, supplements, journal-level DOIs) and flags titles shared by several records.
- **Rate limits handled:** pauses after every 20 items, waits and retries on `429` (including the key check), and the startup scan of the collection uses a single listing instead of one request per item, which used up the limit before uploading began.
- **`--no-pmid-only` now works.** It previously could not be switched off, so most PDFs were never imported. Non-PDF files in the folder are ignored.
- **Stricter title check:** a one-word title (e.g. a journal's DOI resolving to *"Cancer"*) no longer counts as a match. PMIDs shorter than 6 digits are recognised.
- **Clearer collection errors:** "collection not found" now lists your closest collection names and explains the web-sync requirement.
- **README rewritten** as a step-by-step guide, including the Claude desktop **Customize → Skills** setup and adding both folders in Claude Code.

## License

MIT. Contributions and issues are welcome.

# pii-redactor

An offline tool that redacts PII from **PDF** and **Word (.docx)** files using the
**Azure AI Language PII detection container**, which runs on your own machine. Document
text is sent only to `localhost` (or your private network). It never goes to the Azure cloud.

```
 your files ──► pii-redact (Python) ──► http://localhost:5000  (Azure PII container)
                     │                         ▲ detects entities
                     ▼
         *.redacted.pdf / *.redacted.docx  +  *.report.json
```

## Quick start without Docker (cloud mode)

If you can't run Docker, the tool can call the Azure Language **cloud** API directly.
**This is not offline: document text is sent to your Azure Language resource**
(processed in your chosen region and not stored by the PII service).

1. In the Azure Portal, create a **Language service** resource (Standard S tier), then copy
   *Endpoint* and *KEY 1* from **Keys and Endpoint**.
2. Put them in `.env` in the project folder:
   ```ini
   AZURE_LANGUAGE_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com/
   AZURE_LANGUAGE_KEY=<KEY 1>
   ```
3. Install and run:
   ```bash
   pip install -r requirements.txt
   python -m pii_redactor.cli my_docs -o redacted --cloud
   ```

Every other option works the same. Behind a corporate proxy, set `HTTPS_PROXY` and the
tool will use it in cloud mode. In container mode it never uses a proxy.

## What "offline" means here

| Container mode | Document text leaves the machine? | Internet needed? | Requirement |
|---|---|---|---|
| **Connected** | No | Yes, for billing/metering only | Any Language resource (S tier) |
| **Disconnected** | No | No (fully air-gapped after a one-time license download) | Microsoft approval for disconnected containers + a commitment-tier plan |

If the machine must have **zero** network access, you need the disconnected container.
Apply via the *"Request access to disconnected containers"* form in the Azure docs.

The tool also refuses any endpoint that isn't loopback, private-IP or a single-label host
(for example the docker service name `pii`), so text can't be sent to the cloud by accident.
`--allow-remote` overrides this check.

## Setup

```bash
pip install -r requirements.txt     # or: pip install .
cp .env.example .env                # fill in the Language resource endpoint + key
```

### Run the container: connected mode
```bash
cd container && docker compose --env-file ../.env --profile connected up -d
curl http://localhost:5000/ready
```

### Run the container: disconnected / air-gapped mode
```bash
cd container
docker compose --env-file ../.env --profile license run --rm pii-license   # once, while online
docker compose --profile disconnected up -d                                 # then offline
```
Usage logs are written to `container/logs/`. Upload them to Azure as your commitment plan requires.

To move the image to an air-gapped host: `docker save` it on a connected machine, then
`docker load` it on the target.

## Usage

```bash
python -m pii_redactor.cli input_docs/ -o redacted/
python -m pii_redactor.cli contract.docx scan.pdf --mode mask --ocr
python -m pii_redactor.cli records/ --domain phi --exclude Organization,DateTime --min-confidence 0.8
```

| Option | Meaning |
|---|---|
| `--mode category\|mask\|redacted` | DOCX replacement text `[Person]` / `*****` / `[REDACTED]`; also the label on the PDF black box |
| `--categories Person,Email,...` | only detect these categories (default: all) |
| `--exclude Organization,DateTime` | detect these categories but don't redact them |
| `--allow "Contoso Ltd,Support Desk"` | exact strings that are never redacted |
| `--domain phi` | add health-information (PHI) entities |
| `--min-confidence 0.5` | drop low-confidence detections |
| `--ocr` | OCR scanned PDF pages with Tesseract (install `tesseract-ocr`) |
| `--report none\|summary\|full` | `full` writes the detected values into the JSON report (**this file contains PII**) |
| `--endpoint` / `PII_ENDPOINT` | container URL (default `http://localhost:5000`) |

## What gets redacted

**PDF**: the text is actually removed from the page content, not just covered by a box, so
copy/paste and text extraction can't recover it. Image pixels under a box are blanked too.
The tool also removes links that point to PII (for example `mailto:`), deletes comments that
contain PII, and clears the Info/XMP metadata.

**DOCX**: text is replaced inside the existing runs, so formatting is kept, including names
split across several runs. It covers the body, tables, text boxes, headers, footers,
footnotes, endnotes, comments, tracked deletions, field codes and hyperlink targets. Author
names on comments and revisions, and the core document properties, are cleared.

## Known limitations (check these by hand)
- Text inside **images** in a DOCX isn't analysed. Scanned PDFs are only analysed with `--ocr`.
- PDF **form-field values** and **bookmarks** aren't redacted (a warning is printed).
- Legacy `.doc` isn't supported. Convert it first: `soffice --headless --convert-to docx file.doc`.
- Detection quality depends on the Azure model. A human should review sensitive outputs.

## Tests (no Azure needed)
`tests/mock_language_server.py` imitates the container's API, so the full pipeline runs in CI:
```bash
pip install pytest && python -m pytest -q
```

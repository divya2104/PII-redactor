import threading
from pathlib import Path

import docx
import pymupdf
import pytest

from pii_redactor.cli import main
from pii_redactor.client import OfflineViolation, LanguagePiiClient, chunk_text

from tests.mock_language_server import serve

PORT = 5055
OUT = Path(__file__).parent / "_out"
PII = ["John Smith", "jane.doe@example.com", "123-45-6789", "(555) 123-4567", "Priya Sharma"]


@pytest.fixture(scope="session", autouse=True)
def mock_server():
    threading.Thread(target=serve, args=(PORT,), daemon=True).start()
    import time; time.sleep(0.3)


def make_pdf(path: Path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Patient: John Smith", fontsize=12)
    page.insert_text((72, 100), "Email: jane.doe@example.com  SSN: 123-45-6789", fontsize=11)
    page.insert_text((72, 128), "Call (555) 123-4567 about the invoice.", fontsize=11)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 90, 250, 104),
                      "uri": "mailto:jane.doe@example.com"})
    page2 = doc.new_page()
    long = ("Filler sentence for chunking. " * 300) + "Signed, Priya Sharma."
    page2.insert_textbox(pymupdf.Rect(36, 36, 576, 800), long, fontsize=4)
    doc.set_metadata({"author": "John Smith", "title": "Medical record"})
    doc.save(path)


def make_docx(path: Path):
    d = docx.Document()
    p = d.add_paragraph("Customer ")
    p.add_run("John").bold = True          # name split across runs
    p.add_run(" Smith").italic = True
    p.add_run(" can be reached at jane.doe@example.com.")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text = "SSN"
    t.cell(0, 1).text = "123-45-6789"
    d.sections[0].header.paragraphs[0].text = "Prepared for Priya Sharma"
    d.sections[0].footer.paragraphs[0].text = "Phone (555) 123-4567"
    d.core_properties.author = "John Smith"
    d.save(path)


def run_cli(*files):
    return main([*map(str, files), "-o", str(OUT), "--endpoint", f"http://127.0.0.1:{PORT}",
                 "--report", "full"])


def test_pdf_redaction(tmp_path):
    src = tmp_path / "record.pdf"; make_pdf(src)
    assert run_cli(src) == 0
    out = pymupdf.open(OUT / "record.redacted.pdf")
    text = "".join(p.get_text() for p in out)
    for s in PII:
        assert s not in text, s
    assert "Patient:" in text and "invoice" in text and "Filler sentence" in text
    assert not out.metadata.get("author")
    assert not out[0].get_links()


def test_docx_redaction(tmp_path):
    src = tmp_path / "letter.docx"; make_docx(src)
    assert run_cli(src) == 0
    d = docx.Document(OUT / "letter.redacted.docx")
    body = "\n".join(p.text for p in d.paragraphs)
    cells = d.tables[0].cell(0, 1).text
    hdr = d.sections[0].header.paragraphs[0].text
    ftr = d.sections[0].footer.paragraphs[0].text
    blob = "\n".join([body, cells, hdr, ftr])
    for s in PII:
        assert s not in blob, s
    assert "[Person]" in body and "[Email]" in body
    assert "Customer" in body and "Prepared for" in hdr
    assert d.core_properties.author == ""
    # formatting of the first run of the split name survives
    assert d.paragraphs[0].runs[1].bold


def test_offline_guard():
    with pytest.raises(OfflineViolation):
        LanguagePiiClient(endpoint="https://myres.cognitiveservices.azure.com")
    LanguagePiiClient(endpoint="http://pii:5000")       # docker service name ok
    LanguagePiiClient(endpoint="http://10.1.2.3:5000")  # private network ok


def test_chunking_respects_limit():
    text = "word " * 5000
    chunks = list(chunk_text(text))
    assert all(len(c) <= 5000 for _, c in chunks)
    assert "".join(c for _, c in chunks) == text


def test_cloud_mode_uses_env(tmp_path, monkeypatch):
    # Simulates --cloud with endpoint/key from .env (mock server stands in for Azure)
    monkeypatch.setenv("AZURE_LANGUAGE_ENDPOINT", f"http://127.0.0.1:{PORT}/")
    monkeypatch.setenv("AZURE_LANGUAGE_KEY", "dummy-key")
    src = tmp_path / "c.docx"; make_docx(src)
    assert main([str(src), "-o", str(OUT), "--cloud"]) == 0
    assert "[Person]" in docx.Document(OUT / "c.redacted.docx").paragraphs[0].text


def test_cloud_mode_requires_credentials(monkeypatch):
    monkeypatch.delenv("AZURE_LANGUAGE_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_LANGUAGE_KEY", raising=False)
    monkeypatch.setattr("pii_redactor.cli.load_dotenv", lambda: None)  # ignore a real .env
    assert main(["x.pdf", "--cloud"]) == 2

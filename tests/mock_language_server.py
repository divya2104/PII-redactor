"""Tiny stand-in for the PII container (regex based) so the pipeline can be tested
without Azure. Implements GET /ready and POST /language/:analyze-text."""
import json, re, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATTERNS = [
    ("Email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("USSocialSecurityNumber", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PhoneNumber", re.compile(r"\(?\b\d{3}\)?[ -]\d{3}-\d{4}\b")),
    ("Person", re.compile(r"\b(?:John Smith|Jane Doe|Priya\s+Sharma)\b")),
]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._send(200 if self.path == "/ready" else 404, {"ready": True})
    def do_POST(self):
        if not self.path.startswith("/language/:analyze-text"):
            return self._send(404, {})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        docs = []
        for d in body["analysisInput"]["documents"]:
            assert len(d["text"]) <= 5120, "document too long"
            ents = []
            for cat, rx in PATTERNS:
                for m in rx.finditer(d["text"]):
                    ents.append({"text": m.group(), "category": cat, "offset": m.start(),
                                 "length": len(m.group()), "confidenceScore": 0.9})
            docs.append({"id": d["id"], "entities": ents, "warnings": []})
        self._send(200, {"kind": "PiiEntityRecognitionResults",
                         "results": {"documents": docs, "errors": [], "modelVersion": "mock"}})

def serve(port=5055):
    s = ThreadingHTTPServer(("127.0.0.1", port), H); s.serve_forever()

if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 5055)

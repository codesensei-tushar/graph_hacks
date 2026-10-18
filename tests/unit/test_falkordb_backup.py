"""deploy/backup/falkordb-backup.py: the count comparison and the GCS upload, without Docker or GCP.

The full backup and restore round trip needs a FalkorDB container; docs/DEPLOY.md,
"Backups and restore", says how to run it locally.
"""

import base64
import hashlib
import importlib.util
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "backup" / "falkordb-backup.py"
spec = importlib.util.spec_from_file_location("falkordb_backup", SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def counts(person=2, works=1):
    return {"brain": {"node_total": person + 1, "edge_total": works, "nodes": {"Person": person, "Project": 1}, "edges": {"WORKS_ON": works}}}


def test_identical_counts_pass():
    assert backup.compare(counts(), counts()) == []


def test_every_difference_is_reported():
    restored = counts(person=3)
    restored["extra"] = counts()["brain"]
    problems = backup.compare(counts(), restored)
    assert "brain: node_total 3 expected, 4 restored" in problems
    assert "brain: nodes :Person 2 expected, 3 restored" in problems
    assert "extra: in the restore but not in the manifest" in problems
    assert backup.compare(counts(), {}) == ["brain: missing from the restore"]


def test_a_label_missing_on_either_side_is_a_mismatch():
    restored = counts()
    restored["brain"]["nodes"]["Ghost"] = 0
    assert backup.compare(counts(), restored) == ["brain: nodes :Ghost None expected, 0 restored"]
    del restored["brain"]["nodes"]["Ghost"], restored["brain"]["edges"]["WORKS_ON"]
    assert backup.compare(counts(), restored) == ["brain: edges :WORKS_ON 1 expected, None restored"]


def test_counts_that_moved_during_the_snapshot_pass_within_their_range():
    before, after = counts(person=2, works=1), counts(person=6, works=4)
    assert backup.compare(before, counts(person=4, works=2), after) == []
    assert backup.compare(before, counts(person=7, works=2), after) == [
        "brain: node_total 3..7 expected, 8 restored",
        "brain: nodes :Person 2..6 expected, 7 restored",
    ]


def test_cypher_identifiers_are_backquoted():
    assert backup.ident("Weird Label") == "`Weird Label`"


class FakeGCS(BaseHTTPRequestHandler):
    received: list = []
    corrupt = False

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        FakeGCS.received.append((self.path.split("?")[0], query, self.headers["Authorization"], body))
        md5 = hashlib.md5(body + (b"x" if FakeGCS.corrupt else b"")).digest()
        reply = json.dumps({"name": query["name"][0], "md5Hash": base64.b64encode(md5).decode()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def gcs(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), FakeGCS)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakeGCS.received, FakeGCS.corrupt = [], False
    monkeypatch.setattr(backup, "UPLOAD_URL", f"http://127.0.0.1:{server.server_port}/upload/storage/v1/b/{{bucket}}/o")
    yield FakeGCS
    server.shutdown()


def test_upload_never_overwrites_and_checks_the_md5(gcs, tmp_path):
    rdb = tmp_path / "x.rdb"
    rdb.write_bytes(b"REDIS0011" + bytes(range(256)) * 50)
    backup.upload("bkt", "falkordb/2026/10/18/x.rdb", str(rdb), "application/octet-stream", "tok")
    path, query, auth, body = gcs.received[0]
    assert path == "/upload/storage/v1/b/bkt/o"
    assert query == {"uploadType": ["media"], "name": ["falkordb/2026/10/18/x.rdb"], "ifGenerationMatch": ["0"]}
    assert auth == "Bearer tok" and body == rdb.read_bytes()


def test_upload_fails_when_gcs_stored_something_else(gcs, tmp_path):
    gcs.corrupt = True
    rdb = tmp_path / "x.rdb"
    rdb.write_bytes(b"REDIS0011")
    with pytest.raises(backup.BackupError, match="does not match"):
        backup.upload("bkt", "x.rdb", str(rdb), "application/octet-stream", "tok")

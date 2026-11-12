"""Use real Git LFS against a tiny local Git/LFS HTTP fixture."""

import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from scripts.sync import GitMirror, SyncError


@unittest.skipUnless(shutil.which("git-lfs"), "Git LFS is required")
class GitLFSIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_objects = {}
        self.destination_objects = {}
        self.requests = []
        fixture = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(fixture.root), **kwargs)

            def log_message(self, *args):
                pass

            def reply(self, status, payload, content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                path = urlsplit(self.path).path
                fixture.requests.append(("GET", path))
                if path.startswith("/lfs/source/"):
                    oid = path.rsplit("/", 1)[-1]
                    if oid in fixture.source_objects:
                        self.reply(200, fixture.source_objects[oid], "application/octet-stream")
                    else:
                        self.reply(404, b"Object not found", "text/plain")
                else:
                    super().do_GET()

            def do_POST(self):
                path = urlsplit(self.path).path
                fixture.requests.append(("POST", path))
                if path not in (
                    "/source.git/info/lfs/objects/batch",
                    "/destination.git/info/lfs/objects/batch",
                ):
                    self.reply(404, b"{}")
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                objects = []
                for item in payload["objects"]:
                    oid = item["oid"]
                    response = {"oid": oid, "size": item["size"]}
                    if payload["operation"] == "download":
                        if oid in fixture.source_objects:
                            response["actions"] = {
                                "download": {"href": f"{fixture.url}/lfs/source/{oid}"}
                            }
                        else:
                            response["error"] = {"code": 404, "message": "Object not found"}
                    elif oid not in fixture.destination_objects:
                        response["actions"] = {
                            "upload": {"href": f"{fixture.url}/lfs/destination/{oid}"}
                        }
                    objects.append(response)
                self.reply(200, json.dumps({"transfer": "basic", "objects": objects}).encode())

            def do_PUT(self):
                path = urlsplit(self.path).path
                fixture.requests.append(("PUT", path))
                if not path.startswith("/lfs/destination/"):
                    self.reply(404, b"{}")
                    return
                oid = path.rsplit("/", 1)[-1]
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                if hashlib.sha256(payload).hexdigest() != oid:
                    self.reply(422, b"{}")
                    return
                fixture.destination_objects[oid] = payload
                self.reply(200, b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

        self.work = self.root / "work"
        self.environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.git("init", "--initial-branch=main", str(self.work))
        self.git("config", "user.name", "LFS test", cwd=self.work)
        self.git("config", "user.email", "lfs-test@example.invalid", cwd=self.work)
        self.content = bytes(range(256)) * 32
        self.oid = hashlib.sha256(self.content).hexdigest()
        self.source_objects[self.oid] = self.content
        (self.work / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        (self.work / ".lfsconfig").write_text(
            "[lfs]\n"
            f"url = {self.url}/unexpected\n"
            f"pushurl = {self.url}/unexpected\n"
            "allowincompletepush = true\n"
            "skipdownloaderrors = true\n"
            "fetchinclude = never-match-this-file\n"
            "fetchexclude = *.bin\n"
        )
        (self.work / "archive.bin").write_text(
            f"version https://git-lfs.github.com/spec/v1\noid sha256:{self.oid}\nsize {len(self.content)}\n"
        )
        self.git("add", ".", cwd=self.work)
        self.git("commit", "--quiet", "-m", "Track historical LFS object", cwd=self.work)
        self.git("rm", "archive.bin", cwd=self.work)
        self.git("commit", "--quiet", "-m", "Delete file from current tree", cwd=self.work)
        self.git("clone", "--bare", str(self.work), str(self.root / "source.git"))
        self.git("update-server-info", cwd=self.root / "source.git")

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", *args],
            cwd=cwd,
            env=self.environment,
            check=True,
            text=True,
            capture_output=True,
        ).stdout

    def mirror(self):
        return GitMirror(f"{self.url}/source.git", f"{self.url}/destination.git")

    def test_transfers_historical_objects_and_overrides_tracked_lfs_configuration(self):
        for existing_head in (False, True):
            with self.subTest(bare_head_exists=existing_head):
                self.destination_objects.clear()
                self.requests.clear()
                with self.mirror() as mirror:
                    if existing_head:
                        mirror.fetch()
                        mirror._git("symbolic-ref", "HEAD", "refs/heads/main")
                    mirror.transfer_lfs()
                self.assertEqual(self.destination_objects, {self.oid: self.content})
                self.assertIn(("POST", "/source.git/info/lfs/objects/batch"), self.requests)
                self.assertIn(("POST", "/destination.git/info/lfs/objects/batch"), self.requests)
                self.assertFalse(any("unexpected" in path for _, path in self.requests))

    def test_missing_source_lfs_object_fails_instead_of_reporting_success(self):
        self.source_objects.clear()
        with self.mirror() as mirror:
            mirror.fetch()
            mirror._git("symbolic-ref", "HEAD", "refs/heads/main")
            with self.assertRaises(SyncError):
                mirror.transfer_lfs()
        self.assertEqual(self.destination_objects, {})


if __name__ == "__main__":
    unittest.main()

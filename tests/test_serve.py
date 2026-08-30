#!/usr/bin/env python3
"""The range-capable server, which exists because the stdlib one is not.

`SimpleHTTPRequestHandler` has no `Range`, `206` or `Accept-Ranges`, so a viewer
following a growing trace re-downloads it whole on every poll. These tests cover
the contract the client depends on -- and in particular the two cases a
tail-follower hits constantly, which are easy to mistake for errors: a range
starting exactly at the end (nothing new yet) and a range past the end (the file
was replaced).
"""

import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from serve import RangeHandler, parse_open_range


class TestRangeParsing(unittest.TestCase):
    def test_an_open_range_gives_its_start(self):
        self.assertEqual(parse_open_range("bytes=100-", 500), 100)
        self.assertEqual(parse_open_range("bytes=0-", 500), 0)

    def test_whitespace_and_case_are_tolerated(self):
        self.assertEqual(parse_open_range("  Bytes=100-  ", 500), 100)

    def test_a_range_at_exactly_the_end_is_unsatisfiable(self):
        # Not an error: this is what "no new bytes yet" looks like, and the
        # client polls into it once a second forever.
        self.assertEqual(parse_open_range("bytes=500-", 500), -1)

    def test_a_range_past_the_end_is_unsatisfiable(self):
        # Which is how a client discovers the file was truncated or replaced.
        self.assertEqual(parse_open_range("bytes=900-", 500), -1)

    def test_unsupported_forms_fall_back_to_a_whole_body(self):
        # None means "serve 200", which is always a correct answer to a range
        # request -- and keeps the client working against the stdlib server.
        for header in ("bytes=0-99",          # closed range
                       "bytes=-99",           # suffix range
                       "bytes=0-99,200-299",  # multiple ranges
                       "items=0-",            # not bytes
                       "bytes=abc-",          # junk
                       "bytes=",              # empty
                       "",
                       None):
            self.assertIsNone(parse_open_range(header, 500), header)

    def test_an_empty_file_cannot_satisfy_any_range(self):
        self.assertEqual(parse_open_range("bytes=0-", 0), -1)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by default, which would hide the 302 under a 200."""

    def redirect_request(self, *args, **kwargs):
        return None


class TestLiveServer(unittest.TestCase):
    """Against a real socket. The parser tests above prove nothing about status
    codes, headers, transferred byte counts, growth during a transfer, or the
    whole-body fallback -- and those are the whole contract."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.previous = os.getcwd()
        os.chdir(cls.root)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.chdir(cls.previous)
        shutil.rmtree(cls.root, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w") as handle:
            handle.write(text)
        return len(text.encode())

    def get(self, name, byte_range=None, method="GET"):
        url = f"http://127.0.0.1:{self.port}/{name}"
        request = urllib.request.Request(url, method=method)
        if byte_range is not None:
            request.add_header("Range", byte_range)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_a_whole_body_advertises_range_support(self):
        size = self.write("a.jsonl", '{"n":1}\n{"n":2}\n')
        status, headers, body = self.get("a.jsonl")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), size)
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("ETag", headers)

    def test_a_range_transfers_only_the_tail(self):
        self.write("b.jsonl", '{"n":1}\n{"n":2}\n')
        status, headers, body = self.get("b.jsonl", "bytes=8-")
        self.assertEqual(status, 206)
        self.assertEqual(body, b'{"n":2}\n')
        self.assertEqual(headers["Content-Range"], "bytes 8-15/16")
        self.assertEqual(int(headers["Content-Length"]), 8)

    def test_a_range_at_the_end_reports_the_size_and_no_body(self):
        size = self.write("c.jsonl", '{"n":1}\n')
        status, headers, body = self.get("c.jsonl", f"bytes={size}-")
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], f"bytes */{size}")
        self.assertEqual(body, b"")

    def test_appended_bytes_are_served_from_the_old_end(self):
        size = self.write("d.jsonl", '{"n":1}\n')
        with open(os.path.join(self.root, "d.jsonl"), "a") as handle:
            handle.write('{"n":2}\n')
        status, _headers, body = self.get("d.jsonl", f"bytes={size}-")
        self.assertEqual(status, 206)
        self.assertEqual(body, b'{"n":2}\n')

    def test_truncation_is_visible_as_a_smaller_total(self):
        self.write("e.jsonl", "x" * 100)
        offset = 60
        self.write("e.jsonl", "y" * 20)
        status, headers, _body = self.get("e.jsonl", f"bytes={offset}-")
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */20")

    def test_appending_keeps_the_validator(self):
        # The property the follower depends on, and the one the first
        # validator (inode-size-mtime) violated: every growth poll looked
        # like a replacement, so the viewer restarted the replay from zero on
        # every append.
        self.write("h.jsonl", '{"e":"cfg","seed":"1"}\n{"e":"arr"}\n')
        _status, first, _body = self.get("h.jsonl")
        time.sleep(0.01)
        with open(os.path.join(self.root, "h.jsonl"), "a") as handle:
            handle.write('{"e":"arr","o":2}\n')
        _status, second, _body = self.get("h.jsonl")
        self.assertEqual(first["ETag"], second["ETag"])

    def test_replacement_changes_the_validator(self):
        # The case a size comparison cannot see: same length, different run.
        self.write("f.jsonl", "aaaa")
        _status, first, _body = self.get("f.jsonl")
        time.sleep(0.01)
        self.write("f.jsonl", "bbbb")
        _status, second, _body = self.get("f.jsonl")
        self.assertNotEqual(first["ETag"], second["ETag"])

    def test_head_sends_headers_and_no_body(self):
        size = self.write("g.jsonl", '{"n":1}\n')
        status, headers, body = self.get("g.jsonl", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(int(headers["Content-Length"]), size)
        self.assertEqual(body, b"")

    def test_unsupported_range_forms_get_the_whole_body(self):
        size = self.write("h.jsonl", '{"n":1}\n{"n":2}\n')
        for header in ("bytes=0-3", "bytes=-4", "bytes=0-1,4-5", "junk"):
            status, _headers, body = self.get("h.jsonl", header)
            self.assertEqual(status, 200, header)
            self.assertEqual(len(body), size, header)

    def test_a_file_growing_during_a_transfer_sends_only_what_was_promised(self):
        path = os.path.join(self.root, "i.jsonl")
        self.write("i.jsonl", "x" * 5000)
        status, headers, body = self.get("i.jsonl")
        with open(path, "a") as handle:
            handle.write("y" * 5000)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), int(headers["Content-Length"]))
        self.assertEqual(len(body), 5000)

    def test_only_the_viewer_and_traces_are_reachable(self):
        """The served directory is a source tree, so the reachable set is an
        allowlist. A denylist would have to keep pace with whatever the
        repository grows; this test exists because the server once published
        `.git/config` to anything that could reach the port."""
        os.mkdir(os.path.join(self.root, "starksim"))
        with open(os.path.join(self.root, "starksim", "netsim.py"), "w") as f:
            f.write("secret = 1\n")
        os.mkdir(os.path.join(self.root, ".git"))
        with open(os.path.join(self.root, ".git", "config"), "w") as f:
            f.write("[remote]\n")
        self.write("notes.md", "# private\n")
        self.write("viewer.html", "<title>viewer</title>")
        self.write("run.jsonl", '{"e":"cfg"}\n')

        for hidden in ("starksim/netsim.py", ".git/config", "notes.md"):
            with self.subTest(path=hidden):
                status, _, _ = self.get(hidden)
                self.assertEqual(status, 404)
        for allowed in ("viewer.html", "run.jsonl"):
            with self.subTest(path=allowed):
                status, _, _ = self.get(allowed)
                self.assertEqual(status, 200)

    def test_a_symlink_out_of_the_root_is_refused(self):
        # A static link is caught by resolving the name.
        outside = os.path.join(self.root, "..", "outside.jsonl")
        with open(outside, "w") as handle:
            handle.write("secret\n")
        self.addCleanup(os.remove, outside)
        os.symlink(os.path.realpath(outside),
                   os.path.join(self.root, "escape.jsonl"))
        status, _, _ = self.get("escape.jsonl")
        self.assertEqual(status, 404)

    def test_the_bytes_served_are_the_bytes_approved(self):
        # The check and the open are two operations, and a symlink can be
        # repointed between them. Rather than race the server, this asserts the
        # property that makes the race unwinnable: what is opened is confirmed to
        # be the file the allowlist approved, so a descriptor on anything else is
        # refused even when the name still resolves inside the root.
        from serve import open_publishable

        self.write("safe.jsonl", '{"e":"cfg"}\n')
        outside = os.path.join(self.root, "..", "swapped.jsonl")
        with open(outside, "w") as handle:
            handle.write("secret\n")
        self.addCleanup(os.remove, outside)

        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            stream = open_publishable("safe.jsonl")
            self.assertIsNotNone(stream)
            stream.close()

            # The descriptor a swap would have handed back: opened from outside
            # the root while the approved name still looks fine. A module global
            # shadows the builtin, so this substitutes what `open_publishable`
            # calls without touching anything else.
            import builtins
            import serve

            def swapping_open(target, *args, **kwargs):
                return builtins.open(os.path.realpath(outside), *args, **kwargs)

            serve.open = swapping_open
            try:
                self.assertIsNone(open_publishable("safe.jsonl"))
            finally:
                del serve.open
        finally:
            os.chdir(cwd)

    def test_a_directory_never_lists_and_the_root_is_the_viewer(self):
        self.write("viewer.html", "<title>viewer</title>")
        url = f"http://127.0.0.1:{self.port}/"
        request = urllib.request.Request(url, method="GET")
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request) as response:
                status, headers = response.status, dict(response.headers)
        except urllib.error.HTTPError as error:
            status, headers = error.code, dict(error.headers)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/viewer.html")

    def test_a_missing_file_is_a_404(self):
        status, _headers, _body = self.get("nope.jsonl")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

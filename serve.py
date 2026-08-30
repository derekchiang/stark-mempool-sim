#!/usr/bin/env python3
"""
A static file server that honours `Range`, for following a growing trace.

`python -m http.server` does not: `SimpleHTTPRequestHandler` contains no
reference to `Range`, `206` or `Accept-Ranges` and always sends the whole file
with a `Content-Length`. So a viewer polling a 100 MB `trace.jsonl` once a second
re-downloads 100 MB every second. This exists only to close that gap.

    python serve.py            # port 8000, current directory
    python serve.py 8080

It serves the viewer and trace files only. The working directory of a run is a
source tree containing `.git`, so a general-purpose static server pointed at it
publishes the whole repository to anything that can reach the port. Nothing here
needs that, so the reachable set is an allowlist: `viewer.html` and `*.jsonl` in
the served directory, with no directory listings and no traversal into
subdirectories.

The contract the viewer relies on, and the reason each part is here:

- **`Accept-Ranges: bytes`** on every file response, so a client can tell whether
  incremental fetching is available at all rather than guessing from a `206`.
- **`bytes=N-`** open-ended ranges. That is the only form a tail-follower needs,
  and supporting only what is needed keeps the parsing small enough to be
  obviously correct.
- **`416` with `Content-Range: bytes */size`** when `N` is at or past the end.
  Both cases are normal here: `N == size` means "no new bytes yet", and `N > size`
  means the file was truncated or replaced, which is how a client discovers that
  a new run has started and it must reset.
- **`Content-Length` measured once**, before any bytes are sent, and exactly that
  many bytes written. A trace grows *while* the response is being served, so
  reading "to the end" would send more than the header promised.
- **`HEAD`**, so a client can check the size without transferring anything.
- **An `ETag`**, because size alone cannot detect replacement: a run truncated
  and regrown past the client's offset between two polls, or replaced by a
  same-length file, would otherwise be spliced onto the previous one in silence.
- **`Cache-Control: no-store`**, since a cached `200` or `416` is poison for
  something following a live file.

Malformed or unsupported `Range` headers are ignored rather than rejected: a `200`
with the whole body is always a correct answer to a range request, and a client
that must handle `200` anyway is a client that keeps working against the stdlib
server too.
"""

import hashlib
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

READ_CHUNK = 1 << 16


def parse_open_range(header, size):
    """Parse `bytes=N-` into a start offset, or None if it is not that.

    Returns `None` for anything unsupported -- multiple ranges, suffix ranges,
    closed ranges, junk -- because the caller then serves a normal `200`, which
    is always allowed. Returns `-1` for a syntactically valid range that cannot
    be satisfied, which is a `416` and is a *different* answer from "I did not
    understand you".
    """
    if not header:
        return None
    value = header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[len("bytes="):].strip()
    if "," in spec or not spec.endswith("-"):
        return None
    digits = spec[:-1]
    if not digits.isdigit():
        return None
    start = int(digits)
    if start >= size:
        return -1
    return start


def identity(info, handle):
    """A weak validator for one *run*, not one byte-state of the file.

    A follower needs an identity stable across append and different across file
    replacement. The validator therefore combines the inode, creation time
    where available, and a hash of the trace header. A same-length in-place
    rewrite changes the header hash; size checks separately detect truncation.
    """
    position = handle.tell()
    handle.seek(0)
    first_line = handle.readline(65536)
    handle.seek(position)
    header_hash = hashlib.sha256(first_line).hexdigest()[:16]
    born = getattr(info, "st_birthtime", None)
    born_part = "" if born is None else f"-{int(born * 1000):x}"
    return f'W/"{info.st_ino:x}{born_part}-{header_hash}"'


def is_publishable(path):
    """Whether a resolved path is one of the two things a viewer may read.

    The viewer fetches `viewer.html` and a trace by name, and nothing else. An
    allowlist rather than a denylist because the served directory is a source
    tree: a rule that tries to name what to hide has to keep pace with whatever
    the repository grows, while a rule that names what to publish does not.
    Subdirectories are excluded outright, which is what keeps `.git` and the
    package unreachable without enumerating them.

    A judgement about a *name*. Names can change under it, so a caller that goes
    on to read the file must confirm the bytes it opened are the ones this
    approved -- see `open_publishable`.
    """
    root = os.path.realpath(os.getcwd())
    resolved = os.path.realpath(path)
    if os.path.dirname(resolved) != root:
        return False
    name = os.path.basename(resolved)
    return name == "viewer.html" or name.endswith(".jsonl")


def open_publishable(path):
    """Open `path` only if the descriptor obtained is a publishable file.

    Checking the name and then opening it are two operations, and a symlink in
    the served directory can be repointed between them: a request approved as
    `run.jsonl` inside the root would then read whatever the link named at open
    time. Handing back a file the allowlist never saw is exactly the failure the
    allowlist exists to prevent.

    So the descriptor is pinned first and judged afterwards. The open fixes one
    inode; `is_publishable` then re-resolves the name, and the two are required
    to be the same file. Whichever way a swap is timed it fails one of those:
    the resolved name is outside the root, or it is inside but names a different
    inode than the one now held. Returns None if anything does not line up.
    """
    try:
        stream = open(path, "rb")
    except OSError:
        return None
    try:
        if not is_publishable(path):
            stream.close()
            return None
        approved = os.stat(os.path.realpath(path))
        held = os.fstat(stream.fileno())
        if (approved.st_ino, approved.st_dev) != (held.st_ino, held.st_dev):
            stream.close()
            return None
    except OSError:
        stream.close()
        return None
    return stream


class RangeHandler(SimpleHTTPRequestHandler):
    """Serves the viewer and traces with open-ended byte ranges, nothing else."""

    server_version = "starksim-serve/1.0"

    def do_GET(self):
        self._serve(with_body=True)

    def do_HEAD(self):
        self._serve(with_body=False)

    def _serve(self, with_body):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            # No listings: the served directory is a source tree, and a listing
            # of it is the one page this server must never produce. The bare
            # root is the viewer, which is what anyone asking for it wants.
            self.send_response(302)
            self.send_header("Location", "/viewer.html")
            self.end_headers()
            return
        stream = open_publishable(path)
        if stream is None:
            self.send_error(404, "Not Found")
            return
        # Sized from the *open* descriptor, not the path. Between a getsize and
        # an open the file can be replaced, and the length advertised would then
        # belong to a different file than the bytes sent.
        info = os.fstat(stream.fileno())
        size = info.st_size

        with stream:
            start = parse_open_range(self.headers.get("Range"), size)
            if start == -1:
                # At or past the end. Not an error condition in a tail-follower:
                # it is how "nothing new" and "the file was replaced" are told
                # apart from a short read.
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", identity(info, stream))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if start is None:
                first, length, status = 0, size, 200
            else:
                first, length, status = start, size - start, 206

            self.send_response(status)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Accept-Ranges", "bytes")
            # A validator, because size alone cannot detect replacement: a run
            # truncated and regrown past the client's offset between two polls,
            # or replaced by a file of the same length, would otherwise be
            # spliced onto the previous one silently.
            self.send_header("ETag", identity(info, stream))
            # A cached 200 or 416 is poison for something following a live file.
            self.send_header("Cache-Control", "no-store")
            # Measured before a byte is written. The file may be appended to
            # while this response is in flight, and sending more than the
            # promised length would desynchronise the client's byte offset --
            # which is the one piece of state incremental fetching depends on.
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range",
                                 f"bytes {first}-{size - 1}/{size}")
            self.end_headers()
            if not with_body:
                return
            stream.seek(first)
            self._copy_exactly(stream, length)

    def _copy_exactly(self, stream, length):
        remaining = length
        while remaining > 0:
            chunk = stream.read(min(READ_CHUNK, remaining))
            if not chunk:
                # The file shrank mid-response. Nothing honest is left to do:
                # the length is already promised, so stop and let the client's
                # short read surface as a reset rather than pad with lies.
                return
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            remaining -= len(chunk)

    def log_message(self, fmt, *args):
        # One line per poll at one poll a second is noise, and the viewer polls
        # forever. `log_request` passes (requestline, code, size), so the status
        # is the second argument.
        if len(args) > 1 and str(args[1]) in ("200", "206", "416"):
            return
        super().log_message(fmt, *args)


def main():
    port = 8000
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    # Threaded: a single-threaded server lets one large transfer block every
    # other request, and a viewer polling while a big trace streams would stall.
    server = ThreadingHTTPServer(("127.0.0.1", port), RangeHandler)
    print(f"serving {os.getcwd()} on http://127.0.0.1:{port}/ with byte ranges")
    print("open viewer.html there; ^C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""File system and search tools, confined to the configured allowed directories."""
import base64
import difflib
import fnmatch
import http.client
import ipaddress
import os
import re
import shutil
import socket
import stat
import subprocess
import threading
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from xml.etree import ElementTree
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv"}
MAX_LIST_ENTRIES = 1000


class ToolError(Exception):
    """An expected failure reported to the model as a tool error (isError=true)."""


class Files:
    def __init__(self, cfg):
        self.cfg = cfg
        self.refresh()

    def refresh(self):
        self.roots = [os.path.realpath(os.path.expanduser(d)) for d in self.cfg["allowedDirectories"]]

    # -- path guard ---------------------------------------------------------
    def resolve(self, path):
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise ToolError("path must be a non-empty string")
        p = os.path.expanduser(path.strip())
        if not os.path.isabs(p):
            p = os.path.join(self.roots[0] if self.roots else os.path.expanduser("~"), p)
        real = os.path.realpath(p)  # follows symlinks, so a link cannot escape the roots
        if self.roots and not any(_inside(real, root) for root in self.roots):
            raise ToolError("access denied: %s is outside allowed directories %s" % (real, self.roots))
        return real

    def writable(self):
        if self.cfg["readOnly"]:
            raise ToolError("this node is in read-only mode")

    # -- reading ------------------------------------------------------------
    def read_file(self, path, offset=0, length=None, is_url=False, sheet=None):
        if is_url:
            return self._read_url(path, offset, length)
        real = self.resolve(path)
        if os.path.isdir(real):
            raise ToolError("%s is a directory; use list_directory" % real)
        if not os.path.exists(real):
            raise ToolError("file not found: %s" % real)
        if not os.path.isfile(real):  # FIFOs and devices would block or never end
            raise ToolError("%s is not a regular file" % real)
        size = os.path.getsize(real)
        mime = IMAGE_TYPES.get(os.path.splitext(real)[1].lower())
        if mime:
            if size > self.cfg["maxFileSizeBytes"]:
                raise ToolError("image is larger than maxFileSizeBytes")
            with open(real, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            return [{"type": "text", "text": "Image %s (%d bytes)" % (real, size)},
                    {"type": "image", "data": data, "mimeType": mime}]
        ext = os.path.splitext(real)[1].lower()
        if ext in DOCUMENTS:
            if size > self.cfg["maxFileSizeBytes"]:
                raise ToolError("document is larger than maxFileSizeBytes")
            return self._page(DOCUMENTS[ext](self, real, sheet), offset, length)
        if size > self.cfg["maxFileSizeBytes"]:
            raise ToolError("text file is larger than maxFileSizeBytes")
        if _is_binary(real):
            raise ToolError("%s looks like a binary file (%d bytes); use get_file_info" % (real, size))
        length = self._length(length)
        with open(real, encoding="utf-8", errors="replace", newline="") as f:
            if offset < 0:
                tail = deque(f, maxlen=-offset)
                total = None
                lines = list(tail)[:length]
                header = "[last %d lines]" % len(lines)
            else:
                lines, total = [], 0
                for total, line in enumerate(f, 1):
                    if offset < total <= offset + length:
                        lines.append(line)
                start = offset + 1
                end = offset + len(lines)
                header = "" if not offset and end == total else "[lines %d-%d of %d]" % (start, end, total)
                if offset and offset >= total:
                    header = "[offset %d is past the end: file has %d lines]" % (offset, total)
        text = "".join(lines)
        return header + "\n" + text if header else text

    def _length(self, length):
        limit = self.cfg["fileReadLineLimit"]
        return limit if length is None else max(1, min(length, limit))

    def _page(self, text, offset, length):
        """offset/length paging for text that is already in memory (URLs, documents)."""
        lines = text.splitlines(True)
        length = self._length(length)
        total = len(lines)
        part = lines[offset:][:length] if offset < 0 else lines[offset:offset + length]
        if offset < 0:
            header = "[last %d lines of %d]" % (len(part), total)
        elif offset and offset >= total:
            header = "[offset %d is past the end: text has %d lines]" % (offset, total)
        elif offset or len(part) < total:
            header = "[lines %d-%d of %d]" % (offset + 1, offset + len(part), total)
        else:
            header = ""
        body = "".join(part)
        return header + "\n" + body if header else body

    def _read_url(self, url, offset, length):
        if not re.match(r"https?://", url, re.I):
            raise ToolError("only http:// and https:// URLs can be read")
        limit = self.cfg["maxFileSizeBytes"]
        for attempt in (1, 2):
            req = urllib.request.Request(url, headers={"User-Agent": "bridge-commander"})
            try:
                with _OPENER.open(req, timeout=30) as resp:
                    data = resp.read(limit + 1)
                    ctype = resp.headers.get("Content-Type", "")
                    charset = resp.headers.get_content_charset() or "utf-8"
                    status = resp.status
                break
            except ToolError:
                raise
            except Exception as exc:  # URLError, HTTPError, timeouts, bad certificates
                if attempt == 1 and os.name == "nt" and "CERTIFICATE_VERIFY_FAILED" in str(exc):
                    _windows_fetch_root(url)
                    continue
                raise ToolError("cannot fetch %s: %s" % (url, exc))
        if len(data) > limit:
            raise ToolError("response is larger than maxFileSizeBytes")
        mime = ctype.split(";")[0].strip().lower()
        if mime in IMAGE_TYPES.values():
            return [{"type": "text", "text": "Image %s (%d bytes)" % (url, len(data))},
                    {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime}]
        if b"\x00" in data[:8192]:
            raise ToolError("%s returned binary content (%s)" % (url, mime or "unknown type"))
        text = self._page(data.decode(charset, "replace"), offset, length)
        return "[%s %d %s]\n%s" % (url, status, mime, text)

    def read_multiple_files(self, paths):
        parts = []
        used = 0
        for path in paths[:50]:
            try:
                body = self.read_file(path)
                body = body if isinstance(body, str) else body[0]["text"] + " (use read_file to view)"
            except ToolError as exc:
                body = "ERROR: %s" % exc
            part = "=== %s ===\n%s" % (path, body)
            size = len(part.encode("utf-8"))
            if used + size > self.cfg["maxFileSizeBytes"]:
                parts.append("... combined output stopped at maxFileSizeBytes; read remaining files individually")
                break
            parts.append(part)
            used += size
        return "\n\n".join(parts)

    def list_directory(self, path, depth=2):
        real = self.resolve(path)
        if not os.path.isdir(real):
            raise ToolError("not a directory: %s" % real)
        out = []

        def walk(directory, rel, level):
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
            except OSError:
                out.append("[DENIED] %s" % (rel or "."))
                return
            for e in entries:
                if len(out) >= MAX_LIST_ENTRIES:
                    return
                name = os.path.join(rel, e.name) if rel else e.name
                if e.is_dir(follow_symlinks=False):
                    out.append("[DIR] %s/" % name)
                    if level < depth:
                        walk(e.path, name, level + 1)
                else:
                    try:
                        size = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = 0
                    out.append("[FILE] %s (%s)" % (name, _human(size)))

        walk(real, "", 1)
        if len(out) >= MAX_LIST_ENTRIES:
            out.append("... truncated at %d entries; list a subdirectory or lower depth" % MAX_LIST_ENTRIES)
        return "%s\n%s" % (real, "\n".join(out) or "(empty)")

    def get_file_info(self, path):
        real = self.resolve(path)
        try:
            st = os.stat(real)
        except OSError as exc:
            raise ToolError("cannot stat %s: %s" % (real, exc.strerror))
        info = {
            "path": real,
            "type": "directory" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other",
            "size": st.st_size,
            "modified": _iso(st.st_mtime),
            "accessed": _iso(st.st_atime),
            "created": _iso(getattr(st, "st_birthtime", st.st_ctime)),
            "permissions": stat.filemode(st.st_mode),
        }
        if info["type"] == "file" and st.st_size <= self.cfg["maxFileSizeBytes"] and not _is_binary(real):
            with open(real, "rb") as f:
                info["lineCount"] = sum(1 for _ in f)
        return "\n".join("%s: %s" % kv for kv in info.items())

    # -- writing ------------------------------------------------------------
    def write_file(self, path, content, mode="rewrite"):
        self.writable()
        real = self.resolve(path)
        data = content.encode("utf-8")
        if len(data) > self.cfg["maxFileSizeBytes"]:
            raise ToolError("content exceeds maxFileSizeBytes")
        if os.path.isdir(real):
            raise ToolError("%s is a directory" % real)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        if mode == "append":
            with open(real, "ab") as f:
                f.write(data)
        else:
            _atomic_write(real, data)
        return "%s %d bytes (%d lines) to %s" % (
            "Appended" if mode == "append" else "Wrote", len(data), content.count("\n") + (not content.endswith("\n") and bool(content)), real)

    def edit_block(self, file_path, old_string, new_string, expected_replacements=1):
        self.writable()
        real = self.resolve(file_path)
        if not old_string:
            raise ToolError("old_string must not be empty")
        limit = self.cfg["maxFileSizeBytes"]
        try:
            if not stat.S_ISREG(os.stat(real).st_mode):
                raise ToolError("edit_block requires a regular file")
            with open(real, "rb") as f:
                raw = f.read(limit + 1)
        except OSError as exc:
            raise ToolError("cannot read %s: %s" % (real, exc.strerror))
        if len(raw) > limit:
            raise ToolError("file exceeds maxFileSizeBytes; nothing was changed")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError("%s is not UTF-8 text; edit_block only edits text files" % real)
        if "\r\n" in text and "\r\n" not in old_string:  # model usually sends \n for CRLF files
            old_string = old_string.replace("\n", "\r\n")
            new_string = new_string.replace("\n", "\r\n")
        count = text.count(old_string)
        if count == 0:
            raise ToolError("old_string not found in %s. Read the file and copy the text exactly.%s"
                            % (real, _closest(text, old_string)))
        if count != expected_replacements:
            raise ToolError("found %d occurrences in %s but expected_replacements=%d; add surrounding context "
                            "to make old_string unique, or set expected_replacements=%d" % (count, real, expected_replacements, count))
        projected = len(raw) + count * (len(new_string.encode("utf-8")) - len(old_string.encode("utf-8")))
        if projected > limit:
            raise ToolError("edited content exceeds maxFileSizeBytes; nothing was changed")
        _atomic_write(real, text.replace(old_string, new_string).encode("utf-8"))
        return "Applied %d replacement(s) in %s" % (count, real)

    def create_directory(self, path):
        self.writable()
        real = self.resolve(path)
        os.makedirs(real, exist_ok=True)
        return "Directory ready: %s" % real

    def move_file(self, source, destination):
        self.writable()
        src, dst = self.resolve(source), self.resolve(destination)
        if not os.path.lexists(src):
            raise ToolError("source not found: %s" % src)
        if os.path.lexists(dst):
            raise ToolError("destination already exists: %s" % dst)
        shutil.move(src, dst)
        return "Moved %s -> %s" % (src, dst)

    # -- search -------------------------------------------------------------
    def _walk(self, root, include_hidden, deadline, stop=None):
        for directory, dirs, files in os.walk(root):
            if stop is not None and stop.is_set():
                return
            if time.monotonic() > deadline:
                raise TimeoutError()
            dirs[:] = sorted(d for d in dirs if include_hidden or not (d.startswith(".") or d in SKIP_DIRS))
            yield directory, dirs, sorted(files)

    def iter_names(self, root, pattern, include_hidden, deadline, stop=None):
        """Yield matching paths one by one (directories end with /)."""
        glob = any(c in pattern for c in "*?[")
        needle = pattern.lower()
        for directory, dirs, files in self._walk(root, include_hidden, deadline, stop):
            for name in dirs + files:
                if fnmatch.fnmatch(name.lower(), needle) if glob else needle in name.lower():
                    yield os.path.join(directory, name) + ("/" if name in dirs else "")

    def iter_code(self, root, rx, file_pattern, context_lines, include_hidden, deadline, stop=None):
        """Yield one list of output lines per matching line (with context)."""
        context_lines = max(0, min(context_lines, 10))
        walker = [(os.path.dirname(root), [], [os.path.basename(root)])] if os.path.isfile(root) else \
            self._walk(root, include_hidden, deadline, stop)
        for directory, _, names in walker:
            for name in names:
                if file_pattern and not fnmatch.fnmatch(name, file_pattern):
                    continue
                full = os.path.join(directory, name)
                try:
                    real = self.resolve(full)
                    if not os.path.isfile(real) or os.path.getsize(real) > self.cfg["maxFileSizeBytes"] or _is_binary(real):
                        continue
                    with open(real, encoding="utf-8", errors="replace") as f:
                        lines = f.read().splitlines()
                except (OSError, ToolError):
                    continue
                for i, line in enumerate(lines):
                    if rx.search(line):
                        lo, hi = max(0, i - context_lines), min(len(lines), i + context_lines + 1)
                        block = ["%s:%d%s%s" % (full, j + 1, ":" if j == i else "-", lines[j][:500]) for j in range(lo, hi)]
                        yield block + (["--"] if context_lines else [])

    def search_files(self, path, pattern, include_hidden=False, max_results=200, timeout_ms=30000):
        root = self.resolve(path)
        found, note = _collect(self.iter_names(root, pattern, include_hidden, time.monotonic() + timeout_ms / 1000),
                               max_results)
        return "%d match(es)%s\n%s" % (len(found), note, "\n".join(found))

    def search_code(self, path, pattern, file_pattern=None, ignore_case=False, literal=False,
                    context_lines=0, include_hidden=False, max_results=200, timeout_ms=30000):
        root = self.resolve(path)
        rx = compile_pattern(pattern, literal, ignore_case)
        hits, note = _collect(self.iter_code(root, rx, file_pattern, context_lines, include_hidden,
                                             time.monotonic() + timeout_ms / 1000), max_results)
        return "%d matching line(s)%s\n%s" % (len(hits), note, "\n".join(l for block in hits for l in block))


def compile_pattern(pattern, literal, ignore_case):
    try:
        return re.compile(re.escape(pattern) if literal else pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ToolError("invalid regular expression: %s (set literal=true to search plain text)" % exc)


def _collect(items, max_results):
    found = []
    try:
        for item in items:
            found.append(item)
            if len(found) >= max_results:
                return found, " (stopped at max_results)"
    except TimeoutError:
        return found, " (timed out)"
    return found, ""


def _closest(text, needle):
    """Describe where the file differs from a near-miss old_string: common{-sent-}{+in file+}common."""
    lines = text.splitlines(True)
    want = needle.splitlines(True) or [needle]
    n = len(want)
    if not lines or len(lines) > 20000:
        return ""
    best, best_i = 0.0, 0
    for i in range(max(1, len(lines) - n + 1)):
        window = "".join(lines[i:i + n])
        sm = difflib.SequenceMatcher(None, needle, window, autojunk=False)
        if sm.real_quick_ratio() > best and sm.quick_ratio() > best:
            r = sm.ratio()
            if r > best:
                best, best_i = r, i
    if best < 0.5:
        return ""
    window = "".join(lines[best_i:best_i + n])
    if not needle.endswith("\n"):
        window = window.rstrip("\r\n")
    out = []
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, needle, window, autojunk=False).get_opcodes():
        if op == "equal":
            seg = needle[a1:a2]
            out.append(seg if len(seg) <= 80 else seg[:40] + "…" + seg[-40:])
        else:
            if a2 > a1:
                out.append("{-%s-}" % needle[a1:a2])
            if b2 > b1:
                out.append("{+%s+}" % window[b1:b2])
    diff = "".join(out)
    return "\nClosest text at lines %d-%d (%d%% similar). Difference, {-your text-}{+file text+}:\n%s" % (
        best_i + 1, best_i + n, best * 100, diff[:1500])


# -- documents (stdlib only) ---------------------------------------------------
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _check_ip(addr):
    ip = ipaddress.ip_address(addr.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        raise ToolError("refusing to fetch: address %s is not public" % addr)


def _pinned_connect(host, port, timeout):
    """Resolve once, refuse non-public addresses, and connect to exactly the address that was checked.

    Checking at connect time covers redirects and DNS rebinding. It keeps read_file on a read-only node from
    reaching its own local services or cloud metadata endpoints (commands, where allowed, can anyway).
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ToolError("cannot resolve %s: %s" % (host, exc))
    for info in infos:
        try:
            _check_ip(info[4][0])
        except ToolError:
            raise ToolError("refusing to fetch: %s resolves to non-public address %s" % (host, info[4][0]))
    error = None
    for family, kind, proto, _, addr in infos:
        sock = socket.socket(family, kind, proto)
        if isinstance(timeout, (int, float)):
            sock.settimeout(timeout)
        try:
            sock.connect(addr)
            return sock
        except OSError as exc:
            error = exc
            sock.close()
    raise error or OSError("cannot connect to %s" % host)


class _HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _pinned_connect(self.host, self.port, self.timeout)


class _HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = _pinned_connect(self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not re.match(r"https?://", newurl, re.I):
            raise ToolError("refusing redirect to %s" % newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# No ProxyHandler: a proxy from the environment would bypass the address check.
_OPENER = urllib.request.OpenerDirector()
for _handler in (_HTTPHandler(), _HTTPSHandler(), _CheckedRedirect(), urllib.request.HTTPDefaultErrorHandler(),
                 urllib.request.HTTPErrorProcessor()):
    _OPENER.add_handler(_handler)


def _windows_fetch_root(url):
    """Windows installs root certificates on first use through its own TLS stack; Python cannot trigger that.
    One HEAD request to the same public host through PowerShell does, after which Python's check succeeds.
    Only https://host:port/ is sent (no path, no redirects), passed as data in an environment variable."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme.lower() != "https" or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        return
    target = "https://%s:%d/" % (host, parts.port or 443)
    script = ("try { Invoke-WebRequest -Method Head -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 20 "
              "-Uri $env:BC_ROOT_URL | Out-Null } catch { }")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=dict(os.environ, BC_ROOT_URL=target), capture_output=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _zip_xml(zf, name, limit):
    info = zf.getinfo(name)
    if info.file_size > limit:
        raise ToolError("%s inside the document is too large" % name)
    data = zf.read(name)
    markup = data.replace(b"\x00", b"")  # UTF-16/32 markup must not bypass the declaration guard
    if b"<!DOCTYPE" in markup or b"<!ENTITY" in markup:
        raise ToolError("%s contains a DTD or entity declarations; refusing to parse" % name)
    return ElementTree.fromstring(data)


def _docx(files, path, sheet=None):
    limit = 5 * files.cfg["maxFileSizeBytes"]
    try:
        with zipfile.ZipFile(path) as zf:
            body = _zip_xml(zf, "word/document.xml", limit).find(W + "body")
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise ToolError("cannot read DOCX %s: %s" % (path, exc))
    out = []

    def para(p):
        parts = []
        for node in p.iter():
            if node.tag == W + "t":
                parts.append(node.text or "")
            elif node.tag == W + "tab":
                parts.append("\t")
            elif node.tag in (W + "br", W + "cr"):
                parts.append("\n")
        return "".join(parts)

    for el in body if body is not None else []:
        if el.tag == W + "p":
            style = el.find("%spPr/%spStyle" % (W, W))
            text = para(el)
            level = style.get(W + "val", "") if style is not None else ""
            m = re.match(r"(?i)heading(\d)", level)
            out.append(("#" * int(m.group(1)) + " " + text) if m and text else text)
        elif el.tag == W + "tbl":
            for row in el.iter(W + "tr"):
                out.append("| " + " | ".join(" ".join(para(p) for p in tc.iter(W + "p")) for tc in row.iter(W + "tc")) + " |")
    return "\n".join(out) + "\n"


def _col(ref):
    if not isinstance(ref, str) or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", ref):
        raise ToolError("invalid XLSX column/cell reference")
    n = 0
    for ch in re.match(r"[A-Z]*", ref).group():
        n = n * 26 + ord(ch) - 64
    if n > 16384:
        raise ToolError("XLSX column exceeds the XFD limit")
    return max(n - 1, 0)


def _xlsx(files, path, sheet=None):
    limit = 5 * files.cfg["maxFileSizeBytes"]
    try:
        with zipfile.ZipFile(path) as zf:
            remaining = limit

            def xml(member):
                nonlocal remaining
                size = zf.getinfo(member).file_size
                if size > remaining:
                    raise ToolError("document uncompressed XML exceeds the aggregate limit")
                remaining -= size  # every actual parse counts, including repeated relationship targets
                return _zip_xml(zf, member, limit)

            names = set(zf.namelist())
            shared = []
            if "xl/sharedStrings.xml" in names:
                for si in xml("xl/sharedStrings.xml").iter(S_NS + "si"):
                    shared.append("".join(t.text or "" for t in si.iter(S_NS + "t")))
            rels = {r.get("Id"): r.get("Target") for r in xml("xl/_rels/workbook.xml.rels")}
            sheets = [(s.get("name"), rels.get(s.get(R_NS + "id"), "")) for s in
                      xml("xl/workbook.xml").iter(S_NS + "sheet")]
            if sheet is not None:
                pick = [s for i, s in enumerate(sheets) if s[0] == sheet or str(i) == str(sheet)]
                if not pick:
                    raise ToolError("no sheet %r; sheets: %s" % (sheet, [s[0] for s in sheets]))
                sheets = pick
            out = []
            used = 0
            text_limit = files.cfg["maxFileSizeBytes"]
            for name, target in sheets:
                target = target.lstrip("/")
                target = target if target.startswith("xl/") else "xl/" + target
                header = "## Sheet: %s" % name
                used += len(header.encode("utf-8")) + 1
                if used > text_limit:
                    raise ToolError("extracted text exceeds maxFileSizeBytes")
                out.append(header)
                for row in xml(target).iter(S_NS + "row"):
                    cells = {}
                    row_bytes, last_col = 0, 0
                    for c in row.iter(S_NS + "c"):
                        kind, v = c.get("t"), c.find(S_NS + "v")
                        if kind == "inlineStr":
                            value = "".join(t.text or "" for t in c.iter(S_NS + "t"))
                        elif v is None:
                            continue
                        elif kind == "s":
                            value = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
                        elif kind == "b":
                            value = "TRUE" if v.text == "1" else "FALSE"
                        else:
                            value = v.text or ""
                        column = _col(c.get("r", "A1"))
                        value = value.replace("\t", " ").replace("\n", " ")
                        row_bytes += len(value.encode("utf-8")) - len(cells.get(column, "").encode("utf-8"))
                        last_col = max(last_col, column)
                        if used + row_bytes + last_col + 1 > text_limit:
                            raise ToolError("extracted text exceeds maxFileSizeBytes")
                        cells[column] = value
                    if cells:
                        used += row_bytes + last_col + 1
                        out.append("\t".join(cells.get(i, "") for i in range(max(cells) + 1)))
    except (zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError) as exc:
        raise ToolError("cannot read XLSX %s: %s" % (path, exc))
    return "\n".join(out) + "\n"


# macOS has PDFKit built in; JavaScript for Automation reaches it without installing anything.
PDFKIT_JS = """ObjC.import('PDFKit');
function run(argv) {
  var doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
  if (!doc || doc.isNil()) throw new Error('cannot open PDF');
  var out = [];
  for (var i = 0; i < doc.pageCount; i++) {
    var s = doc.pageAtIndex(i).string;
    out.push(s && !s.isNil() ? s.js : '');
  }
  return out.join('\\f');
}"""


def _bounded_stdout(argv, limit, input=None, timeout=60):
    """Read a converter's finite output; kill only that converter on timeout or overflow."""
    expired = threading.Event()
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def stop():
        expired.set()
        try:
            proc.kill()
        except OSError:
            pass

    timer = threading.Timer(timeout, stop)
    timer.daemon = True
    timer.start()
    try:
        if input is not None:
            proc.stdin.write(input)
            proc.stdin.close()
        data = proc.stdout.read(limit + 1)
        if len(data) > limit:
            raise ToolError("PDF extracted text exceeds maxFileSizeBytes")
        code = proc.wait()
        if expired.is_set():
            raise ToolError("PDF text extraction timed out")
        if code:
            raise ToolError("PDF text extraction failed (exit code %d)" % code)
        return data
    finally:
        timer.cancel()
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        proc.wait()
        for stream in (proc.stdin, proc.stdout):
            if stream is not None and not stream.closed:
                stream.close()


def _pdf(files, path, sheet=None):
    tool = shutil.which("pdftotext")
    if tool:
        argv, script = [tool, "-layout", "-enc", "UTF-8", path, "-"], None
    elif os.path.exists("/usr/bin/osascript"):
        argv, script = ["/usr/bin/osascript", "-l", "JavaScript", "-", path], PDFKIT_JS.encode()
    else:
        raise ToolError("reading PDF needs pdftotext (poppler-utils) on this node: "
                        "apt install poppler-utils / brew install poppler")
    try:
        out = _bounded_stdout(argv, files.cfg["maxFileSizeBytes"], input=script)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError("PDF text extraction failed: %s" % exc)
    text = out.decode("utf-8", "replace")
    marker = "\n--- page break ---\n"
    if len(text.encode("utf-8")) + text.count("\f") * (len(marker) - 1) > files.cfg["maxFileSizeBytes"]:
        raise ToolError("PDF extracted text exceeds maxFileSizeBytes")
    return text.replace("\f", marker)


DOCUMENTS = {".docx": _docx, ".xlsx": _xlsx, ".xlsm": _xlsx, ".pdf": _pdf}


def _inside(path, root):
    path, root = os.path.normcase(path), os.path.normcase(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _is_binary(path):
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return False


def _atomic_write(path, data):
    directory = os.path.dirname(path)
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else None
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bc-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (size, unit)
        size /= 1024.0


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")

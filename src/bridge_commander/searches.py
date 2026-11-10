"""Background searches: start now, page through results while the walk continues."""
import threading
import time
import uuid

from .files import ToolError, compile_pattern

MAX_SEARCHES = 20
SEARCH_SECONDS = 600  # hard limit for one background search
KEEP_SECONDS = 600  # finished searches are forgotten after this


class Search:
    def __init__(self, kind, root, pattern, items, max_results, stop):
        self.id = uuid.uuid4().hex[:8]
        self.kind, self.root, self.pattern = kind, root, pattern
        self.max_results = max_results
        self.results = []  # output lines
        self.hits = 0
        self.status = "running"
        self.started = time.time()
        self.finished = None
        self.stop = stop
        self.cond = threading.Condition()
        threading.Thread(target=self._run, args=(items,), daemon=True).start()

    def _run(self, items):
        status = "finished"
        try:
            for item in items:
                with self.cond:
                    self.results.extend(item if isinstance(item, list) else [item])
                    self.hits += 1
                    self.cond.notify_all()
                if self.hits >= self.max_results:
                    status = "stopped at max_results"
                    break
                if self.stop.is_set():
                    status = "stopped"
                    break
            else:
                if self.stop.is_set():
                    status = "stopped"
        except TimeoutError:
            status = "timed out after %ds" % SEARCH_SECONDS
        except Exception as exc:  # e.g. a directory vanished mid-walk
            status = "failed: %s" % exc
        with self.cond:
            self.status, self.finished = status, time.time()
            self.cond.notify_all()

    def wait(self, timeout_ms):
        deadline = time.monotonic() + timeout_ms / 1000.0
        with self.cond:
            while self.finished is None and time.monotonic() < deadline:
                self.cond.wait(deadline - time.monotonic())

    def page(self, offset, length):
        with self.cond:
            total = len(self.results)
            part = self.results[offset:] if offset < 0 else self.results[offset:offset + length]
            start = max(total + offset, 0) if offset < 0 else offset
            unit = "match(es)" if self.kind == "files" else "matching line(s)"
            head = "Search %s: %s | %d %s | %s (%ds) | showing results %d-%d of %d" % (
                self.id, self.status, self.hits, unit, self.root, (self.finished or time.time()) - self.started,
                start + 1 if part else start, start + len(part), total)
            more = total - (start + len(part))
            tail = "\n[%d more: call get_more_search_results with offset=%d]" % (more, start + len(part)) \
                if more > 0 and offset >= 0 else ""
            if self.finished is None:
                tail += "\n[still running: call get_more_search_results again for new results]"
        return head + "\n" + "\n".join(part) + tail


class Searches:
    def __init__(self, files):
        self.files = files
        self.items = {}
        self.lock = threading.Lock()

    def _gc(self):
        now = time.time()
        for sid, s in list(self.items.items()):
            if s.finished and now - s.finished > KEEP_SECONDS:
                del self.items[sid]
        if len(self.items) >= MAX_SEARCHES:
            done = sorted((s.finished, sid) for sid, s in self.items.items() if s.finished)
            if not done:
                raise ToolError("too many running searches (%d); stop one first" % MAX_SEARCHES)
            del self.items[done[0][1]]

    def start_search(self, path, pattern, search_type="files", file_pattern=None, ignore_case=True,
                     literal=False, context_lines=0, include_hidden=False, max_results=1000, timeout_ms=3000):
        root = self.files.resolve(path)
        deadline = time.monotonic() + SEARCH_SECONDS
        stop = threading.Event()
        if search_type == "files":
            items = self.files.iter_names(root, pattern, include_hidden, deadline, stop)  # names: always case-insensitive
        else:
            rx = compile_pattern(pattern, literal, ignore_case)
            items = self.files.iter_code(root, rx, file_pattern, context_lines, include_hidden, deadline, stop)
        with self.lock:
            self._gc()
            search = Search(search_type, root, pattern, items, max_results, stop)
            self.items[search.id] = search
        search.wait(timeout_ms)
        return search.page(0, 100)

    def _get(self, search_id):
        search = self.items.get(search_id)
        if search is None:
            raise ToolError("no search %r; use list_searches" % search_id)
        return search

    def get_more_search_results(self, search_id, offset=0, length=100):
        return self._get(search_id).page(offset, max(1, min(length, 1000)))

    def stop_search(self, search_id):
        search = self._get(search_id)
        search.stop.set()
        search.wait(2000)
        return "Search %s: %s, %d result line(s) kept for %d minutes" % (
            search_id, search.status, len(search.results), KEEP_SECONDS // 60)

    def list_searches(self):
        if not self.items:
            return "No searches."
        return "\n".join("%s | %s | %s | %r in %s | %d hit(s) | %ds" % (
            s.id, s.kind, s.status, s.pattern, s.root, s.hits, (s.finished or time.time()) - s.started)
            for s in sorted(self.items.values(), key=lambda s: s.started))

    def close(self):
        for s in self.items.values():
            s.stop.set()

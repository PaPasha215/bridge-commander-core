"""Remote mode: this machine connects out to a Bridge Commander cloud and runs the calls it receives.

No open ports: the device makes outgoing HTTPS requests only.
  1. POST {cloud}/device/code          -> user_code + verification URL (sign-in happens in the browser)
  2. POST {cloud}/device/token         -> device token once the user approved the code (RFC 8628 style)
  3. POST {cloud}/device/poll          -> long-poll for calls [{id, tool, args}]
  4. POST {cloud}/device/result        -> {id, content, isError} for each call
Optional fallback routes (e.g. a CDN-fronted name of the same cloud) are tried when the primary is unreachable.
The device applies its own config (allowed directories, read-only, blocked commands) to every call.
"""
import json
import os
import platform
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .receipts import ReceiptError, ReceiptJournal, device_scope

POLL_SECONDS = 25  # the cloud holds a poll open up to this long
PRIMARY_RECHECK = 300  # while on a fallback route, look for the primary this often (seconds)
CREDENTIALS = Path("~/.config/bridge-commander/device.json")
RECEIPTS = Path("~/.local/state/bridge-commander/remote-receipts/receipts.sqlite3")
RESULT_CACHE_COUNT = 4
RESULT_CACHE_BYTES = 32 * 1024 * 1024


class Revoked(Exception):
    pass


def _remember_result(cache, cid, body):
    """Bound resend memory; an evicted body still has its durable completion receipt."""
    wire = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    cache.pop(cid, None)
    cache[cid] = (wire, dict(body["receipt"]))
    while len(cache) > RESULT_CACHE_COUNT or sum(len(item[0]) for item in cache.values()) > RESULT_CACHE_BYTES:
        cache.pop(next(iter(cache)))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # device credentials belong only to the configured route


_POST_OPENER = urllib.request.build_opener(_NoRedirect())


def _post(url, body, token=None, timeout=POLL_SECONDS + 15):
    data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "bridge-commander/" + __version__})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _POST_OPENER.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def check_cloud_url(url):
    parts = urllib.parse.urlsplit(url or "")
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise SystemExit("bridge-commander: the cloud URL must be https:// (http only for localhost)")
    return url.rstrip("/")


def check_fallback(primary, url):
    """A fallback must be another route to the same cloud: the primary's host, a subdomain of it, or its parent."""
    url = check_cloud_url(url)
    a, b = urllib.parse.urlsplit(primary).hostname, urllib.parse.urlsplit(url).hostname
    if not (a == b or b.endswith("." + a) or a.endswith("." + b)):
        raise SystemExit("bridge-commander: fallback %s is not on the cloud's domain %s" % (b, a))
    return url


def healthy(url, timeout=5):
    try:
        req = urllib.request.Request(url + "/health", headers={"User-Agent": "bridge-commander/" + __version__})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (OSError, ValueError):
        return False


def pick_route(routes, check=healthy):
    """First route (in preference order) whose /health answers, or None."""
    return next((url for url in routes if check(url)), None)


def load_credentials(path=None):
    path = Path(path or CREDENTIALS).expanduser()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_credentials(data, path=None):
    path = Path(path or CREDENTIALS).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(str(tmp), str(path))


def device_info(name=None):
    return {"name": name or socket.gethostname().split(".")[0], "hostname": socket.gethostname(),
            "platform": "%s %s (%s)" % (platform.system(), platform.release(), platform.machine()),
            "version": __version__}


def sign_in(cloud, name=None, open_browser=True, out=sys.stderr):
    status, data = _post(cloud + "/device/code", device_info(name), timeout=30)
    if status != 200 or "device_code" not in data:
        raise SystemExit("bridge-commander: the cloud refused the sign-in request (%s %s)" % (status, data))
    url = data.get("verification_uri_complete") or data["verification_uri"]
    print("\nTo connect this machine, open %s\nand confirm the code: %s\n" % (url, data["user_code"]), file=out)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    interval = max(1, int(data.get("interval", 5)))
    deadline = time.monotonic() + int(data.get("expires_in", 900))
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, token = _post(cloud + "/device/token", {"device_code": data["device_code"]}, timeout=30)
        if status == 200 and token.get("device_token"):
            print("Connected as device %r." % token.get("device_name"), file=out)
            return {"cloud": cloud, "device_token": token["device_token"], "device_id": token.get("device_id"),
                    "device_name": token.get("device_name")}
        error = token.get("error")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise SystemExit("bridge-commander: sign-in failed: %s" % (error or status))
    raise SystemExit("bridge-commander: the code expired; run the command again")


def serve(toolbox, creds, out=sys.stderr, stop=None, check=healthy, clock=time.monotonic, journal=None):
    """Long-poll the cloud and run calls until revoked or stopped.

    Routes: creds["cloud"] first, then creds["fallback"]. On a network error the client switches to the
    first route whose /health answers; while on a fallback it re-checks the primary every PRIMARY_RECHECK s.
    """
    token = creds["device_token"]
    routes = [creds["cloud"]] + [u for u in creds.get("fallback") or [] if u != creds["cloud"]]
    start = (len(routes) > 1 and pick_route(routes, check)) or routes[0]  # don't wait out a blocked primary
    state = {"base": start, "checked": clock()}
    stop = stop or threading.Event()
    backoff = 1
    if journal is None:
        if not creds.get("device_id"):
            raise ReceiptError("device credentials lack an identity; reconnect this device before executing calls")
        journal = ReceiptJournal(RECEIPTS, device_scope(creds))
    cached, uploading = {}, set()  # outputs live only in memory; the journal contains hashes and states
    result_lock = threading.Lock()
    uploads_done = threading.Event()
    uploads_done.set()
    work_slots, upload_slots = threading.BoundedSemaphore(4), threading.BoundedSemaphore(2)

    def log(line):
        print(line, file=out, flush=True)

    def switch(url, why):
        if url != state["base"]:
            print("route: %s -> %s (%s)" % (state["base"], url, why), file=out, flush=True)
            state["base"] = url
        state["checked"] = clock()

    def upload(cid, body, receipt):
        try:
            for _ in range(3):
                try:
                    status, _ = _post(state["base"] + "/device/result", body, token, timeout=60)
                    if status in (200, 400, 401, 404, 409, 410):
                        if status not in (200, 404, 410):
                            log("result delivery refused (%s); retained receipt, no automatic execution retry" % status)
                        if journal.posted(cid, receipt):
                            with result_lock:
                                cached.pop(cid, None)
                        return
                except OSError:
                    pass
                if stop.wait(2):
                    return
        finally:
            with result_lock:
                uploading.discard(cid)
                if not uploading:
                    uploads_done.set()
            upload_slots.release()

    def send_pending():
        for row in journal.pending():
            cid = row["id"]
            with result_lock:
                if cid in uploading or not upload_slots.acquire(blocking=False):
                    continue
                uploading.add(cid)
                uploads_done.clear()
                body, receipt = cached[cid] if cid in cached else (None, None)
            if body is None:
                completed = row["state"] == "COMPLETED"
                receipt = {"state": row["state"], "result_available": False,
                           "is_error": bool(row["is_error"]) if completed else True}
                if completed:
                    receipt["result_hash"] = row["result_hash"]
                body = {"id": cid, "content": [{"type": "text", "text":
                        ("Execution completed, but output was not retained on this device. Reconcile the actual result; "
                         "do not repeat the operation." if completed else
                         "OUTCOME_UNKNOWN: execution was interrupted or could not be acknowledged. "
                         "Check the actual result before any new mutating call.")}], "isError": True, "receipt": receipt}
            threading.Thread(target=upload, args=(cid, body, receipt), daemon=True).start()

    def run(call):
        cid = call["id"]
        try:
            # A durable claim exists before ACK. An uncertain ACK can never authorize execution.
            acknowledged = False
            for _ in range(3):
                try:
                    status, _ = _post(state["base"] + "/device/ack", {"id": cid}, token, timeout=10)
                    if status == 200:
                        acknowledged = True
                        break
                    if status in (400, 401, 404, 409, 410):
                        break
                except OSError:
                    pass
                if stop.wait(2):
                    break
            if not acknowledged or stop.is_set() or time.time() >= call["expires_at"]:
                journal.unknown(cid)
                return
            try:
                content, is_error = toolbox.call(call["tool"], call.get("args") or {}, from_hub=True)
            except KeyError:
                content, is_error = [{"type": "text", "text": "Error: unknown tool %s" % call.get("tool")}], True
            with result_lock:
                # A poll must not upload a metadata-only completion between commit and memory caching.
                receipt = journal.complete(cid, content, is_error)
                _remember_result(cached, cid, {"id": cid, "content": content,
                                               "isError": bool(is_error), "receipt": receipt})
        except Exception:
            journal.unknown(cid)  # a tool may have changed the machine before throwing
            log("execution result unavailable; retained UNKNOWN receipt, no automatic execution retry")
        finally:
            work_slots.release()
            send_pending()

    print("bridge-commander %s: connected to %s, waiting for calls (Ctrl+C to stop)" % (__version__, start),
          file=out)
    if len(routes) > 1:
        print("fallback routes: %s" % ", ".join(routes[1:]), file=out)
    try:
        send_pending()
        while not stop.is_set():
            if state["base"] != routes[0] and clock() - state["checked"] >= PRIMARY_RECHECK:
                switch(routes[0] if check(routes[0]) else state["base"], "primary is back")
            try:
                status, data = _post(state["base"] + "/device/poll", {"info": device_info(creds.get("device_name"))},
                                     token)
            except OSError as exc:  # network down, route blocked, cloud restarting
                others = [u for u in routes if u != state["base"]]  # the failed route goes last
                alive = pick_route(others, check) if others else None
                if alive and alive != state["base"]:
                    switch(alive, "%s unreachable: %s" % (state["base"], exc))
                    continue
                print("cloud unreachable (%s); retrying in %ds" % (exc, backoff), file=out)
                stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if status == 401:
                stop.set()
                raise Revoked()
            if status != 200:
                log("%s cloud poll failed (HTTP %d); retrying in %ds" %
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), status, backoff))
                stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 1
            send_pending()
            for call in data.get("calls") or []:
                if stop.is_set():
                    break
                if isinstance(call, dict) and isinstance(call.get("tool"), str) and "id" in call:
                    try:
                        row, is_new = journal.begin(call)
                    except (ReceiptError, ValueError, TypeError):
                        log("refused a call without a valid durable identity/window; nothing was executed")
                        continue
                    if is_new:
                        if work_slots.acquire(blocking=False):
                            threading.Thread(target=run, args=(call,), daemon=True).start()
                        else:
                            journal.unknown(call["id"])
                            send_pending()
                    elif row["state"] != "RUNNING":
                        send_pending()
    finally:
        stop.set()
        uploads_done.wait(2)  # finish an already accepted receipt before shutdown/revocation returns

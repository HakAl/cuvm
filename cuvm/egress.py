"""Per-run egress proxy: the real login token stays on the host (docs/design.md, Credentials).

Only CONNECT to allowlisted hosts on port 443. Swap hosts (api.anthropic.com) are terminated with
leaf certificates from a per-run CA, one request per connection, and the guest's dummy token is
replaced by the real one in the Authorization or x-api-key header only. The dummy anywhere else in
a request is refused, never rewritten. Responses are scanned as a client would decode them
(chunked framing removed, other codings refused) and the connection is closed before any part of
the real token reaches the guest. Other allowlisted hosts get a plain
tunnel, not inspected. Plain HTTP and everything else gets 403.

Stdlib only and run as a script (python3 -I egress.py ...), so nothing else in cuvm reads the
token. The dummy arrives on stdin, the real token from the host keychain. Log lines (JSON) never
hold either token.
"""
import argparse
import datetime
import fnmatch
import json
import os
import pathlib
import re
import select
import socket
import ssl
import subprocess
import sys
import threading
import traceback
import urllib.parse

FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
SWAP_HEADERS = {"authorization", "x-api-key"}
DROP_HEADERS = {"connection", "proxy-connection", "keep-alive", "upgrade", "te", "proxy-authorization",
                "accept-encoding"}
MAX_HEAD = 1 << 16
IDLE_S = 120
CERT_DAYS = 2


def now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _openssl(*args):
    subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True)


def make_ca(d):
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "ca.key", "-out", d / "ca.pem",
             "-days", CERT_DAYS, "-subj", "/CN=cuvm per-run CA", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    os.chmod(d / "ca.key", 0o600)


def make_leaf(d, host):
    crt, key, csr, ext = (d / (host + s) for s in (".pem", ".key", ".csr", ".ext"))
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", csr, "-subj", "/CN=" + host)
    os.chmod(key, 0o600)
    ext.write_text("subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature,"
                   "keyEncipherment\nbasicConstraints=critical,CA:FALSE\n" % host)
    _openssl("x509", "-req", "-in", csr, "-CA", d / "ca.pem", "-CAkey", d / "ca.key", "-CAcreateserial",
             "-out", crt, "-days", CERT_DAYS, "-extfile", ext)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    ctx.set_alpn_protocols(["http/1.1"])  # one request per connection is only enforceable on HTTP/1.1
    # A client FIN without close_notify must not poison the connection before the response is sent (TI-W2).
    ctx.options |= getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
    return ctx


def read_head(sock):
    """Read up to the end of an HTTP head. Returns (head bytes, bytes after it); head None on EOF."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > MAX_HEAD:
            raise ValueError("head larger than %d bytes" % MAX_HEAD)
        try:
            chunk = sock.recv(65536)
        except (ssl.SSLEOFError, ConnectionResetError, BrokenPipeError):
            chunk = b""
        if not chunk:
            return None, buf
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    return head, rest


def _readable(socks, timeout):
    """TLS sockets may hold decrypted bytes that select() cannot see."""
    ready = [s for s in socks if isinstance(s, ssl.SSLSocket) and s.pending()]
    return ready or select.select(socks, [], [], timeout)[0]


def _recv(s):
    """Non-blocking read: None when nothing to read yet (a TLS 1.3 session ticket makes a socket
    readable without application data), b"" on EOF."""
    try:
        return s.recv(65536)
    except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
        return None
    except (ssl.SSLEOFError, ConnectionResetError):
        return b""


def _send(s, data):
    s.settimeout(60)
    try:
        s.sendall(data)
    finally:
        s.settimeout(0)


class Egress:
    def __init__(self, listen, guest, allow, swap_hosts, dummy, real, ca_dir, log_path, port=0,
                 upstream_ctx=None, dial=None):
        if not real.startswith("sk-ant-") or len(real) < 20:
            raise ValueError("real token missing or malformed")
        if len(dummy) < 20 or dummy in real or real in dummy:
            raise ValueError("dummy token missing, too short, or overlapping the real token")
        self.guest, self.dummy, self.real = guest, dummy, real
        self.allow = [a.lower() for a in allow]
        self.swap = {h.lower() for h in swap_hosts}
        if not all(self.allowed(h) for h in self.swap):
            raise ValueError("swap hosts must be allowlisted")
        self.log_path = pathlib.Path(log_path)
        self._lock = threading.Lock()
        ca_dir = pathlib.Path(ca_dir)
        make_ca(ca_dir)
        self.ca_pem = ca_dir / "ca.pem"
        self.ctx = {h: make_leaf(ca_dir, h) for h in sorted(self.swap)}  # all made before serving
        self.upstream = upstream_ctx or ssl.create_default_context()
        self.dial = dial or (lambda host, port_: socket.create_connection((host, port_), timeout=30))
        self.srv = socket.create_server((listen, port))
        self.port = self.srv.getsockname()[1]

    def allowed(self, host):
        return any(fnmatch.fnmatchcase(host, a) for a in self.allow)

    def log(self, **ev):
        line = json.dumps(dict(at=now(), **ev))
        # Belt and braces: no token, whole or as any sk-ant- string, is ever written.
        line = line.replace(self.real, "<real>").replace(self.dummy, "<dummy>")
        line = re.sub(r"sk-ant-[A-Za-z0-9_\-]*", "sk-ant-<redacted>", line)
        with self._lock, open(self.log_path, "a") as f:
            f.write(line + "\n")

    def serve_forever(self):
        self.log(ev="listening", port=self.port, guest=self.guest, allow=self.allow, swap=sorted(self.swap))
        while True:
            try:
                c, addr = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.client, args=(c, addr), daemon=True).start()

    def close(self):
        self.srv.close()

    def client(self, c, addr):
        try:
            if addr[0] != self.guest:
                self.log(ev="unauthorized", client=addr[0])
                return
            c.settimeout(60)
            head, rest = read_head(c)
            if head is None:
                return
            parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            if len(parts) != 3 or parts[0].upper() != "CONNECT":
                target = parts[1] if len(parts) > 1 else ""
                self.log(ev="refused", method=parts[0][:20] if parts else "", host=urllib.parse.urlsplit(target).hostname,
                         why="not CONNECT")
                c.sendall(FORBIDDEN)
                return
            host, _, port = parts[1].rpartition(":")
            host = host.strip("[]").lower()
            if port != "443" or not self.allowed(host):
                self.log(ev="refused", method="CONNECT", host=host[:255], port=port[:10],
                         why="port" if self.allowed(host) else "not allowlisted")
                c.sendall(FORBIDDEN)
                return
            if host in self.swap:
                if rest:
                    self.log(ev="refused", method="CONNECT", host=host, port=port, why="bytes before tunnel reply")
                    c.sendall(FORBIDDEN)
                    return
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                try:
                    t = self.ctx[host].wrap_socket(c, server_side=True)
                except (ssl.SSLError, OSError) as e:
                    self.log(ev="client_tls_failed", host=host, error=str(e)[:200])
                    return
                try:
                    self.request(t, host)
                finally:
                    t.close()
            else:
                self.tunnel(c, host, rest)
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)[-1]
            self.log(ev="error", error=repr(e)[:300], where="%s:%d" % (tb.name, tb.lineno))
        finally:
            c.close()

    def tunnel(self, c, host, rest):
        try:
            u = self.dial(host, 443)
        except OSError as e:
            self.log(ev="upstream_failed", host=host, error=str(e)[:200])
            c.sendall(BAD_GATEWAY)
            return
        c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        up, down = len(rest), 0
        try:
            u.settimeout(0)
            c.settimeout(0)
            if rest:
                _send(u, rest)
            peer, socks = {c: u, u: c}, [c, u]
            while socks:
                r = _readable(socks, IDLE_S)
                if not r:
                    break
                for s in r:
                    data = _recv(s)
                    if data is None:
                        continue
                    if not data:
                        socks.remove(s)
                        try:
                            peer[s].shutdown(socket.SHUT_WR)  # half-close: the other side may still answer
                        except OSError:
                            pass
                        continue
                    _send(peer[s], data)
                    if s is c:
                        up += len(data)
                    else:
                        down += len(data)
        finally:
            u.close()
            self.log(ev="tunnel", host=host, bytes_up=up, bytes_down=down)

    def request(self, t, host):
        t.settimeout(60)
        try:
            head, rest = read_head(t)
        except ValueError:
            self.log(ev="refused", host=host, why="request head too large")
            t.sendall(FORBIDDEN)
            return
        if head is None:
            self.log(ev="no_request", host=host)
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        method, path = (parts + ["", ""])[:2]
        info = dict(host=host, method=method[:20], path=path.split("?")[0][:200], query="?" in path)
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            return self._refuse(t, info, "bad request line")
        # A bare CR or LF would let one "header" carry more headers or a body past the swap (TI-W1).
        if any("\r" in l or "\n" in l for l in lines):
            return self._refuse(t, info, "bare cr or lf")
        if self.dummy in lines[0]:
            return self._dummy_elsewhere(t, info, "request line")
        out, swapped, other = [lines[0]], [], []
        exact = {"authorization": "Bearer " + self.dummy, "x-api-key": self.dummy}
        for h in lines[1:]:
            name, sep, value = h.partition(":")
            low = name.lower()
            if not sep or not low or low != low.strip() or " " in low or "\t" in low:
                return self._refuse(t, info, "bad header")
            if self.dummy in h:
                # Only the whole credential, in one auth header per request, is swapped.
                if low not in exact or value.strip() != exact[low] or swapped:
                    return self._dummy_elsewhere(t, info, "header " + low[:40])
                value = " " + exact[low].replace(self.dummy, self.real)
                swapped.append(low)
            elif "sk-ant-" in value:
                other.append(low)
            if low not in DROP_HEADERS:
                out.append(name + ":" + value)
        # One request per connection, enforced here (Lookout round 3 F2): the body is exactly
        # Content-Length bytes (none without it); request transfer codings are refused.
        fields = {}
        for h in lines[1:]:
            name, _, value = h.partition(":")
            fields.setdefault(name.lower(), []).append(value.strip())
        if "transfer-encoding" in fields:
            return self._refuse(t, info, "request transfer coding")
        cl = fields.get("content-length", ["0"])
        if len(cl) != 1 or not (cl[0].isascii() and cl[0].isdigit()) or len(cl[0]) > 12:
            return self._refuse(t, info, "bad content-length")
        length = int(cl[0])
        if len(rest) > length:
            return self._refuse(t, info, "bytes after the request")
        if self.dummy.encode() in rest:
            return self._dummy_elsewhere(t, info, "body")
        out += ["Accept-Encoding: identity", "Connection: close"]
        info.update(swapped=swapped, other_token_headers=other)
        try:
            u = self.upstream.wrap_socket(self.dial(host, 443), server_hostname=host)
        except (OSError, ssl.SSLError) as e:
            self.log(ev="upstream_failed", host=host, error=str(e)[:200])
            t.sendall(BAD_GATEWAY)
            return
        try:
            u.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + rest)
            u.settimeout(0)
            t.settimeout(0)
            st = {"status": None, "why": None}
            try:
                outcome = self.relay(t, u, rest, length - len(rest), ResponseFilter(self.real, method), st)
            except (ssl.SSLError, OSError) as e:  # either side went away mid-exchange; still logged
                outcome = "connection_lost"
                info["lost"] = type(e).__name__
            status = st["status"]
            if st["why"]:
                info["why"] = st["why"]
        finally:
            u.close()
        if outcome in ("dummy_elsewhere", "real_in_response"):
            self.log(ev=outcome, **dict(info, where="body") if outcome == "dummy_elsewhere" else info)
        self.log(ev="request", status=status, outcome=outcome, **info)

    def _refuse(self, t, info, why):
        self.log(ev="refused", why=why, **info)
        t.sendall(FORBIDDEN)

    def _dummy_elsewhere(self, t, info, where):
        self.log(ev="dummy_elsewhere", where=where, **info)
        self.log(ev="request", status=None, outcome="dummy_elsewhere", swapped=[], other_token_headers=[], **info)
        t.sendall(FORBIDDEN)

    def relay(self, t, u, sent_body, body_left, resp, st):
        """Client body to upstream: exactly body_left more bytes, dummy scanned across reads; any
        byte beyond ends the exchange. Response to the guest through resp (ResponseFilter), which
        scans the decoded body. Returns the outcome; resp.status and refusal reasons go into st."""
        dummy = self.dummy.encode()
        ctail = sent_body[-(len(dummy) - 1):]
        socks = [t, u]
        try:
            while u in socks:
                r = _readable(socks, IDLE_S)
                if not r:
                    _send(t, resp.finish())
                    return "idle_timeout"
                for s in r:
                    data = _recv(s)
                    if data is None:
                        continue
                    if s is t:
                        if not data:
                            socks.remove(t)  # client half-close: keep reading the response
                            continue
                        if len(data) > body_left:
                            return "surplus_request_bytes"
                        body_left -= len(data)
                        win = ctail + data
                        if dummy in win:
                            return "dummy_elsewhere"
                        _send(u, data)
                        ctail = win[-(len(dummy) - 1):]
                        continue
                    if not data:
                        _send(t, resp.finish())
                        if resp.state != "body":  # upstream closed before the head or the last chunk
                            raise BadResponse("incomplete response")
                        return "ok"
                    _send(t, resp.feed(data))
                    if resp.done:
                        return "ok"
            return "ok"
        except Reflected:
            return "real_in_response"
        except BadResponse as e:
            st["why"] = str(e)[:80]
            if not resp.head_sent:
                _send(t, BAD_GATEWAY)
            return "bad_response"
        finally:
            st["status"] = resp.status


class Reflected(Exception):
    pass


class BadResponse(Exception):
    pass


class ResponseFilter:
    """Upstream response bytes in, bytes for the guest out (Lookout round 3 F1).

    The real-token scan runs on what a client would decode: the head, and the body after chunked
    transfer coding is removed. The last len(real)-1 decoded bytes are held back until more arrive
    or the body ends, so a token split across reads or chunks is caught before any of it is sent.
    Chunked bodies are re-chunked for the guest; trailers are dropped. Content codings other than
    identity and transfer codings other than chunked are refused (upstream was asked for identity),
    because the scan cannot see through them."""

    def __init__(self, real, method):
        self.real, self.keep, self.method = real.encode(), len(real) - 1, method
        self.raw = self.pend = b""
        self.state, self.status, self.head_sent, self.chunked, self.remaining = "head", None, False, False, 0

    @property
    def done(self):
        return self.state == "done"

    def feed(self, data):
        self.raw += data
        out = []
        while True:
            if self.state == "head":
                if self.real in self.raw:
                    raise Reflected()
                i = self.raw.find(b"\r\n\r\n")
                if i < 0:
                    if len(self.raw) > MAX_HEAD:
                        raise BadResponse("response head too large")
                    break
                head, self.raw = self.raw[:i], self.raw[i + 4:]
                out.append(self._head(head))
            elif self.state == "body":
                self._scan(self.raw, out)
                self.raw = b""
                break
            elif self.state == "size":
                i = self.raw.find(b"\r\n")
                if i < 0:
                    if len(self.raw) > 1024:
                        raise BadResponse("chunk size line too long")
                    break
                line, self.raw = self.raw[:i], self.raw[i + 2:]
                size = line.split(b";", 1)[0].strip()
                if not size or len(size) > 12 or any(c not in b"0123456789abcdefABCDEF" for c in size):
                    raise BadResponse("bad chunk size")
                self.remaining = int(size, 16)
                self.state = "data" if self.remaining else "trailers"
            elif self.state == "data":
                if not self.raw:
                    break
                take, self.raw = self.raw[:self.remaining], self.raw[self.remaining:]
                self.remaining -= len(take)
                self._scan(take, out)
                if not self.remaining:
                    self.state = "crlf"
            elif self.state == "crlf":
                if len(self.raw) < 2:
                    break
                if self.raw[:2] != b"\r\n":
                    raise BadResponse("bad chunk end")
                self.raw, self.state = self.raw[2:], "size"
            elif self.state == "trailers":
                end = 2 if self.raw.startswith(b"\r\n") else self.raw.find(b"\r\n\r\n") + 4
                if end < 4 and not self.raw.startswith(b"\r\n"):
                    if len(self.raw) > MAX_HEAD:
                        raise BadResponse("trailers too large")
                    break
                out.append(self._emit(self.pend) + b"0\r\n\r\n")  # trailers dropped
                self.pend, self.raw, self.state = b"", b"", "done"
            else:  # done: nothing after the message is forwarded
                self.raw = b""
                break
        return b"".join(out)

    def finish(self):
        """Upstream closed or went idle: release what is held back (already scanned)."""
        if self.state == "head":
            return b""  # no complete head: nothing forwarded
        rest, self.pend = self.pend, b""
        return self._emit(rest) if rest else b""

    def _head(self, head):
        lines = head.split(b"\r\n")
        self.status = lines[0].decode("latin-1", "replace")[:80]
        parts = lines[0].split(b" ")
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        # Same line hygiene as request heads (agy r3 review R3-F1): a header the scan does not see as
        # one, through a bare CR or LF, a folded line or an odd name, could hide a coding from it.
        if any(b"\r" in l or b"\n" in l for l in lines):
            raise BadResponse("bare cr or lf in response head")
        if code == 101 or not 100 <= code < 600:
            raise BadResponse("bad status line")
        kept = []
        for line in lines[1:]:
            name, sep, value = line.partition(b":")
            low, v = name.lower(), value.strip().lower()
            if not sep or not low or low != low.strip() or b" " in low or b"\t" in low:
                raise BadResponse("bad response header")
            if low == b"content-encoding" and v != b"identity":
                raise BadResponse("content-encoding " + v.decode("latin-1")[:40])
            if low == b"transfer-encoding":
                if v != b"chunked" or self.chunked:
                    raise BadResponse("transfer-encoding " + v.decode("latin-1")[:40])
                self.chunked = True
            if low not in (b"connection", b"keep-alive", b"proxy-connection"):
                kept.append(line)
        if code < 200:  # informational: forward, then expect the final head
            self.chunked = False
            return head + b"\r\n\r\n"
        if self.chunked:  # re-chunked for the guest, so a length upstream sent no longer applies
            kept = [l for l in kept if l.partition(b":")[0].strip().lower() != b"content-length"]
        self.head_sent = True
        if self.method == "HEAD" or code in (204, 304):
            self.state = "done"
        else:
            self.state = "size" if self.chunked else "body"
        return b"\r\n".join([lines[0], *kept, b"Connection: close"]) + b"\r\n\r\n"

    def _scan(self, data, out):
        self.pend += data
        if self.real in self.pend:
            raise Reflected()
        if len(self.pend) > self.keep:
            out.append(self._emit(self.pend[:-self.keep]))
            self.pend = self.pend[-self.keep:]

    def _emit(self, data):
        if not data:
            return b""
        return b"%x\r\n%s\r\n" % (len(data), data) if self.chunked else data


def summarize(text):
    """The run record's proxy section, from the JSON-lines log."""
    ev, bad = [], 0
    for line in text.splitlines():
        try:
            ev.append(json.loads(line))
        except ValueError:
            bad += 1
    of = lambda kind: [e for e in ev if e.get("ev") == kind]
    reqs = of("request")
    return {
        # Every request and tunnel passed the allowlist, whatever its outcome (TI-W3).
        "allowed": sorted({e["host"] for e in reqs + of("tunnel")}),
        "refused": sorted({str(e.get("host")) for e in of("refused")}),
        "unauthorized": sorted({e["client"] for e in of("unauthorized")}),
        "requests": len(reqs),
        "swapped": sum(1 for e in reqs if e.get("swapped")),
        "statuses": sorted({e["status"] for e in reqs if e.get("status")}),
        "tunnels": len(of("tunnel")),
        "dummy_elsewhere": len(of("dummy_elsewhere")),
        "real_in_response": len(of("real_in_response")),
        "bad_response": sum(1 for e in reqs if e.get("outcome") == "bad_response"),
        "surplus_request_bytes": sum(1 for e in reqs if e.get("outcome") == "surplus_request_bytes"),
        "errors": len(of("error")) + len(of("upstream_failed")) + len(of("client_tls_failed")),
        "unparsed_lines": bad,
    }


def keychain_token(item):
    r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", item, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("egress: host keychain item %r not readable (security exit %d)" % (item, r.returncode))
    return r.stdout.strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--listen", required=True)
    ap.add_argument("--guest", required=True)
    ap.add_argument("--port", type=int, default=0, help="0 picks a free port; the log's listening line has it")
    ap.add_argument("--allow", required=True, help="allowlist file, one fnmatch pattern per line")
    ap.add_argument("--swap-host", action="append", default=None, help="default api.anthropic.com")
    ap.add_argument("--dir", required=True, help="per-run CA and leaf certificates (host only)")
    ap.add_argument("--log", required=True)
    ap.add_argument("--keychain-item", default=os.environ.get("CUVM_KEYCHAIN_ITEM", "cuvm-claude-oauth"))
    a = ap.parse_args(argv)
    dummy = sys.stdin.readline().strip()
    real = keychain_token(a.keychain_item)
    try:
        e = Egress(a.listen, a.guest, pathlib.Path(a.allow).read_text().split(), a.swap_host or ["api.anthropic.com"],
                   dummy, real, pathlib.Path(a.dir), a.log, port=a.port)
    except ValueError as err:
        raise SystemExit("egress: %s" % err)
    print(json.dumps({"port": e.port, "ca": str(e.ca_pem)}), flush=True)
    e.serve_forever()


if __name__ == "__main__":
    main()

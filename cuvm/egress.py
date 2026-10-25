"""Per-run egress proxy: the real login token stays on the host (docs/design.md, Credentials).

Only CONNECT to allowlisted hosts on port 443. Swap hosts (api.anthropic.com) are terminated with
leaf certificates from a per-run CA, one request per connection, and the guest's dummy token is
replaced by the real one in the Authorization or x-api-key header only. The dummy anywhere else in
a request is refused, never rewritten. Responses are scanned across reads and the connection is
closed before any part of the real token reaches the guest. Other allowlisted hosts get a plain
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
            st = {"status": None}
            try:
                outcome = self.relay(t, u, rest, st)
            except (ssl.SSLError, OSError) as e:  # either side went away mid-exchange; still logged
                outcome = "connection_lost"
                info["lost"] = type(e).__name__
            status = st["status"]
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

    def relay(self, t, u, sent_body, st):
        """Client body to upstream (dummy scanned across reads), response to client with its Connection
        header forced to close and the last len(real)-1 bytes held back until the next read or EOF,
        so a token split across reads is caught before any of it is sent. Returns the outcome; the
        response status line goes into st["status"]."""
        dummy, real = self.dummy.encode(), self.real.encode()
        ctail = sent_body[-(len(dummy) - 1):]
        keep = len(real) - 1
        rhead, pend = b"", None  # pend is None until the response head is complete
        socks = [t, u]
        while u in socks:
            r = _readable(socks, IDLE_S)
            if not r:
                if pend:
                    _send(t, pend)
                return "idle_timeout"
            for s in r:
                data = _recv(s)
                if data is None:
                    continue
                if s is t:
                    if not data:
                        socks.remove(t)  # client half-close: keep reading the response
                        continue
                    win = ctail + data
                    if dummy in win:
                        return "dummy_elsewhere"
                    _send(u, data)
                    ctail = win[-(len(dummy) - 1):]
                    continue
                if not data:
                    if pend is None:
                        if real in rhead:
                            return "real_in_response"
                        _send(t, rhead)  # incomplete head: pass what came, checked
                    elif pend:
                        _send(t, pend)
                    return "ok"
                if pend is None:
                    rhead += data
                    if real in rhead:
                        return "real_in_response"
                    if b"\r\n\r\n" not in rhead:
                        if len(rhead) > MAX_HEAD:
                            return "response head too large"
                        continue
                    h, pend = rhead.split(b"\r\n\r\n", 1)
                    hl = h.split(b"\r\n")
                    st["status"] = hl[0].decode("latin-1", "replace")[:80]
                    keep_lines = [l for l in hl[1:] if l.split(b":", 1)[0].strip().lower()
                                  not in (b"connection", b"keep-alive", b"proxy-connection")]
                    _send(t, b"\r\n".join([hl[0], *keep_lines, b"Connection: close"]) + b"\r\n\r\n")
                else:
                    pend += data
                if real in pend:
                    return "real_in_response"
                if len(pend) > keep:
                    _send(t, pend[:-keep])
                    pend = pend[-keep:]
        return "ok"


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

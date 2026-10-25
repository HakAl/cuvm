"""cuvm/egress.py against local TLS servers on 127.0.0.1 (no network, no keychain token)."""
import gzip
import http.client
import io
import json
import os
import pathlib
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

from cuvm import egress

REAL = "sk-ant-oat01-REALtokenABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghij"
DUMMY = "sk-ant-oat01-cuvm-dummy-0123456789abcdef0123456789abcdef"
SWAP = "api.anthropic.com"
OTHER = "other.example"


def openssl(*args):
    subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True)


def upstream_pki(d):
    """A CA the proxy trusts for upstream, and a server certificate for both test hosts."""
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "uca.key", "-out", d / "uca.pem",
            "-days", "1", "-subj", "/CN=test upstream CA", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "srv.key", "-out", d / "srv.csr", "-subj", "/CN=srv")
    (d / "srv.ext").write_text("subjectAltName=DNS:%s,DNS:%s\nextendedKeyUsage=serverAuth\n" % (SWAP, OTHER))
    openssl("x509", "-req", "-in", d / "srv.csr", "-CA", d / "uca.pem", "-CAkey", d / "uca.key", "-CAcreateserial",
            "-out", d / "srv.pem", "-days", "1", "-extfile", d / "srv.ext")


class Upstream:
    """TLS server: records each connection's bytes, answers with respond(received) after the request."""

    def __init__(self, d, respond):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(d / "srv.pem", d / "srv.key")
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.respond, self.got, self.conns = respond, [], 0
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conns += 1
            threading.Thread(target=self.one, args=(c,), daemon=True).start()

    def one(self, c):
        t = c
        try:
            t = self.ctx.wrap_socket(c, server_side=True)
            t.settimeout(5)
            buf = b""
            while True:
                try:
                    data = t.recv(65536)
                except (socket.timeout, ssl.SSLError, OSError):
                    break
                if not data:
                    break
                buf += data
                if complete(buf):
                    break
            self.got.append(buf)
            for part in self.respond(buf):
                t.sendall(part)
                time.sleep(0.05)
        except Exception:
            pass
        finally:
            t.close()

    def close(self):
        self.srv.close()


def complete(buf):
    if b"\r\n\r\n" not in buf:
        return False
    head, body = buf.split(b"\r\n\r\n", 1)
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == b"content-length":
            return len(body) >= int(v)
    return True


def decoded(raw):
    """What a standard HTTP client makes of the bytes the guest received: (status, body or error)."""
    class Sock:
        def makefile(self, mode):
            return io.BytesIO(raw)
    r = http.client.HTTPResponse(Sock())
    try:
        r.begin()
        return r.status, r.read()
    except (http.client.HTTPException, ValueError) as e:
        return None, type(e).__name__


def chunked(*parts, trailer=b""):
    return b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in parts) + b"0\r\n" + trailer + b"\r\n"


CHUNKED_HEAD = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"


def ok(body=b"hello", extra=b""):
    return lambda got: [b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n%s\r\n" % (len(body), extra), body]


class EgressTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = pathlib.Path(tempfile.mkdtemp())
        upstream_pki(cls.d)

    def start(self, respond=ok(), guest="127.0.0.1", allow=(SWAP, OTHER, "*.wild.example")):
        self.up = Upstream(self.d, respond)
        self.addCleanup(self.up.close)
        run = pathlib.Path(tempfile.mkdtemp())
        self.log = run / "egress.jsonl"
        uctx = ssl.create_default_context(cafile=str(self.d / "uca.pem"))
        port = self.up.port
        self.e = egress.Egress("127.0.0.1", guest, list(allow), [SWAP], DUMMY, REAL, run / "ca", self.log,
                               upstream_ctx=uctx, dial=lambda host, p: socket.create_connection(("127.0.0.1", port), 5))
        threading.Thread(target=self.e.serve_forever, daemon=True).start()
        self.addCleanup(self.e.close)
        self.ca = run / "ca" / "ca.pem"

    def events(self, ev=None):
        time.sleep(0.2)
        lines = [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []
        return [l for l in lines if ev is None or l["ev"] == ev]

    def tearDown(self):
        if hasattr(self, "log") and self.log.exists():
            text = self.log.read_text()
            for secret in (REAL, DUMMY, REAL[:20], DUMMY[:24]):
                self.assertNotIn(secret, text, "a token reached the log")

    def connect(self, target, head=b""):
        s = socket.create_connection(("127.0.0.1", self.e.port), 5)
        s.sendall(b"CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n%s" % (target.encode(), target.encode(), head))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        return s, resp.split(b"\r\n", 1)[0]

    def tls(self, host=SWAP, cafile=None):
        s, status = self.connect(host + ":443")
        self.assertIn(b" 200 ", status)
        ctx = ssl.create_default_context(cafile=str(cafile or self.ca))
        t = ctx.wrap_socket(s, server_hostname=host)
        t.settimeout(5)
        return t

    def exchange(self, parts, host=SWAP, cafile=None):
        t = self.tls(host, cafile)
        for p in parts:
            t.sendall(p)
            time.sleep(0.05)
        out = b""
        while True:
            try:
                chunk = t.recv(65536)
            except (OSError, ssl.SSLError):
                break
            if not chunk:
                break
            out += chunk
        t.close()
        return out

    def req(self, auth=DUMMY, path=b"/v1/models", extra=b"", body=b""):
        return (b"POST %s HTTP/1.1\r\nHost: api.anthropic.com\r\nAuthorization: Bearer %s\r\n"
                b"Accept-Encoding: gzip, br\r\nConnection: keep-alive\r\nContent-Length: %d\r\n%s\r\n"
                % (path, auth.encode(), len(body), extra)) + body

    # swap
    def test_authorization_swapped_for_real_token_upstream_only(self):
        self.start()
        out = self.exchange([self.req()])
        self.assertTrue(out.startswith(b"HTTP/1.1 200 OK"), out[:80])
        self.assertTrue(out.endswith(b"hello"))
        got = self.up.got[0]
        self.assertIn(b"Authorization: Bearer " + REAL.encode() + b"\r\n", got)
        self.assertNotIn(DUMMY.encode(), got)
        self.assertIn(b"Accept-Encoding: identity\r\n", got)
        self.assertNotIn(b"gzip", got)
        self.assertIn(b"Connection: close\r\n", got)
        self.assertNotIn(b"keep-alive", got)
        r = self.events("request")[0]
        self.assertEqual((r["host"], r["method"], r["path"], r["swapped"], r["outcome"]),
                         (SWAP, "POST", "/v1/models", ["authorization"], "ok"))
        self.assertEqual(r["status"], "HTTP/1.1 200 OK")

    def test_x_api_key_swapped(self):
        self.start()
        self.exchange([b"GET / HTTP/1.1\r\nHost: api.anthropic.com\r\nx-api-key: %s\r\n\r\n" % DUMMY.encode()])
        self.assertIn(b"x-api-key: " + REAL.encode() + b"\r\n", self.up.got[0])
        self.assertEqual(self.events("request")[0]["swapped"], ["x-api-key"])

    def test_body_without_dummy_forwarded_intact(self):
        self.start()
        body = bytes(range(256)) * 400
        full = self.req(body=body)
        self.exchange([full[:3000], full[3000:]])
        self.assertTrue(self.up.got[0].endswith(body))

    def test_query_not_logged(self):
        self.start()
        self.exchange([self.req(path=b"/v1/x?secret=abc")])
        r = self.events("request")[0]
        self.assertEqual((r["path"], r["query"]), ("/v1/x", True))
        self.assertNotIn("secret=abc", self.log.read_text())

    # dummy outside the Authorization header: refused, never rewritten
    def test_dummy_in_body_split_across_writes_refused(self):
        self.start()
        body = b'{"x":"' + DUMMY.encode() + b'"}'
        cut = 6 + len(DUMMY) // 2
        head = self.req(body=b"")[:-2].replace(b"Content-Length: 0", b"Content-Length: %d" % len(body)) + b"\r\n"
        self.exchange([head + body[:cut], body[cut:]])
        sent = b"".join(self.up.got)
        self.assertNotIn(DUMMY.encode(), sent)
        self.assertNotIn(REAL.encode() + b'"', sent)
        self.assertEqual(self.events("dummy_elsewhere")[0]["where"], "body")
        self.assertEqual(self.events("request")[0]["outcome"], "dummy_elsewhere")

    def test_dummy_in_path_refused_before_upstream(self):
        self.start()
        out = self.exchange([self.req(path=b"/v1/x/" + DUMMY.encode())])
        self.assertIn(b" 403 ", out.split(b"\r\n", 1)[0])
        self.assertEqual(self.up.conns, 0)
        self.assertEqual(self.events("dummy_elsewhere")[0]["where"], "request line")

    def test_dummy_in_other_header_refused(self):
        self.start()
        out = self.exchange([self.req(extra=b"X-Echo: %s\r\n" % DUMMY.encode())])
        self.assertIn(b" 403 ", out.split(b"\r\n", 1)[0])
        self.assertEqual(self.up.conns, 0)
        self.assertEqual(self.events("dummy_elsewhere")[0]["where"], "header x-echo")

    def test_dummy_in_body_start_refused_before_upstream(self):
        self.start()
        out = self.exchange([self.req(body=DUMMY.encode())])
        self.assertIn(b" 403 ", out.split(b"\r\n", 1)[0])
        self.assertEqual(self.up.conns, 0)

    def test_other_token_not_swapped(self):
        self.start(respond=lambda got: [b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"])
        other = "sk-ant-oat01-somethingelse"
        self.exchange([self.req(auth=other)])
        self.assertIn(other.encode(), self.up.got[0])
        self.assertNotIn(REAL.encode(), self.up.got[0])
        r = self.events("request")[0]
        self.assertEqual((r["swapped"], r["other_token_headers"]), ([], ["authorization"]))

    # header smuggling (TI-W1)
    def refused_before_upstream(self, raw, why_ev="dummy_elsewhere"):
        self.start()
        out = self.exchange([raw])
        self.assertIn(b" 403 ", out.split(b"\r\n", 1)[0])
        self.assertEqual(self.up.conns, 0)
        self.assertTrue(self.events(why_ev), self.events())

    def test_bare_lf_inside_authorization_refused(self):
        d = DUMMY.encode()
        self.refused_before_upstream(b"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\n"
                                     b"Authorization: Bearer x\nX-Smuggled: " + d + b"\n\n{\"prompt\":\"" + d + b"\"}\r\n\r\n",
                                     "refused")

    def test_bare_cr_in_header_refused(self):
        self.refused_before_upstream(self.req(extra=b"X-A: a\rb\r\n"), "refused")

    def test_authorization_must_be_exactly_bearer_dummy(self):
        self.refused_before_upstream(self.req(auth=DUMMY + " " + DUMMY))

    def test_second_swap_header_refused(self):
        self.refused_before_upstream(self.req(extra=b"x-api-key: %s\r\n" % DUMMY.encode()))

    def test_whitespace_before_colon_refused(self):
        self.refused_before_upstream(b"GET / HTTP/1.1\r\nHost: api.anthropic.com\r\nAuthorization : Bearer %s\r\n\r\n"
                                     % DUMMY.encode(), "refused")

    def test_tls_client_half_close_still_gets_response(self):
        # TCP FIN without close_notify after the request; the response comes later (TI-W2).
        self.start(respond=lambda got: [b"", b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"])
        t = self.tls()
        t.sendall(self.req())
        with socket.socket(fileno=os.dup(t.fileno())) as dup:
            dup.shutdown(socket.SHUT_WR)
        out = b""
        while True:
            try:
                chunk = t.recv(65536)
            except (OSError, ssl.SSLError):
                break
            if not chunk:
                break
            out += chunk
        t.close()
        self.assertTrue(out.endswith(b"hello"), out)
        self.assertEqual([e["outcome"] for e in self.events("request")], ["ok"])
        self.assertEqual(self.events("error"), [])

    def test_client_gone_mid_response_still_logs_request(self):
        self.start(respond=lambda got: [b"HTTP/1.1 200 OK\r\nContent-Length: 300000\r\n\r\n", b"z" * 100000,
                                        b"z" * 100000, b"z" * 100000])
        t = self.tls()
        t.sendall(self.req())
        t.recv(10)
        socket.socket(fileno=t.detach()).close()
        time.sleep(0.5)
        r = self.events("request")
        self.assertEqual(len(r), 1, self.events())
        self.assertEqual((r[0]["outcome"], r[0]["swapped"]), ("connection_lost", ["authorization"]))
        self.assertEqual(self.events("error"), [])

    # HTTP framing (Lookout COMPUTERUSE-002 round 3 F1, F2)
    def test_real_token_split_across_http_chunks_never_reaches_client(self):
        cut = len(REAL) // 2
        self.start(respond=lambda got: [CHUNKED_HEAD + chunked(b"abc" + REAL[:cut].encode(), REAL[cut:].encode() + b"x" * 300)])
        out = self.exchange([self.req()])
        body = decoded(out)[1]
        self.assertFalse(isinstance(body, bytes) and REAL[:12].encode() in body, body)
        self.assertNotIn(REAL[:12].encode(), out)
        self.assertEqual(self.events("request")[0]["outcome"], "real_in_response")

    def test_real_token_split_one_byte_per_chunk_and_write(self):
        body = CHUNKED_HEAD + chunked(*[bytes([b]) for b in b"zz" + REAL.encode() + b"zz"])
        self.start(respond=lambda got: [body[i:i + 7] for i in range(0, len(body), 7)])
        out = self.exchange([self.req()])
        body = decoded(out)[1]
        self.assertFalse(isinstance(body, bytes) and REAL[:12].encode() in body, body)
        self.assertNotIn(REAL[:12].encode(), out.replace(b"\r\n1\r\n", b""))
        self.assertEqual(self.events("request")[0]["outcome"], "real_in_response")

    def test_real_token_in_chunked_trailer_not_forwarded(self):
        wire = CHUNKED_HEAD + chunked(b"fine", trailer=b"X-T: " + REAL.encode() + b"\r\n")
        cut = wire.index(b"X-T")
        self.start(respond=lambda got: [wire[:cut], wire[cut:]])  # trailer after the head scan
        out = self.exchange([self.req()])
        self.assertNotIn(REAL[:12].encode(), out)
        self.assertEqual(decoded(out), (200, b"fine"))
        self.assertEqual(self.events("request")[0]["outcome"], "ok")

    def test_compressed_response_refused(self):
        gz = gzip.compress(b'{"echo":"' + REAL.encode() + b'"}')
        self.start(respond=lambda got: [b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % len(gz), gz])
        out = self.exchange([self.req()])
        self.assertNotIn(gz[10:30], out)
        self.assertEqual(decoded(out)[0], 502)
        r = self.events("request")[0]
        self.assertEqual((r["outcome"], r["why"]), ("bad_response", "content-encoding gzip"))

    def test_other_transfer_coding_refused(self):
        self.start(respond=lambda got: [b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n", chunked(b"x")])
        out = self.exchange([self.req()])
        self.assertEqual(decoded(out)[0], 502)
        self.assertEqual(self.events("request")[0]["outcome"], "bad_response")

    def test_chunked_stream_decodes_identically(self):
        parts = [b"data: %d %s\n\n" % (i, b"q" * (i * 37 % 500)) for i in range(400)]
        wire = CHUNKED_HEAD + chunked(*parts)
        self.start(respond=lambda got: [wire[i:i + 1000] for i in range(0, len(wire), 1000)])
        out = self.exchange([self.req()])
        self.assertEqual(decoded(out), (200, b"".join(parts)))
        self.assertEqual(self.events("request")[0]["outcome"], "ok")

    def test_bad_chunk_size_refused(self):
        self.start(respond=lambda got: [CHUNKED_HEAD + b"zz\r\nabc\r\n0\r\n\r\n"])
        self.exchange([self.req()])
        self.assertEqual(self.events("request")[0]["outcome"], "bad_response")

    def test_informational_response_then_final(self):
        self.start(respond=lambda got: [b"HTTP/1.1 100 Continue\r\n\r\n", b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"])
        out = self.exchange([self.req()])
        self.assertTrue(out.endswith(b"hi"), out)
        self.assertEqual(self.events("request")[0]["status"], "HTTP/1.1 200 OK")

    # response head hygiene and truncation (agy r3 review R3-F1, R3-F2)
    def test_response_head_framing_tricks_refused(self):
        gz = gzip.compress(REAL.encode())
        cases = {
            "bare lf hides content-encoding": b"HTTP/1.1 200 OK\r\nX-Pad: 1\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % len(gz) + gz,
            "obs-fold content-encoding": b"HTTP/1.1 200 OK\r\nContent-Encoding:\r\n gzip\r\nContent-Length: %d\r\n\r\n" % len(gz) + gz,
            "space before colon": b"HTTP/1.1 200 OK\r\nContent-Encoding : gzip\r\nContent-Length: %d\r\n\r\n" % len(gz) + gz,
            "empty content-encoding": b"HTTP/1.1 200 OK\r\nContent-Encoding:\r\nContent-Length: 2\r\n\r\nhi",
            "duplicate chunked": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked(chunked(b"hi")),
            "switching protocols": b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: x\r\n\r\nraw bytes",
        }
        for name, wire in cases.items():
            with self.subTest(name):
                self.start(respond=lambda got, wire=wire: [wire])
                out = self.exchange([self.req()])
                self.assertNotIn(gz[10:30], out)
                self.assertEqual(decoded(out)[0], 502, out[:200])
                self.assertEqual(self.events("request")[-1]["outcome"], "bad_response")
                self.tearDown()

    def test_truncated_responses_are_bad_response(self):
        cases = {"no head": [b"HTTP/1.1 200 OK\r\nX: y"], "nothing": [b""],
                 "chunked without terminal chunk": [CHUNKED_HEAD + b"5\r\nhello\r\n"]}
        for name, parts in cases.items():
            with self.subTest(name):
                self.start(respond=lambda got, parts=parts: parts)
                self.exchange([self.req()])
                self.assertEqual(self.events("request")[-1]["outcome"], "bad_response")
                self.tearDown()

    def test_pipelined_second_request_refused_before_upstream(self):
        self.start()
        out = self.exchange([self.req() + self.req(path=b"/v1/second")])
        self.assertIn(b" 403 ", out.split(b"\r\n", 1)[0])
        self.assertEqual(self.up.conns, 0)
        self.assertEqual(self.events("refused")[0]["why"], "bytes after the request")

    def test_late_second_request_not_forwarded(self):
        self.start(respond=lambda got: [b"", b"", b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"])
        first = self.req(body=b"{}")
        self.exchange([first, self.req(path=b"/v1/second")])
        self.assertNotIn(b"/v1/second", b"".join(self.up.got))
        self.assertEqual(self.events("request")[0]["outcome"], "surplus_request_bytes")

    def test_request_transfer_coding_refused(self):
        self.start()
        out = self.exchange([b"POST /v1/x HTTP/1.1\r\nHost: api.anthropic.com\r\nAuthorization: Bearer %s\r\n"
                             b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n" % DUMMY.encode()])
        self.assertIn(b" 403 ", out.split(b"\r\n", 1)[0])
        self.assertEqual(self.up.conns, 0)
        self.assertEqual(self.events("refused")[0]["why"], "request transfer coding")

    def test_bad_content_length_refused(self):
        self.start()
        out = self.exchange([self.req().replace(b"Content-Length: 0", b"Content-Length: 1, 1")])
        self.assertIn(b" 403 ", out.split(b"\r\n", 1)[0])
        self.assertEqual(self.up.conns, 0)

    def test_non_ascii_digit_content_length_refused(self):
        self.start()
        out = self.exchange([self.req().replace(b"Content-Length: 0", b"Content-Length: \xb2")])
        self.assertIn(b" 403 ", out.split(b"\r\n", 1)[0])
        self.assertEqual(self.up.conns, 0)
        self.assertEqual(self.events("refused")[0]["why"], "bad content-length")

    def test_content_length_dropped_when_rechunked(self):
        self.start(respond=lambda got: [b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked(b"hello")])
        out = self.exchange([self.req()])
        self.assertNotIn(b"content-length", out.split(b"\r\n\r\n", 1)[0].lower())
        self.assertEqual(decoded(out), (200, b"hello"))

    # responses
    def test_real_token_split_across_response_chunks_never_reaches_client(self):
        cut = len(REAL) // 2
        self.start(respond=lambda got: [b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\nabc" + REAL[:cut].encode(),
                                        REAL[cut:].encode() + b"x" * 300])
        out = self.exchange([self.req()])
        self.assertNotIn(REAL[:12].encode(), out)
        self.assertEqual(self.events("request")[0]["outcome"], "real_in_response")
        self.assertEqual(len(self.events("real_in_response")), 1)

    def test_real_token_in_response_head_never_reaches_client(self):
        self.start(respond=lambda got: [b"HTTP/1.1 200 OK\r\nX-Reflect: " + REAL.encode() + b"\r\nContent-Length: 0\r\n\r\n"])
        out = self.exchange([self.req()])
        self.assertNotIn(REAL[:12].encode(), out)
        self.assertEqual(self.events("request")[0]["outcome"], "real_in_response")

    def test_response_connection_forced_close_and_large_body_intact(self):
        body = b"y" * (1 << 20)
        self.start(respond=lambda got: [b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nKeep-Alive: timeout=5\r\n"
                                        b"Content-Length: %d\r\n\r\n" % len(body), body[:1000], body[1000:]])
        out = self.exchange([self.req()])
        head, rest = out.split(b"\r\n\r\n", 1)
        self.assertIn(b"\r\nConnection: close", head)
        self.assertNotIn(b"keep-alive", head.lower())
        self.assertEqual(rest, body)

    def test_client_dropping_tls_mid_head_is_no_request(self):
        self.start()
        t = self.tls()
        t.sendall(b"GET / HTTP/1.1\r\n")  # partial head, then the TCP connection goes without close_notify
        time.sleep(0.05)  # the session ticket is unread now, so close() sends RST (TI-W2)
        socket.socket(fileno=t.detach()).close()
        self.assertEqual([e["ev"] for e in self.events() if e["ev"] != "listening"], ["no_request"])
        self.assertEqual(self.up.conns, 0)

    def test_alpn_is_http11_only(self):
        self.start()
        s, _ = self.connect(SWAP + ":443")
        ctx = ssl.create_default_context(cafile=str(self.ca))
        ctx.set_alpn_protocols(["h2", "http/1.1"])
        t = ctx.wrap_socket(s, server_hostname=SWAP)
        self.assertEqual(t.selected_alpn_protocol(), "http/1.1")
        t.close()

    # refusals
    def test_refusals_logged_with_403(self):
        self.start()
        for target, why in (("example.com:443", "not allowlisted"), ("api.anthropic.com:8443", "port"),
                            ("127.0.0.1:443", "not allowlisted"), ("claude.com.example.net:443", "not allowlisted")):
            s, status = self.connect(target)
            s.close()
            self.assertIn(b" 403 ", status, target)
        refused = self.events("refused")
        self.assertEqual([(r["host"], r["port"], r["why"]) for r in refused],
                         [("example.com", "443", "not allowlisted"), ("api.anthropic.com", "8443", "port"),
                          ("127.0.0.1", "443", "not allowlisted"), ("claude.com.example.net", "443", "not allowlisted")])
        self.assertEqual(self.up.conns, 0)

    def test_plain_http_refused(self):
        self.start()
        s = socket.create_connection(("127.0.0.1", self.e.port), 5)
        s.sendall(b"GET http://api.anthropic.com/v1/x?t=%s HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n" % DUMMY.encode())
        self.assertIn(b" 403 ", s.recv(4096).split(b"\r\n", 1)[0])
        s.close()
        r = self.events("refused")[0]
        self.assertEqual((r["method"], r["host"], r["why"]), ("GET", SWAP, "not CONNECT"))

    def test_unauthorized_client_dropped(self):
        self.start(guest="192.0.2.9")
        s = socket.create_connection(("127.0.0.1", self.e.port), 5)
        s.sendall(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
        try:
            self.assertEqual(s.recv(4096), b"")
        except ConnectionResetError:  # closed with the request unread
            pass
        s.close()
        self.assertEqual(self.events("unauthorized")[0]["client"], "127.0.0.1")
        self.assertEqual(self.up.conns, 0)

    # pass-through tunnel
    def test_non_swap_host_is_plain_tunnel_not_intercepted(self):
        self.start()
        # The client trusts only the upstream CA: this works only if the proxy did not terminate TLS.
        out = self.exchange([b"GET / HTTP/1.1\r\nHost: other.example\r\nAuthorization: Bearer %s\r\n\r\n" % DUMMY.encode()],
                            host=OTHER, cafile=self.d / "uca.pem")
        self.assertTrue(out.endswith(b"hello"))
        self.assertIn(DUMMY.encode(), self.up.got[0])  # never inspected or rewritten
        t = self.events("tunnel")[0]
        self.assertEqual(t["host"], OTHER)
        self.assertGreater(t["bytes_up"], 0)
        self.assertEqual(self.events("request"), [])

    def test_wildcard_allow_tunnels(self):
        self.start()
        s, status = self.connect("a.wild.example:443")
        s.close()
        self.assertIn(b" 200 ", status)

    def test_tunnel_forwards_early_bytes_and_half_close(self):
        got = []
        srv = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(srv.close)

        def plain():
            c, _ = srv.accept()
            buf = b""
            while chunk := c.recv(100):
                buf += chunk
            got.append(buf)
            c.sendall(b"after eof")
            c.close()
        threading.Thread(target=plain, daemon=True).start()
        run = pathlib.Path(tempfile.mkdtemp())
        self.log = run / "egress.jsonl"
        port = srv.getsockname()[1]
        self.e = egress.Egress("127.0.0.1", "127.0.0.1", [OTHER, SWAP], [SWAP], DUMMY, REAL, run / "ca", self.log,
                               dial=lambda host, p: socket.create_connection(("127.0.0.1", port), 5))
        threading.Thread(target=self.e.serve_forever, daemon=True).start()
        self.addCleanup(self.e.close)
        s, status = self.connect(OTHER + ":443", head=b"early")
        s.sendall(b"-late")
        s.shutdown(socket.SHUT_WR)
        reply = s.recv(100)
        s.close()
        self.assertEqual(got, [b"early-late"])
        self.assertEqual(reply, b"after eof")

    # certificates and startup
    def test_ca_and_leaf_validity_and_key_mode(self):
        self.start()
        ca_dir = self.ca.parent
        self.assertEqual(oct((ca_dir / "ca.key").stat().st_mode & 0o777), "0o600")
        end = subprocess.run(["openssl", "x509", "-in", ca_dir / "ca.pem", "-noout", "-enddate"],
                             capture_output=True, text=True).stdout
        self.assertIn("notAfter=", end)
        days = subprocess.run(["openssl", "x509", "-in", ca_dir / "ca.pem", "-noout", "-checkend", str(3 * 86400)],
                              capture_output=True).returncode
        self.assertEqual(days, 1, "CA must expire within 3 days")
        self.assertFalse((ca_dir / (OTHER + ".pem")).exists(), "leaf certificates only for swap hosts")

    @unittest.skipUnless(pathlib.Path("/usr/bin/openssl").exists(), "macOS system openssl")
    def test_certificates_with_macos_system_openssl(self):
        d = pathlib.Path(tempfile.mkdtemp()) / "ca"
        with unittest.mock.patch.dict(os.environ, PATH="/usr/bin:/bin"):
            egress.make_ca(d)
            ctx = egress.make_leaf(d, SWAP)
        v = subprocess.run(["/usr/bin/openssl", "verify", "-CAfile", d / "ca.pem", d / (SWAP + ".pem")],
                           capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        self.assertIsInstance(ctx, ssl.SSLContext)

    def test_swap_host_must_be_allowlisted(self):
        with self.assertRaises(ValueError):
            egress.Egress("127.0.0.1", "127.0.0.1", [OTHER], [SWAP], DUMMY, REAL, pathlib.Path(tempfile.mkdtemp()),
                          pathlib.Path(tempfile.mkdtemp()) / "l")

    def test_bad_tokens_rejected(self):
        for dummy, real in ((DUMMY, "nope"), ("", REAL), (REAL, REAL), (DUMMY[:10], REAL)):
            with self.assertRaises(ValueError):
                egress.Egress("127.0.0.1", "127.0.0.1", [SWAP], [SWAP], dummy, real, pathlib.Path(tempfile.mkdtemp()),
                              pathlib.Path(tempfile.mkdtemp()) / "l")

    @unittest.skipUnless(pathlib.Path("/usr/bin/security").exists(), "macOS keychain")
    def test_main_exits_when_keychain_item_missing(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "allow").write_text(SWAP + "\n")
        p = subprocess.run([sys.executable, "-I", egress.__file__, "--listen", "127.0.0.1", "--guest", "127.0.0.1",
                            "--allow", d / "allow", "--dir", d / "ca", "--log", d / "log",
                            "--keychain-item", "cuvm-test-absent-item-%d" % time.time_ns()],
                           input=DUMMY + "\n", capture_output=True, text=True, timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("keychain", p.stderr)
        self.assertNotIn("sk-ant-", p.stdout + p.stderr)


class SummaryTest(unittest.TestCase):
    def test_summarize(self):
        lines = [{"ev": "listening", "port": 1},
                 {"ev": "request", "host": SWAP, "swapped": ["authorization"], "outcome": "ok", "status": "HTTP/1.1 200 OK"},
                 {"ev": "request", "host": SWAP, "swapped": [], "outcome": "ok", "status": "HTTP/1.1 200 OK"},
                 {"ev": "tunnel", "host": OTHER},
                 {"ev": "refused", "host": "example.com", "why": "not allowlisted"},
                 {"ev": "refused", "host": "example.com", "why": "not allowlisted"},
                 {"ev": "unauthorized", "client": "192.168.128.1"},
                 {"ev": "dummy_elsewhere", "host": SWAP, "where": "body"},
                 {"ev": "real_in_response", "host": SWAP},
                 {"ev": "request", "host": SWAP, "outcome": "bad_response", "why": "content-encoding gzip"},
                 {"ev": "request", "host": SWAP, "outcome": "surplus_request_bytes"}]
        s = egress.summarize("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
        self.assertEqual(s["allowed"], [SWAP, OTHER])
        timed_out = egress.summarize(json.dumps({"ev": "request", "host": "x.example", "outcome": "idle_timeout"}))
        self.assertEqual(timed_out["allowed"], ["x.example"])  # contacted counts, whatever the outcome (TI-W3)
        self.assertEqual(s["refused"], ["example.com"])
        self.assertEqual(s["unauthorized"], ["192.168.128.1"])
        self.assertEqual((s["requests"], s["swapped"]), (4, 1))
        self.assertEqual((s["bad_response"], s["surplus_request_bytes"]), (1, 1))
        self.assertEqual((s["dummy_elsewhere"], s["real_in_response"], s["unparsed_lines"]), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()

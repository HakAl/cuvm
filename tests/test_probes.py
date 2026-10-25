import base64
import json
import subprocess
import unittest

from cuvm import probes


def fake(name, exit_, out):
    return "P\t%s\t%d\t%s" % (name, exit_, base64.b64encode(out.encode()).decode())


def elog(*events):
    return "".join(json.dumps(dict(at="t", **e)) + "\n" for e in events)


UPSTREAM = {"ev": "request", "host": "api.anthropic.com", "method": "GET", "path": "/", "status": "HTTP/1.1 404 Not Found",
            "outcome": "ok", "swapped": []}


class ProbeTest(unittest.TestCase):
    def test_script_runs_and_roundtrips(self):
        s = probes.script([("A", "echo 'hi there'; exit 3"), ("B:x", "printf 'a\\tb'")], "HOST")
        r = subprocess.run(["zsh", "-c", s], capture_output=True, text=True)
        p = probes.parse(r.stdout)
        self.assertEqual(p["A"], {"exit": 3, "out": "hi there"})
        self.assertEqual(p["B:x"]["out"], "a\tb")

    def test_judge_feas2_values(self):
        out = "\n".join([
            fake("N1", 0, "404 0"),
            fake("N2a", 56, "curl: (56) CONNECT tunnel failed, response 403\n403"),
            fake("N2d", 56, "curl: (56) CONNECT tunnel failed, response 403\n403"),
            fake("N2f", 0, "403"),
            fake("F2", 1, "touch: /Volumes/My Shared Files/inputs/x: Operation not permitted"),
            fake("F4:/hosthome/alice", 1, "ls: /hosthome/alice: No such file or directory"),
            fake("N2e-udp", 9, ";; connection timed out; no servers could be reached"),
            fake("I1", 0, "uid=501 host=guest-vm.local\n0\n0\n0"),
        ])
        log = elog(UPSTREAM, {"ev": "refused", "method": "CONNECT", "host": "example.com", "port": "443", "why": "not allowlisted"},
                   {"ev": "refused", "method": "CONNECT", "host": "api.anthropic.com", "port": "8443", "why": "port"},
                   {"ev": "refused", "method": "GET", "host": "api.anthropic.com", "why": "not CONNECT"}) + "not json\n"
        j = probes.judge(probes.parse(out), "x", 501, "alices-mac", log, "192.168.128.1")
        self.assertTrue(all(v["pass"] for v in j.values()), {k: v for k, v in j.items() if not v["pass"]})

    def test_denied_without_log_line_fails(self):
        j = probes.judge(probes.parse(fake("N2a", 56, "403")), "x", 501, "h", "", "192.168.128.1")
        self.assertFalse(j["N2a"]["pass"])

    def test_e1_needs_this_runs_port_and_ca(self):
        ca = "\nNODE_EXTRA_CA_CERTS=/Users/admin/.cuvm/ca.pem"
        stale = fake("E1", 0, "HTTPS_PROXY=http://192.168.128.1:8888\nDISABLE_AUTOUPDATER=1" + ca)
        no_ca = fake("E1", 0, "HTTPS_PROXY=http://192.168.128.1:52426\nDISABLE_AUTOUPDATER=1")
        good = fake("E1", 0, "HTTPS_PROXY=http://192.168.128.1:52426\nDISABLE_AUTOUPDATER=1" + ca)
        for out, want in ((stale, False), (no_ca, False), (good, True)):
            self.assertIs(probes.judge(probes.parse(out), "x", 501, "h", "", "192.168.128.1", 52426)["E1"]["pass"], want)

    def test_n1_needs_upstream_response_in_log(self):
        res = probes.parse(fake("N1", 0, "404 0"))
        self.assertFalse(probes.judge(res, "x", 501, "h", "", "g")["N1"]["pass"])
        self.assertFalse(probes.judge(res, "x", 501, "h", elog(dict(UPSTREAM, outcome="real_in_response")), "g")["N1"]["pass"])
        self.assertTrue(probes.judge(res, "x", 501, "h", elog(UPSTREAM), "g")["N1"]["pass"])
        self.assertFalse(probes.judge(probes.parse(fake("N1", 0, "404 1")), "x", 501, "h", elog(UPSTREAM), "g")["N1"]["pass"])

    def test_n2f_plain_http_needs_403_and_refusal(self):
        res = probes.parse(fake("N2f", 0, "403"))
        refusal = {"ev": "refused", "method": "GET", "host": "api.anthropic.com", "why": "not CONNECT"}
        self.assertFalse(probes.judge(res, "x", 501, "h", "", "g")["N2f"]["pass"])
        self.assertTrue(probes.judge(res, "x", 501, "h", elog(refusal), "g")["N2f"]["pass"])
        self.assertFalse(probes.judge(probes.parse(fake("N2f", 0, "200")), "x", 501, "h", elog(refusal), "g")["N2f"]["pass"])

    def test_refusal_must_name_the_host(self):
        res = probes.parse(fake("N2a", 56, "403"))
        other = {"ev": "refused", "host": "example.org", "port": "443", "why": "not allowlisted"}
        self.assertFalse(probes.judge(res, "x", 501, "h", elog(other), "g")["N2a"]["pass"])

    def test_n1_trusts_only_the_run_ca(self):
        n1 = dict(probes.commands("g", 1, "/hosthome/alice", "/r", []))["N1"]
        self.assertIn("--cacert ~/.cuvm/ca.pem", n1)

    def test_admin_host_user_skips_home_paths(self):
        names = [n for n, _ in probes.commands("g", 1, "/hosthome/admin", "/r", [])]
        self.assertEqual([n for n in names if n.startswith("F4:")], ["F4:/r"])

    def test_guest_private_key_fails_i1(self):
        j = probes.judge(probes.parse(fake("I1", 0, "uid=501 host=guest\n0\n0\n1")), "x", 501, "h", "", "g")
        self.assertFalse(j["I1"]["pass"])

    def test_dns_answer_fails(self):
        j = probes.judge(probes.parse(fake("N2e-udp", 0, ";; ->>HEADER<<- status: NOERROR\n;; ANSWER SECTION:")),
                         "x", 501, "h", "", "g")
        self.assertFalse(j["N2e-udp"]["pass"])


if __name__ == "__main__":
    unittest.main()

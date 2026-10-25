"""Containment probes (feasibility plan step 5 and step 11) as data.

The guest script prints one line per probe: P<TAB>name<TAB>exit<TAB>base64(output).
Pass rules run on the host. Proxy-log halves are checked against log lines
written after the probe batch started.
"""
import base64

SHARE = "/Volumes/My Shared Files"


def commands(gateway, port, host_home, host_repo, n3_targets):
    px = "http://%s:%d" % (gateway, port)
    c = [
        ("E1", "zsh -c \"env | grep -E 'HTTPS_PROXY|DISABLE_AUTOUPDATER'\""),
        ("N1", "curl -x %s -sS -m 15 -o /dev/null -w '%%{http_code} %%{ssl_verify_result}' https://api.anthropic.com/" % px),
        ("N2a", "curl -x %s -sS -m 15 -o /dev/null -w '%%{http_connect}' https://example.com/" % px),
        ("N2b-gw", "curl --noproxy '' -x %s -p -sS -m 15 -o /dev/null -w '%%{http_connect}' http://%s:443/" % (px, gateway)),
        ("N2b-lo", "curl --noproxy '' -x %s -p -sS -m 15 -o /dev/null -w '%%{http_connect}' http://127.0.0.1:443/" % px),
        ("N2c-1", "curl -x %s -sS -m 15 -o /dev/null -w '%%{http_connect}' https://claude.com.example.net/" % px),
        ("N2c-2", "curl -x %s -sS -m 15 -o /dev/null -w '%%{http_connect}' https://xclaude.com/" % px),
        ("N2d", "curl -x %s -sS -m 15 -o /dev/null -w '%%{http_connect}' https://api.anthropic.com:8443/" % px),
        ("N2e-udp", "dig +time=3 +tries=1 @%s example.com A" % gateway),
        ("N2e-tcp", "dig +tcp +time=3 +tries=1 @%s example.com A" % gateway),
        ("F1", "shasum -a 256 '%s/inputs/known.txt'" % SHARE),
        ("F2", "touch '%s/inputs/x'; e=$?; test -e '%s/inputs/x' && echo EXISTS; exit $e" % (SHARE, SHARE)),
        ("F3", "ls -A '%s'" % SHARE),
        ("I1", "echo uid=$(id -u) host=$(hostname); zsh -c env | grep -c -e '%s' -e \"$H\"; mount | grep -c -e '%s' -e \"$H\"; "
                "ls ~/.ssh 2>/dev/null | grep -c '^id_'" % (host_home, host_home)),
    ]
    # A host user named admin shares the guest's home paths; those three are skipped (feasibility step 5).
    homes = [] if host_home.rstrip("/").endswith("/admin") else [host_home, host_home + "/.ssh", host_home + "/.claude"]
    for p in homes + [host_repo]:
        c.append(("F4:" + p, "ls -d '%s'" % p))
    for addr, port_ in n3_targets:
        c.append(("N3:%s:%d" % (addr, port_), "nc -z -G 2 %s %d" % (addr, port_)))
    return c


def script(cmds, host_name):
    lines = ["H='%s'" % host_name,
             "p(){ n=$1; shift; o=$(eval \"$1\" 2>&1); e=$?; printf 'P\\t%s\\t%s\\t%s\\n' \"$n\" \"$e\" \"$(printf %s \"$o\" | base64)\"; }"]
    for name, cmd in cmds:
        lines.append("p %s %s" % (_q(name), _q(cmd)))
    return "\n".join(lines) + "\n"


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def parse(out):
    res = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[0] == "P":
            res[parts[1]] = {"exit": int(parts[2]), "out": base64.b64decode(parts[3]).decode("utf-8", "replace")}
    return res


def _denied(r):
    return r["exit"] == 56 or r["out"].strip().endswith("403")


def judge(res, known_sha, uid_host, host_name, log_after, gateway, port=None):
    """Return {name: {exit, out, pass, why}}. log_after: proxy log text written after the batch began."""
    out = {}

    def put(name, ok, why=""):
        r = res.get(name, {"exit": None, "out": "(not run)"})
        out[name] = dict(r, **{"pass": bool(ok) and name in res, "why": why})

    def logged(target):
        return ('filtered domain "%s"' % target) in log_after

    for name in res:
        r = res[name]
        if name == "E1":
            put(name, r["exit"] == 0 and ("HTTPS_PROXY=http://%s:%d" % (gateway, port)) in r["out"]
                and "DISABLE_AUTOUPDATER=1" in r["out"], "proxy must be this run's port")
        elif name == "N1":
            code, _, verify = r["out"].strip().partition(" ")
            put(name, r["exit"] == 0 and code in {"200", "400", "401", "403", "404"} and verify == "0")
        elif name == "N2a":
            put(name, _denied(r) and logged("example.com"), "needs 403 and a log line")
        elif name == "N2b-gw":
            put(name, _denied(r) and logged(gateway))
        elif name == "N2b-lo":
            put(name, _denied(r) and logged("127.0.0.1"))
        elif name == "N2c-1":
            put(name, _denied(r) and logged("claude.com.example.net"))
        elif name == "N2c-2":
            put(name, _denied(r) and logged("xclaude.com"))
        elif name == "N2d":
            put(name, _denied(r) and "CONNECT api.anthropic.com:8443" in log_after, "403 plus request line")
        elif name.startswith("N2e"):
            put(name, "ANSWER SECTION" not in r["out"] and "status: NOERROR" not in r["out"])
        elif name == "F1":
            put(name, r["exit"] == 0 and r["out"].split()[:1] == [known_sha])
        elif name == "F2":
            put(name, r["exit"] != 0 and "EXISTS" not in r["out"] and
                ("Read-only file system" in r["out"] or "Operation not permitted" in r["out"]))
        elif name == "F3":
            put(name, r["out"].strip() == "inputs")
        elif name.startswith("F4:"):
            put(name, r["exit"] != 0)
        elif name == "I1":
            first, *counts = r["out"].strip().splitlines()
            put(name, host_name not in first and all(c.strip() == "0" for c in counts))
        elif name.startswith("N3:"):
            out[name] = dict(r, **{"pass": True, "why": "measured: %s" % ("open" if r["exit"] == 0 else "closed"),
                                   "open": r["exit"] == 0})
    return out

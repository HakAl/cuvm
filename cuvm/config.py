"""Experiment config: load, validate strictly, hash.

Relative paths (`inputs`, `net_allow`) resolve against the config file's directory.
`net_allow` is optional and defaults to cuvm/data/allow-run.txt.
"""
import hashlib
import json
import pathlib

from cuvm import DATA

KEYS = {
    "agent": str, "agent_args": list, "image": str, "plan": str,
    "inputs": str, "results": str, "deadline_s": int, "apps": list,
    "grants": list, "allow_fullscreen": bool, "prompt": str, "done": dict, "checks": list,
}
OPTIONAL = {"net_allow": str}


class ConfigError(ValueError):
    pass


def load(path):
    path = pathlib.Path(path).resolve()
    raw = path.read_bytes()
    try:
        c = json.loads(raw)
    except ValueError as e:
        raise ConfigError("not JSON: %s" % e)
    missing = sorted(set(KEYS) - set(c))
    unknown = sorted(set(c) - set(KEYS) - set(OPTIONAL))
    if missing or unknown:
        raise ConfigError("missing %s, unknown %s" % (missing, unknown))
    for k, t in {**KEYS, **OPTIONAL}.items():
        if k in c and (not isinstance(c[k], t) or (t is int and isinstance(c[k], bool))):
            raise ConfigError("%s must be %s" % (k, t.__name__))
    if c["agent"] != "claude":
        raise ConfigError("only agent 'claude' is implemented")
    if not c["apps"]:
        raise ConfigError("apps must list at least one app")
    c["inputs"] = str(path.parent / c["inputs"])
    c["net_allow"] = str(path.parent / c["net_allow"]) if "net_allow" in c else str(DATA / "allow-run.txt")
    if not (pathlib.Path(c["inputs"]) / "known.txt").is_file():
        raise ConfigError("inputs dir needs known.txt (probe F1 reads it): %s" % c["inputs"])
    return c, hashlib.sha256(raw).hexdigest()

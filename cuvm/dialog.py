"""Read an agent approval dialog from a tmux pane and decide it against the config.

Fails closed: a dialog that is not recognized, or that lists anything the parser
does not understand, is never approved.
"""
import re
from dataclasses import dataclass, field

ACCESS_TITLE = "Computer Use wants to control these apps"
ALLOW_ACCESS = "Allow for this session"
CONFIRM_FOOTER = "Enter to confirm"
# Section grammar of the 2.1.310 access dialog (K_e in the binary): title, then the
# model-written reason (with or without a "│" gutter), then from the first app row
# on only the lines below. Anything else after the first app row is "extra".
APP_ROW = re.compile(r"^◉ (.+)$")
ROW_START = ("◉ ", "◯ ", "✔ ")
GRANT_HEADER = "Also requested:"
GRANT_ROW = re.compile(r"^· (\w+)$")
HIDDEN = re.compile(r"^\d+ other apps? will be hidden while Claude works\.$")
DENY = re.compile(r"^(❯ )?Deny, and tell Claude what to do differently \(esc\)$")
ALLOW = re.compile(r"^(❯ )?Allow for this session \((\d+) apps?\)$")
FOOTER = "Enter to confirm · Esc to cancel"


@dataclass
class Dialog:
    kind: str  # access | fullscreen | unknown
    apps: list = field(default_factory=list)
    extra: list = field(default_factory=list)
    allow_option: bool = False
    text: str = ""
    grants: list = field(default_factory=list)
    allow_count: int = None


def _block(pane):
    """Return the lines of the last dialog in the pane, or None if none is open."""
    lines = pane.splitlines()
    footer = [i for i, l in enumerate(lines) if CONFIRM_FOOTER in l]
    if not footer:
        return None
    end = footer[-1]
    start = end
    while start > 0 and not lines[start - 1].startswith("────"):
        start -= 1
    return lines[start:end + 1]


def parse(pane):
    block = _block(pane)
    if block is None:
        return None
    text = "\n".join(block)
    stripped = [l.strip() for l in block if l.strip()]
    if ACCESS_TITLE in text:
        return _access(stripped, text)
    if "full screen" in text.lower() or "fullscreen" in text.lower():
        return Dialog("fullscreen", [], [], "Allow" in text, text)
    return Dialog("unknown", [], [], False, text)


def _access(stripped, text):
    d = Dialog("access", text=text)
    title = stripped.index(ACCESS_TITLE)
    d.extra += stripped[:title]
    body = stripped[title + 1:]
    first = next((i for i, l in enumerate(body) if l.startswith(ROW_START)), len(body))
    in_grants = False
    for l in body[first:]:
        m = APP_ROW.match(l)
        if m and not in_grants:
            d.apps.append(m.group(1).strip())
        elif l == GRANT_HEADER:
            in_grants = True
        elif in_grants and GRANT_ROW.match(l):
            d.grants.append(GRANT_ROW.match(l).group(1))
        elif HIDDEN.match(l) or DENY.match(l) or l == FOOTER:
            in_grants = False
        elif ALLOW.match(l):
            in_grants = False
            d.allow_option, d.allow_count = True, int(ALLOW.match(l).group(2))
        else:
            d.extra.append(l)
    return d


def decide(d, apps, grants, allow_fullscreen):
    """Return (approve, reason)."""
    if d.kind == "access":
        if not d.allow_option:
            return False, "allow option not found"
        outside = [a for a in d.apps if a not in apps]
        if outside:
            return False, "apps outside config: %s" % outside
        if not d.apps:
            return False, "no apps listed"
        if d.extra:
            return False, "unrecognized lines: %s" % d.extra
        if d.allow_count != len(d.apps):
            return False, "allow count %s != %d app rows" % (d.allow_count, len(d.apps))
        outside = [g for g in d.grants if g not in grants]
        if outside:
            return False, "grants outside config: %s" % outside
        return True, "apps %s within config" % d.apps
    if d.kind == "fullscreen":
        if not allow_fullscreen:
            return False, "full screen not allowed by config"
        # The 2.1.310 takeover card has not been captured, so there is no grammar to check it against.
        return False, "full-screen card text not yet captured; blocked even though config allows it"
    return False, "unrecognized dialog"

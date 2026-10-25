"""Mechanical extraction from a Claude Code transcript (feasibility plan step 10)."""
import base64
import hashlib
import json
import re

BATCH_INPUT = re.compile(r"^\[\d+/\d+\] (type|key): ")
INPUT_TOOLS = ("type", "key")


def _results(lines):
    """Yield (tool_use name, tool_result block) once per tool_use id, in order."""
    names, seen = {}, set()
    for line in lines:
        if not line.strip():
            continue
        o = json.loads(line)
        content = (o.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                names.setdefault(b["id"], b["name"])
            elif b.get("type") == "tool_result" and b.get("tool_use_id") not in seen:
                seen.add(b.get("tool_use_id"))
                yield names.get(b.get("tool_use_id"), "?"), b


REFUSED = "No such tool available"


def _executed(lines, forbidden):
    """Forbidden tools that may have run: every call except those Claude Code refused as unavailable.
    An is_error result alone is not a refusal (a shell command that exits non-zero is an error too),
    and a call with no result counts as executed."""
    calls, refused = {}, set()
    for line in lines:
        if not line.strip():
            continue
        content = (json.loads(line).get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") in forbidden:
                calls[b["id"]] = b["name"]
            elif b.get("type") == "tool_result" and b.get("is_error"):
                c = b.get("content")
                text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                if text.startswith("<tool_use_error>") and REFUSED in text:
                    refused.add(b.get("tool_use_id"))
    return sorted({name for i, name in calls.items() if i not in refused})


def extract(lines, allowed_prefixes, allowed_names):
    lines = list(lines)
    used = []
    for line in lines:
        if not line.strip():
            continue
        content = (json.loads(line).get("message") or {}).get("content")
        if isinstance(content, list):
            used += [b["name"] for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    tools = sorted(set(used))
    forbidden = [t for t in tools if t not in allowed_names and not t.startswith(allowed_prefixes)]
    executed = _executed(lines, set(forbidden))

    granted, input_ok_at, final = [], None, None
    for i, (name, b) in enumerate(_results(lines)):
        parts = b.get("content") if isinstance(b.get("content"), list) else []
        texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
        ok = not b.get("is_error")
        short = name.rsplit("__", 1)[-1]
        if short == "request_access" and ok:
            for t in texts:
                try:
                    granted += [g.get("displayName") for g in json.loads(t).get("granted", [])]
                except ValueError:
                    pass
        if ok and (short in INPUT_TOOLS or (short == "computer_batch" and any(BATCH_INPUT.match(t) for t in texts))):
            input_ok_at = i
        for p in parts:
            if p.get("type") == "image":
                final = (i, ok, p.get("source", {}))
    r = {"tools_used": tools, "forbidden_tools_attempted": forbidden, "forbidden_tools_used": executed,
         "request_access_granted": sorted(set(granted)),
         "input_tool_ok_before_final_shot": bool(final and input_ok_at is not None and input_ok_at <= final[0]),
         "final_image_sha256": None, "final_image_is_error": None, "final_image_media_type": None,
         "final_image_bytes": None}
    if final:
        raw = base64.b64decode(final[2].get("data", ""))
        r.update(final_image_sha256=hashlib.sha256(raw).hexdigest(), final_image_is_error=not final[1],
                 final_image_media_type=final[2].get("media_type"), final_image_bytes=raw)
    return r

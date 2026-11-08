"""Project snapshots for web-dev experiments (the web-dev design, docs/config.md).

A snapshot is one `.tgz` in the inputs directory with exactly one top-level directory. The run unpacks
it into a writable guest directory and, with `dev_server`, starts the configured command there on guest
loopback before the agent starts. Collection reports what changed.
"""
import bisect
import hashlib
import json
import os
import pathlib
import posixpath
import re
import shlex
import tarfile

NAME = re.compile(r"[A-Za-z0-9._-]+")
ARCHIVE = re.compile(r"[A-Za-z0-9._-]+\.tgz")
TOKEN = b"sk-ant-"
SKIP = {".git", ".DS_Store"}
SHARE_INPUTS = "/Volumes/My Shared Files/inputs"
GUEST_DIR = "~/exp/project"
CLEAN_DIR = "~/.cuvm/project-clean"
PATCH = "~/.cuvm/project.patch"
CHANGED = "~/.cuvm/project-changed.tgz"
LIST = "~/.cuvm/project-changed.txt"
# Files under the top directory, node_modules pruned; "<sha256>  ./<path>" per line. pipefail: a find that
# fails on a subtree must not read as a complete manifest (Lookout F2). xargs on empty input runs nothing.
MANIFEST_CMD = ("set -o pipefail; cd %s && find . -name node_modules -prune -o -type f -print0 </dev/null "
                "| xargs -0 shasum -a 256")
# The clean comparison copy needs no node_modules (the diff excludes it); bsdtar excludes the subtree.
CLEAN_EXCLUDE = "--exclude '*/node_modules' --exclude '*/node_modules/*'"
BAD_CHARS = re.compile(r"[\\\x00-\x1f\x7f]")  # shasum escapes these in its output; refused instead (Lookout F3)
# Home capture (the guest-output design): every regular file under the guest home except the harness's own
# directories, Claude Code's state, the Photos library (its daemons rewrite it) and the two trees collected on
# their own. One NUL-terminated record per file: "mtime ctime size ./path\n", written by one perl process
# reading find's NUL-separated names (the home-capture cleanup: a stat per file took two minutes for
# 60k files). A name that vanished between find and lstat is skipped; any other lstat failure fails the
# listing. A newline in a name stays inside its record and BAD_CHARS refuses it. pipefail: an unreadable
# directory fails the listing instead of thinning it.
HOME_STAT_CMD = (r"""perl -MErrno=ENOENT -0ne 'chomp; @s = lstat($_); """
                 r"""if (!@s) { $! == ENOENT ? next : die "lstat $_: $!\n" } print "$s[9] $s[10] $s[7] $_\n\0"'""")
HOME_LIST_CMD = ("set -o pipefail; cd %s && find . %s -prune -o -type f ! -name .DS_Store -print0 </dev/null | "
                 + HOME_STAT_CMD)
HOME_PRUNES = ("./Library", "./.claude", "./.claude.json", "./.claude.json.backup", "./.cuvm", "./.npm", "./.cache", "./.Trash",
               "./Pictures/Photos Library.photoslibrary", "./.local/state/claude")
HOME_LIST = "~/.cuvm/home-changed.txt"
HOME_CHANGED = "~/.cuvm/home-changed.tgz"
HOME_CAP_BYTES = 512 * 1024 * 1024
LOOPBACK = ("localhost", "127.0.0.1", "::1")
RESOLVE_BUDGET = 1000000  # path components visited while validating one archive's links; a few per link with the memo
MAX_NAME = 1024  # characters in a member name or link target (macOS PATH_MAX); checked first, so every per-path cost is bounded


class ProjectError(ValueError):
    pass


def _resolve(path, links, top, st=None, depth=0):
    """`path` (under `top`) with every symlink in `links` ({name: linkname}) followed component by
    component, or None when it leaves the top directory, cycles, nests more than 40 links deep or is
    absolute (Lookout F4: a lexical normpath accepts `a -> .` plus `b -> a/..`, which the filesystem
    resolves above the top). `st` is shared across one validation: every link is resolved once (`memo`),
    a link met again while it is being resolved is a cycle (`active`), and the components visited are
    capped at RESOLVE_BUDGET (Lookout R2-F1: without the memo, 31 links whose targets share subproblems
    cost 2^31 calls under the depth guard). A resolved path longer than MAX_NAME is refused as it grows
    (Lookout R4-F1: short targets such as s_i -> s_{i-1}/X make each destination 81 characters longer than
    the last, so without this cap the memo, not the step count, is what a small archive can inflate; the
    guest could not hold such a path anyway). Every returned path and every memo value is therefore at
    most MAX_NAME characters; a temporary join or split before a refusal is at most 2 * MAX_NAME + 1 (a
    cached path plus one target), so the work is O(RESOLVE_BUDGET * MAX_NAME) and the memo holds at most
    one path of MAX_NAME per link (Lookout R5-F1)."""
    st = {"memo": {}, "active": set(), "steps": 0} if st is None else st
    if depth > 40:
        return None
    out = []
    for comp in path.split("/"):
        st["steps"] += 1
        if st["steps"] > RESOLVE_BUDGET:
            raise ProjectError("symlink resolution exceeded %d steps" % RESOLVE_BUDGET)
        if comp in ("", "."):
            continue
        if comp == "..":
            if len(out) <= 1:
                return None
            out.pop()
            continue
        out.append(comp)
        cur = "/".join(out)
        if len(cur) > MAX_NAME:
            return None
        if cur in links:
            if cur in st["active"]:
                return None
            if cur in st["memo"]:
                r = st["memo"][cur]
            else:
                target, base = links[cur], "/".join(out[:-1])
                st["active"].add(cur)
                r = None if target.startswith("/") else _resolve(base + "/" + target if base else target, links, top, st, depth + 1)
                st["active"].discard(cur)
                st["memo"][cur] = r
            if r is None:
                return None
            out = r.split("/")
    if not out or out[0] != top:
        return None
    return "/".join(out)


def _plain(name):
    return not BAD_CHARS.search(name)


def _ident(path, follow=True):
    """(device, inode) of `path`, or None when it does not exist. Identity, not path text, is what a
    case-insensitive volume shares between `App` and `app` and what a mount or firmlink shares with the
    directory it exposes."""
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def _inside(out, src):
    """True when `out` is, or will be created in, `src` or any directory under it (Lookout R6-F1, R7-F1,
    R8-F1, R8-F2). The identities of every directory of the tree are collected first (SKIP directories
    included: they are inside the tree even though pack omits them). The output entry itself is compared
    without dereferencing (so pack(src, src) and a case alias of src are refused, while a symlink to src that
    lives elsewhere is judged by where it lives); then its parent, resolved through symlinks, and each
    ancestor are compared, so an alias that realpath does not fold still matches by the identity of the
    directory it exposes. Ancestors that do not exist yet are skipped."""
    ids = set()
    for root, _dirs, _files in os.walk(src, followlinks=False):
        ids.add(_ident(root))
    if _ident(out, follow=False) in ids:
        return True
    cur = os.path.realpath(pathlib.Path(out).parent)
    while True:
        if _ident(cur) in ids:
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def _cleanup(exc, label, fn, *args):
    """Run the cleanup `fn(*args)` while `exc` is being handled. A failure of the cleanup is attached to
    `exc` as a note and does not replace it, so the caller sees the pack or setup error and reads the
    secondary one in its traceback (Lookout R12-F1)."""
    try:
        fn(*args)
    except Exception as e:
        exc.add_note("%s failed during cleanup: %r" % (label, e))


def _owned(fd, mode):
    """A file object owning raw descriptor `fd`; the descriptor is closed if the wrapping itself fails, so no
    path leaves a raw descriptor without an owner (Lookout R11-F1)."""
    try:
        return os.fdopen(fd, mode)
    except BaseException as exc:
        _cleanup(exc, "close of descriptor %d" % fd, os.close, fd)
        raise


def _discard(tmp, created, mine):
    """Remove the `.part` when this call created it and the name still points at the file it created
    (R10-F1); `mine` None means created, then fstat failed, so the entry is pack's and its identity is
    unknown (R11-F1)."""
    if created and (mine is None or _ident(tmp, follow=False) == mine):
        tmp.unlink(missing_ok=True)


def pack(src, out):
    """Write `out` from directory `src` (top directory = src's name). Excludes .git and .DS_Store, keeps
    node_modules. Refuses a tree holding the token prefix, a symlink leaving the tree or a symlink with an
    absolute target (validate refuses those, even when they point inside the tree; Lookout R2-F4), and
    validates the archive before publishing it, so pack never writes one the loader refuses (Lookout R3-F2).
    A hard link is stored as a plain copy, never as a hard-link member. `out` must lie outside `src`: the
    members are added by hand, without TarFile.add's skip of the archive being written, and an earlier
    archive inside the tree would be snapshotted as content (Lookout R6-F1); the test is by directory
    identity, not path text, so a case alias, a symlinked parent or an unfolded mount alias counts
    (R7-F1, R8-F1), and the walk itself refuses a file that is the partial archive or the existing output
    (a backstop for an alias no identity check saw). The partial archive is created exclusively, without
    following links, so a stale or planted `.part` entry is refused rather than written through (R9-F1);
    validation reads that same descriptor, and publication and cleanup first check the name still points
    at it (R10-F1; the output directory is single-writer by contract). A failure of the cleanup itself (a
    close or the unlink) is noted on the error being raised, never raised in its place (R12-F1). Returns
    (sha256 of the archive, number of regular files)."""
    src, out = pathlib.Path(src).resolve(), pathlib.Path(out)
    if not src.is_dir() or not NAME.fullmatch(src.name):
        raise ProjectError("source must be a directory with a plain name: %s" % src)
    top, count = src.name, 0
    real_root = os.path.realpath(src)
    if _inside(out, src):
        raise ProjectError("output %s lies inside the source tree %s; write it elsewhere" % (out, src))
    tmp = out.with_name(out.name + ".part")
    created, mine = False, None  # set before anything fallible: cleanup reads both (R11-F1)
    try:
        # Exclusive creation without following links: a `.part` entry left there by anyone else (a symlink or
        # hard link to a source file, say) is refused, never opened for writing (Lookout R9-F1: tarfile's own
        # open would follow it and truncate the target). Cleanup removes only what this call created.
        try:
            fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        except OSError as e:
            raise ProjectError("temporary file %s already exists or cannot be created (%s); remove it first"
                               % (tmp, e.strerror)) from None
        created = True
        try:  # until a file object owns fd, any failure here closes it (R11-F1: fstat or dup at the descriptor limit)
            st = os.fstat(fd)
            mine = (st.st_dev, st.st_ino)
            rf = _owned(os.dup(fd), "rb")  # a second handle on that inode: validation reads it once the archive is closed
        except BaseException as exc:
            _cleanup(exc, "close of descriptor %d" % fd, os.close, fd)
            raise
        with rf:
            with _owned(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as t:
                own = {mine, _ident(out, follow=False)} - {None}
                for root, dirs, files in os.walk(src, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d not in SKIP)
                    rel = pathlib.Path(root).relative_to(src)
                    for d in dirs:
                        p = pathlib.Path(root) / d
                        if p.is_symlink():
                            files.append(d)
                    for f in sorted(files):
                        if f in SKIP:
                            continue
                        p = pathlib.Path(root) / f
                        name = posixpath.join(top, *(rel / f).parts)
                        if not _plain(name):
                            raise ProjectError("file name %r has a backslash or control character" % name)
                        if p.is_symlink():
                            link = os.readlink(p)
                            if link.startswith("/"):
                                raise ProjectError("symlink %s has an absolute target (%s); make it relative" % (name, link))
                            real = os.path.realpath(p)
                            if real != real_root and not real.startswith(real_root + "/"):
                                raise ProjectError("symlink %s resolves outside the tree (%s)" % (name, real))
                            t.add(p, arcname=name, recursive=False)
                            continue
                        if TOKEN in p.read_bytes():
                            raise ProjectError("%s contains %s; snapshots must not carry a token" % (name, TOKEN.decode()))
                        if _ident(p) in own:  # the partial archive or the previous output, reached through an alias (R8-F1)
                            raise ProjectError("output %s lies inside the source tree %s (reached as %s); write it elsewhere"
                                               % (out, src, name))
                        ti = t.gettarinfo(p, arcname=name)
                        if ti.islnk():  # a second hard link to an inode already added: store a plain copy (npm's esbuild does this)
                            ti.type, ti.linkname, ti.size = tarfile.REGTYPE, "", p.stat().st_size
                        with open(p, "rb") as fh:
                            t.addfile(ti, fh)
                        count += 1
            rf.seek(0)  # a dup shares the write offset
            validate(tmp, fileobj=rf)  # the inode this call wrote, whatever the name points at now
        # Publication and cleanup act on the name, so they first check it still names the inode this call
        # created; a name swapped by another writer in the output directory is neither published nor removed
        # (Lookout R10-F1). The check-to-action window is not closed: the output directory is single-writer
        # by contract (docs/config.md).
        if _ident(tmp, follow=False) != mine:
            raise ProjectError("temporary file %s was replaced while packing; nothing published" % tmp)
        tmp.replace(out)
    except BaseException as exc:
        _cleanup(exc, "removal of %s" % tmp, _discard, tmp, created, mine)
        raise
    return hashlib.sha256(out.read_bytes()).hexdigest(), count


def validate(path, fileobj=None):
    """The archive's single top-level directory name, or ProjectError. Symlinks are followed through each
    other and must stay under the top; nothing may lie under a symlink member (Lookout R2-F2: the resolver
    would follow the ancestor and never look at such a member's own target; pack never emits one). Names and
    targets are capped at MAX_NAME before anything else looks at them (Lookout R3-F1: a tiny archive can
    carry a name of a million parts), and the ancestor check is a bisect over the sorted names per link, not
    a walk over every prefix of every name."""
    try:
        t = tarfile.open(path, "r:gz", fileobj=fileobj)  # fileobj: pack validates its own descriptor (R10-F1)
    except (tarfile.TarError, OSError) as e:
        raise ProjectError("not a readable .tgz: %s" % e)
    tops, links, names = set(), {}, []
    with t:
        for m in t:
            if len(m.name) > MAX_NAME or (m.issym() and len(m.linkname) > MAX_NAME):
                raise ProjectError("member name or link target longer than %d: %r" % (MAX_NAME, m.name[:80]))
            names.append(m.name)
            parts = m.name.split("/")
            if m.name.startswith("/") or any(x in ("", ".", "..") for x in parts) or not _plain(m.name):
                raise ProjectError("bad member name %r" % m.name)
            if not NAME.fullmatch(parts[0]):
                raise ProjectError("top-level name must be plain: %r" % parts[0])
            if len(parts) == 1 and not m.isdir():
                raise ProjectError("top level holds a file (%r); the archive needs one directory" % m.name)
            tops.add(parts[0])
            if m.issym():
                links[m.name] = m.linkname
            elif not (m.isfile() or m.isdir()):
                raise ProjectError("member %r is not a file, directory or symlink" % m.name)
    if len(tops) != 1:
        raise ProjectError("archive must hold exactly one top-level directory, has %s" % sorted(tops))
    top = tops.pop()
    names.sort()
    for link in links:
        i = bisect.bisect_right(names, link + "/")
        if i < len(names) and names[i].startswith(link + "/"):
            raise ProjectError("member %r lies under symlink %r" % (names[i], link))
    st = {"memo": {}, "active": set(), "steps": 0}
    for name, target in links.items():
        if _resolve(name, links, top, st) is None:
            raise ProjectError("symlink %s -> %s leaves the top directory, cycles, is absolute or resolves to a path longer than %d"
                               % (name, target, MAX_NAME))
    return top


def parse_manifest(text):
    """{path: sha256}; a line that is not a plain shasum record (an escaped name) is an error, not a gap."""
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        m = re.fullmatch(r"([0-9a-f]{64})  \./(.+)", line)
        if not m or not _plain(m.group(2)):
            raise ProjectError("unparsed manifest line %r" % line[:120])
        out[m.group(2)] = m.group(1)
    return out


def classify(before, after):
    return {"added": sorted(set(after) - set(before)),
            "modified": sorted(p for p in before if p in after and after[p] != before[p]),
            "deleted": sorted(set(before) - set(after))}


def home_prunes(cfg):
    """The find expression pruning what the home capture must not list: fixed harness, Claude Code and Photos
    paths, the configured results directory (results.tgz), ~/exp/project only when a project snapshot covers
    it (without one it is an ordinary agent directory), and every node_modules. Each path is shell-quoted
    (the Photos library has a space in its name)."""
    paths = list(HOME_PRUNES) + ["." + cfg["results"][1:]]  # "~/exp/results" -> "./exp/results" (config.load checked it)
    if "project" in cfg:
        paths.append("." + GUEST_DIR[1:])
    return "\\( " + " -o ".join("-path %s" % shlex.quote(q) for q in paths) + " -o -name node_modules \\)"


def parse_listing(text):
    """({path: (mtime, ctime, size)}, bad) from HOME_LIST_CMD output. A record whose name has a BAD_CHARS
    character or that does not parse is counted, never kept; the others are kept (plan v2.1)."""
    out, bad = {}, 0
    for rec in text.split("\0"):
        if not rec:
            continue
        rec = rec.removesuffix("\n")  # exactly one: a name ending in a newline keeps it and is refused
        m = re.fullmatch(r"(\d+) (\d+) (\d+) \./(.+)", rec, re.S)
        if not m or not _plain(m.group(4)):
            bad += 1
            continue
        out[m.group(4)] = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return out, bad


def cap_changes(after, changed, limit=None):
    """({files, bytes, capped}, archived): the whole list's count and size, and the sorted prefix that fits
    under the cap (the first file that does not fit ends the prefix)."""
    limit = HOME_CAP_BYTES if limit is None else limit
    total = sum(after[q][2] for q in changed)
    keep, used = [], 0
    for q in sorted(changed):
        if used + after[q][2] > limit:
            break
        keep.append(q)
        used += after[q][2]
    return {"files": len(changed), "bytes": total, "capped": total > limit}, keep


def list_input(paths):
    """tar -T input: every entry ./-prefixed, so a name such as -C is a member, not a directive (observed)."""
    return "".join("./%s\n" % q for q in paths)


def listen_judge(out, rc, port):
    """D1: the dev server listens on guest loopback only (lsof -nP -iTCP:<port> -sTCP:LISTEN output)."""
    listen = [l.split()[-2] for l in out.splitlines() if l.endswith("(LISTEN)") and len(l.split()) >= 2]
    ok_addr = {"127.0.0.1:%d" % port, "[::1]:%d" % port}
    bad = [a for a in listen if a not in ok_addr]
    ok = rc == 0 and bool(listen) and not bad
    why = "loopback only" if ok else ("no listener" if not listen else "not loopback: %s" % bad)
    return {"exit": rc, "out": out, "pass": ok, "why": why, "listen": listen}


def loopback_judge(text):
    """D2: no refused loopback request in the session window of the egress log."""
    n = 0
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("ev") == "refused" and e.get("host") in LOOPBACK:
            n += 1
    return {"exit": None, "out": "%d loopback refusal(s) in the session window" % n, "pass": n == 0,
            "why": "the browser's loopback bypass must keep dev-server traffic off the proxy", "refused": n}

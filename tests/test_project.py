"""Project snapshot helpers (the web-dev design): pack, archive validation, change detection, D1 and D2 judges."""
import io
import json
import os
import pathlib
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from cuvm import project


def tree(root, files, links=()):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
    for rel, target in links:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p)


def archive(path, members):
    """members: (name, kind, payload) with kind f (payload bytes), d, or l (payload = link target)."""
    with tarfile.open(path, "w:gz") as t:
        for name, kind, payload in members:
            ti = tarfile.TarInfo(name)
            if kind == "f":
                ti.size = len(payload)
                t.addfile(ti, io.BytesIO(payload))
            elif kind == "d":
                ti.type = tarfile.DIRTYPE
                t.addfile(ti)
            else:
                ti.type = tarfile.SYMTYPE
                ti.linkname = payload
                t.addfile(ti)


class PackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.src = self.tmp / "app"
        tree(self.src, {"package.json": "{}", "src/main.js": "x", ".git/HEAD": "ref", ".DS_Store": "", "node_modules/vite/bin/vite.js": "#!"},
             links=[("node_modules/.bin/vite", "../vite/bin/vite.js")])

    def test_pack_excludes_git_and_ds_store_and_keeps_node_modules(self):
        out = self.tmp / "project.tgz"
        sha, n = project.pack(self.src, out)
        self.assertEqual(len(sha), 64)
        with tarfile.open(out) as t:
            names = sorted(t.getnames())
        self.assertIn("app/package.json", names)
        self.assertIn("app/node_modules/.bin/vite", names)
        self.assertNotIn("app/.git/HEAD", names)
        self.assertNotIn("app/.DS_Store", names)
        self.assertEqual(n, 3)  # package.json, main.js, vite.js
        self.assertEqual(project.validate(out), "app")

    def test_pack_refuses_token_string(self):
        (self.src / "src/leak.js").write_text("const k = 'sk-ant-oat01-x'")
        with self.assertRaisesRegex(project.ProjectError, "sk-ant-"):
            project.pack(self.src, self.tmp / "p.tgz")
        self.assertFalse((self.tmp / "p.tgz").exists())

    def test_pack_refuses_escaping_symlink(self):
        os.symlink("../../outside", self.src / "esc")
        with self.assertRaisesRegex(project.ProjectError, "outside"):
            project.pack(self.src, self.tmp / "p.tgz")

    def test_pack_refuses_chained_links_that_resolve_above_the_tree(self):
        os.symlink(".", self.src / "a")
        os.symlink("a/..", self.src / "b")
        with self.assertRaisesRegex(project.ProjectError, "outside"):
            project.pack(self.src, self.tmp / "p.tgz")

    def test_pack_refuses_absolute_link_even_inside_the_tree(self):
        """validate refuses absolute targets, so pack must not produce one (Lookout R2-F4)."""
        os.symlink(str(self.src / "package.json"), self.src / "abs")
        with self.assertRaisesRegex(project.ProjectError, "absolute target"):  # pack's own check, before validate
            project.pack(self.src, self.tmp / "p.tgz")
        self.assertFalse((self.tmp / "p.tgz").exists())

    def test_pack_refuses_relative_link_that_leaves_and_reenters_the_tree(self):
        """realpath lands inside the tree, the loader's component walk does not; pack must agree with the
        loader, so it validates its own output before publishing it (Lookout R3-F2)."""
        os.symlink("../app/package.json", self.src / "l")
        with self.assertRaisesRegex(project.ProjectError, "leaves the top"):
            project.pack(self.src, self.tmp / "p.tgz")
        self.assertFalse((self.tmp / "p.tgz").exists())
        self.assertFalse((self.tmp / "p.tgz.part").exists())

    def test_pack_stores_a_hard_link_as_a_regular_file(self):
        """npm's esbuild installer hard-links its native binary into two packages; tarfile would store the
        second as a hard-link member, which validate refuses (live step 2, 2026-11-07)."""
        os.link(self.src / "package.json", self.src / "hard.json")
        sha, n = project.pack(self.src, self.tmp / "p.tgz")
        self.assertEqual(n, 4)
        with tarfile.open(self.tmp / "p.tgz") as t:
            kinds = {m.name: (m.isfile(), m.islnk(), m.size) for m in t if m.name.endswith(".json")}
        self.assertEqual(kinds, {"app/package.json": (True, False, 2), "app/hard.json": (True, False, 2)})
        self.assertEqual(project.validate(self.tmp / "p.tgz"), "app")

    def test_pack_refuses_an_output_inside_the_source_tree(self):
        """The manual addfile path has no self-archive skip, so OUT.tgz.part under DIR would be walked into
        the snapshot, and any earlier archive there would be snapshotted as content (Lookout R6-F1)."""
        for out in (self.src / "snap.tgz", self.src / "dist" / "snap.tgz"):
            with self.assertRaisesRegex(project.ProjectError, "inside the source tree"):
                project.pack(self.src, out)
            self.assertFalse(out.exists())
            self.assertFalse(out.with_name(out.name + ".part").exists())
        self.assertEqual(sorted(q.name for q in self.src.iterdir() if q.name.endswith((".tgz", ".part"))), [])

    def test_pack_refuses_an_output_under_a_case_alias_of_the_source(self):
        """On a case-insensitive volume `APP/` is the same directory as `app/` while the path strings differ;
        the check is by inode, not text (Lookout R7-F1). Skipped where the volume does not alias."""
        alias = self.tmp / "APP"
        if not alias.is_dir() or os.stat(alias).st_ino != os.stat(self.src).st_ino:
            self.skipTest("test volume is case-sensitive")
        for out in (alias / "snap.tgz", alias / "dist" / "snap.tgz"):
            with self.assertRaisesRegex(project.ProjectError, "inside the source tree"):
                project.pack(self.src, out)
        self.assertEqual(sorted(q.name for q in self.src.iterdir() if q.name.endswith((".tgz", ".part"))), [])

    def test_pack_refuses_an_output_under_a_symlinked_parent_in_the_source(self):
        """A parent directory reached through a symlink is resolved before the identity walk (Lookout R7-F1)."""
        os.symlink(str(self.src), self.tmp / "alias")
        (self.src / "dist").mkdir()
        os.symlink("app/dist", self.tmp / "deep")
        for out in (self.tmp / "alias" / "snap.tgz", self.tmp / "deep" / "snap.tgz"):
            with self.assertRaisesRegex(project.ProjectError, "inside the source tree"):
                project.pack(self.src, out)
        self.assertEqual(sorted(q.name for q in (self.src / "dist").iterdir()), [])

    def test_pack_refuses_an_output_equal_to_the_source_before_creating_anything(self):
        """pack(src, src), and src under a case alias where the volume has one, are refused before the
        archive is opened, with the documented error, not a filesystem error at replace time (Lookout R8-F2)."""
        outs = [self.src, self.src / "dist"]
        (self.src / "dist").mkdir()
        alias = self.tmp / "APP"
        if alias.is_dir() and os.stat(alias).st_ino == os.stat(self.src).st_ino:
            outs.append(alias)
        with mock.patch("cuvm.project.tarfile.open", side_effect=AssertionError("archive opened before the refusal")):
            for out in outs:
                with self.assertRaisesRegex(project.ProjectError, "inside the source tree"):
                    project.pack(self.src, out)
        self.assertEqual(sorted(q.name for q in self.tmp.iterdir() if q.name.endswith(".part")), [])

    def test_pack_refuses_an_alias_of_a_subdirectory_that_realpath_does_not_fold(self):
        """A mount or firmlink exposing app/dist under another path is not folded by realpath; its identity is
        that of dist, not of app, so the check must know every directory of the tree (Lookout R8-F1). The alias
        is a symlink with realpath patched to abspath, which is exactly an unfolded alias."""
        (self.src / "dist").mkdir()
        os.symlink(str(self.src / "dist"), self.tmp / "alias")
        with mock.patch("cuvm.project.os.path.realpath", side_effect=lambda q, **kw: os.path.abspath(q)), \
             mock.patch("cuvm.project.tarfile.open", side_effect=AssertionError("archive opened before the refusal")):
            with self.assertRaisesRegex(project.ProjectError, "inside the source tree"):
                project.pack(self.src, self.tmp / "alias" / "snap.tgz")
        self.assertEqual(sorted(q.name for q in (self.src / "dist").iterdir()), [])

    def test_pack_refuses_when_the_walk_meets_its_own_partial_archive(self):
        """Backstop for an alias no identity check saw: the partial archive appears inside the tree under
        another name (here a hard link made right after the archive is opened). The walk must refuse and
        publish nothing; the `.part` name is removed, while ghost.bin, the test's own second name for that
        inode, necessarily keeps the partial bytes (Lookout R8-F1, R9-F1 wording)."""
        real = tarfile.open

        def opened(name=None, mode="r", fileobj=None, **kw):
            t = real(name, mode, fileobj=fileobj, **kw)
            os.link(self.tmp / "p.tgz.part", self.src / "ghost.bin")  # pack opened it exclusively just now
            return t

        with mock.patch("cuvm.project.tarfile.open", side_effect=opened):
            with self.assertRaisesRegex(project.ProjectError, "inside the source tree.*ghost.bin"):
                project.pack(self.src, self.tmp / "p.tgz")
        self.assertFalse((self.tmp / "p.tgz").exists())
        self.assertFalse((self.tmp / "p.tgz.part").exists())
        self.assertTrue((self.src / "ghost.bin").exists())

    def test_pack_refuses_a_preexisting_part_entry_without_touching_its_target(self):
        """A `.part` symlink or hard link to a source file left at the temporary path must not be opened for
        writing: tarfile's own open would follow it and truncate the source file before any check. The
        entry is refused by exclusive creation, the source bytes and the entry itself are unchanged, and
        cleanup does not remove what pack did not create (Lookout R9-F1)."""
        victim = self.src / "package.json"
        for kind in ("symlink", "hardlink"):
            out = self.tmp / ("%s.tgz" % kind)
            part = out.with_name(out.name + ".part")
            if kind == "symlink":
                os.symlink(str(victim), part)
            else:
                os.link(victim, part)
            before = os.lstat(part)
            with self.assertRaisesRegex(project.ProjectError, "already exists"):
                project.pack(self.src, out)
            self.assertEqual(victim.read_bytes(), b"{}", kind)
            self.assertTrue(part.is_symlink() if kind == "symlink" else part.exists(), kind)
            self.assertEqual((os.lstat(part).st_ino, os.lstat(part).st_size), (before.st_ino, before.st_size), kind)
            self.assertFalse(out.exists(), kind)
        # a stale regular .part is refused the same way, and left alone
        stale = self.tmp / "stale.tgz.part"
        stale.write_bytes(b"old")
        with self.assertRaisesRegex(project.ProjectError, "already exists"):
            project.pack(self.src, self.tmp / "stale.tgz")
        self.assertEqual(stale.read_bytes(), b"old")

    def test_pack_neither_publishes_nor_removes_a_part_name_swapped_after_creation(self):
        """Another writer in the output directory renames the just-created `.part` away and puts its own file at
        that name. pack keeps writing its own inode, validates that inode, and then refuses to publish the
        swapped name; on a failure it does not unlink the swapped name either (Lookout R10-F1)."""
        real = tarfile.open
        out = self.tmp / "p.tgz"
        part, moved = out.with_name(out.name + ".part"), self.tmp / "moved"

        def opened(name=None, mode="r", fileobj=None, **kw):
            t = real(name, mode, fileobj=fileobj, **kw)
            if mode.startswith("w"):
                os.rename(part, moved)
                part.write_bytes(b"theirs")
            return t

        with mock.patch("cuvm.project.tarfile.open", side_effect=opened):
            with self.assertRaisesRegex(project.ProjectError, "replaced while packing"):
                project.pack(self.src, out)
        self.assertEqual(part.read_bytes(), b"theirs")
        self.assertFalse(out.exists())
        self.assertTrue(moved.exists())  # pack's own inode, under the other writer's name; not pack's to remove
        moved.unlink()
        part.unlink()  # the other writer's file; pack rightly left it, so clear it for the second scenario
        (self.src / "bad.txt").write_text("sk-ant-x")  # now a failure inside the walk, after the swap
        with mock.patch("cuvm.project.tarfile.open", side_effect=opened):
            with self.assertRaisesRegex(project.ProjectError, "contains sk-ant-"):
                project.pack(self.src, out)
        self.assertEqual(part.read_bytes(), b"theirs")
        self.assertFalse(out.exists())

    def test_pack_setup_failures_release_the_descriptor_and_the_part_file(self):
        """A failure between the exclusive create and file-object ownership (fstat, dup at the descriptor limit,
        or the fdopen of the dup) must close the raw descriptor, remove the `.part` pack created, and raise the
        original error rather than an UnboundLocalError from cleanup (Lookout R11-F1)."""
        out = self.tmp / "p.tgz"
        part = out.with_name(out.name + ".part")
        real_fdopen = os.fdopen

        def fdopen_dup_fails(fd, mode="r", *a, **kw):
            if mode == "rb":
                raise OSError(24, "Too many open files")
            return real_fdopen(fd, mode, *a, **kw)

        for label, target, effect in (
            ("fstat", "cuvm.project.os.fstat", OSError(5, "Input/output error")),
            ("dup", "cuvm.project.os.dup", OSError(24, "Too many open files")),
            ("fdopen of the dup", "cuvm.project.os.fdopen", fdopen_dup_fails),
        ):
            before = sorted(os.listdir("/dev/fd"))
            with mock.patch(target, side_effect=effect):
                with self.assertRaises(OSError, msg=label) as cm:
                    project.pack(self.src, out)
            self.assertNotIsInstance(cm.exception, project.ProjectError, label)
            self.assertEqual(sorted(os.listdir("/dev/fd")), before, label)  # no descriptor left behind
            self.assertFalse(part.exists(), label)
            self.assertFalse(out.exists(), label)

    def test_pack_cleanup_failures_do_not_replace_the_original_error(self):
        """A failure inside cleanup while pack is already failing (the close of a raw descriptor in `_owned` or
        the setup guard, or the unlink of the `.part`) must not become the raised error: the original error
        propagates, with the cleanup failure attached to it as a note (Lookout R12-F1)."""
        out = self.tmp / "p.tgz"
        part = out.with_name(out.name + ".part")
        real_close, real_fdopen = os.close, os.fdopen

        def close_then_fail(fd):
            real_close(fd)  # the descriptor is released, as a real close reporting EIO does
            raise OSError(5, "Input/output error")

        def fdopen_dup_fails(fd, mode="r", *a, **kw):
            if mode == "rb":
                raise OSError(24, "Too many open files")
            return real_fdopen(fd, mode, *a, **kw)

        for label, target, effect, errno, closes in (
            ("fstat then close", "cuvm.project.os.fstat", OSError(28, "No space left on device"), 28, 1),
            ("fdopen of the dup then both closes", "cuvm.project.os.fdopen", fdopen_dup_fails, 24, 2),
        ):
            before = sorted(os.listdir("/dev/fd"))
            with mock.patch(target, side_effect=effect), mock.patch("cuvm.project.os.close", side_effect=close_then_fail):
                with self.assertRaises(OSError, msg=label) as cm:
                    project.pack(self.src, out)
            self.assertEqual(cm.exception.errno, errno, label)  # the original error, not the close failure
            notes = getattr(cm.exception, "__notes__", [])
            self.assertEqual(sum("Input/output error" in n for n in notes), closes, (label, notes))
            self.assertEqual(sorted(os.listdir("/dev/fd")), before, label)
            self.assertFalse(part.exists(), label)
            self.assertFalse(out.exists(), label)
        bad = self.src / "bad.txt"
        bad.write_text("sk-ant-x")  # a pack failure inside the walk, then the unlink of the .part fails
        with mock.patch("cuvm.project.pathlib.Path.unlink", side_effect=OSError(13, "Permission denied")):
            with self.assertRaisesRegex(project.ProjectError, "contains sk-ant-") as cm:
                project.pack(self.src, out)
        notes = getattr(cm.exception, "__notes__", [])
        self.assertTrue(any("Permission denied" in n and part.name in n for n in notes), notes)
        self.assertTrue(part.exists())  # the unlink did fail; pack reported it rather than hiding it
        self.assertFalse(out.exists())
        part.unlink()
        bad.unlink()

    def test_pack_accepts_a_sibling_whose_name_extends_the_source_name(self):
        """app2 next to app is outside app (a prefix test on the path text would get this wrong too)."""
        (self.tmp / "app2").mkdir()
        sha, n = project.pack(self.src, self.tmp / "app2" / "p.tgz")
        self.assertEqual(n, 3)
        self.assertTrue((self.tmp / "app2" / "p.tgz").exists())

    def test_pack_refuses_backslash_and_newline_names(self):
        for bad in ("logo\\v2.svg", "two\nlines"):
            q = self.src / bad
            q.write_text("x")
            with self.assertRaisesRegex(project.ProjectError, "backslash or control"):
                project.pack(self.src, self.tmp / "p.tgz")
            q.unlink()


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def check(self, members):
        p = self.tmp / "a.tgz"
        archive(p, members)
        return project.validate(p)

    def test_self_and_chained_internal_links_accepted(self):
        top = self.check([("app/x", "f", b""), ("app/a", "l", "."), ("app/b", "l", "a/x"), ("app/c", "l", "b")])
        self.assertEqual(top, "app")

    def test_good_archive_with_internal_dotdot_symlink(self):
        top = self.check([("app", "d", None), ("app/package.json", "f", b"{}"), ("app/node_modules/vite/bin/vite.js", "f", b"#"),
                          ("app/node_modules/.bin/vite", "l", "../vite/bin/vite.js")])
        self.assertEqual(top, "app")

    def fan_out(self, n):
        """n+1 acyclic links that all resolve to the top: s0 -> ., s_i -> s_{i-1}/s_{i-1} (Lookout R2-F1)."""
        members = [("app/x", "f", b""), ("app/s0", "l", ".")]
        members += [("app/s%d" % i, "l", "s%d/s%d" % (i - 1, i - 1)) for i in range(1, n + 1)]
        return members

    def test_fan_out_links_resolve_in_bounded_work(self):
        """31 links whose targets share subproblems: each is resolved once, not 2^31 times (Lookout R2-F1)."""
        self.assertEqual(self.check(self.fan_out(30)), "app")

    def test_resolver_budget_refuses_rather_than_runs_away(self):
        with mock.patch.object(project, "RESOLVE_BUDGET", 10):
            with self.assertRaisesRegex(project.ProjectError, "steps"):
                self.check(self.fan_out(30))

    def test_long_names_and_targets_are_refused_before_any_scan(self):
        """Lookout R3-F1: a 2 KB archive can carry a member name of a million parts; the per-name limit is
        checked in the first member loop, so no prefix or resolver work depends on the name's length."""
        deep = "app/" + "a/" * 200000 + "x"
        with self.assertRaisesRegex(project.ProjectError, "longer than"):
            self.check([(deep, "f", b""), ("app/z", "l", ".")])
        with self.assertRaisesRegex(project.ProjectError, "longer than"):
            self.check([("app/x", "f", b""), ("app/l", "l", "../" * 400 + "x")])
        near = "app/" + "a/" * 400 + "x"
        self.assertEqual(len(near), 805)
        self.assertEqual(self.check([(near, "f", b""), ("app/z", "l", ".")]), "app")

    def growing_chain(self, n):
        """s00000 -> ., s_i -> s_{i-1}/X with X 80 characters: short targets whose resolved destinations grow by
        81 characters per link (Lookout R4-F1)."""
        x = "x" * 80
        members = [("app/x", "f", b""), ("app/s00000", "l", ".")]
        members += [("app/s%05d" % i, "l", "s%05d/%s" % (i - 1, x)) for i in range(1, n + 1)]
        return members

    def test_growing_destinations_are_capped_at_the_path_limit(self):
        self.assertEqual(self.check(self.growing_chain(10)), "app")  # the last destination is 813 characters
        with self.assertRaisesRegex(project.ProjectError, "longer than"):
            self.check(self.growing_chain(20))  # 1,623 characters: longer than any path the guest can hold
        links = {name: target for name, kind, target in self.growing_chain(200) if kind == "l"}
        st = {"memo": {}, "active": set(), "steps": 0}
        for name in links:
            project._resolve(name, links, "app", st)
        self.assertLessEqual(max(len(v) for v in st["memo"].values() if v), project.MAX_NAME)

    def test_rejects(self):
        cases = {
            "two tops": [("a/x", "f", b""), ("b/x", "f", b"")],
            "file at top": [("x", "f", b"")],
            "absolute": [("/app/x", "f", b"")],
            "dotdot name": [("app/../x", "f", b"")],
            "escaping link": [("app/x", "f", b""), ("app/esc", "l", "../../outside")],
            "absolute link": [("app/x", "f", b""), ("app/esc", "l", "/etc/passwd")],
            "odd top name": [("app dir/x", "f", b"")],
            "dot part": [("app/./x", "f", b"")],
            "chained links above top": [("app/x", "f", b""), ("app/a", "l", "."), ("app/b", "l", "a/..")],
            "link cycle": [("app/x", "f", b""), ("app/a", "l", "b"), ("app/b", "l", "a")],
            "self cycle": [("app/x", "f", b""), ("app/a", "l", "a")],
            "link under a symlink ancestor": [("app/real", "d", None), ("app/alias", "l", "real"), ("app/alias/escape", "l", "/outside")],
            "relative link under a symlink ancestor": [("app/real", "d", None), ("app/alias", "l", "real"), ("app/alias/esc", "l", "../../../outside")],
            "file under a symlink ancestor": [("app/real", "d", None), ("app/alias", "l", "real"), ("app/alias/file", "f", b"")],
            "link under a symlink ancestor listed first": [("app/alias/escape", "l", "../../../outside"), ("app/real", "d", None), ("app/alias", "l", "real")],
            "backslash name": [("app/logo\\v2.svg", "f", b"")],
            "newline name": [("app/two\nlines", "f", b"")],
            "trailing slash link": [("app/x", "f", b""), ("app/sub/", "l", "../x")],
            "empty part": [("app//x", "f", b"")],
            "empty": [],
        }
        for why, members in cases.items():
            with self.assertRaises(project.ProjectError, msg=why):
                self.check(members)

    def test_rejects_device_member(self):
        p = self.tmp / "d.tgz"
        with tarfile.open(p, "w:gz") as t:
            ti = tarfile.TarInfo("app/dev")
            ti.type = tarfile.CHRTYPE
            t.addfile(ti)
        with self.assertRaises(project.ProjectError):
            project.validate(p)


class ChangeTest(unittest.TestCase):
    def test_manifest_and_classify(self):
        before = project.parse_manifest("a" * 64 + "  ./src/main.js\n" + "b" * 64 + "  ./index.html\n" + "c" * 64 + "  ./gone.txt\n")
        self.assertEqual(before["src/main.js"], "a" * 64)
        after = project.parse_manifest("d" * 64 + "  ./src/main.js\n" + "b" * 64 + "  ./index.html\n" + "e" * 64 + "  ./new.css\n")
        self.assertEqual(project.classify(before, after),
                         {"added": ["new.css"], "modified": ["src/main.js"], "deleted": ["gone.txt"]})

    def test_unparsed_manifest_line_is_an_error(self):
        for bad in ("shasum: x: No such file\n", "\\" + "a" * 64 + "  ./logo\\\\v2.svg\n"):
            with self.assertRaises(project.ProjectError, msg=bad):
                project.parse_manifest(bad)
        self.assertEqual(project.parse_manifest("\n\n"), {})

    def test_manifest_command_propagates_a_failed_find(self):
        """Host control with the guest's tools (zsh, find, xargs, shasum): an unreadable subtree makes the
        pipeline exit non-zero even though the checksums of the readable files are produced."""
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        d = pathlib.Path(tempfile.mkdtemp())
        tree(d, {"ok/a": "x", "locked/b": "y", "node_modules/m": "z"})
        try:
            r = subprocess.run(["zsh", "-c", project.MANIFEST_CMD % d], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(sorted(project.parse_manifest(r.stdout)), ["locked/b", "ok/a"])
            (d / "locked").chmod(0)
            r = subprocess.run(["zsh", "-c", project.MANIFEST_CMD % d], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ok/a", r.stdout)
        finally:
            (d / "locked").chmod(0o755)

    def test_empty_tree_manifest_is_empty(self):
        d = pathlib.Path(tempfile.mkdtemp())
        r = subprocess.run(["zsh", "-c", project.MANIFEST_CMD % d], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, project.parse_manifest(r.stdout)), (0, {}))

    def test_clean_unpack_excludes_node_modules(self):
        """Host control with bsdtar: the exclude patterns drop the subtree and keep the rest."""
        d = pathlib.Path(tempfile.mkdtemp())
        tree(d / "app", {"index.html": "x", "node_modules/vite/bin/vite.js": "y", "src/main.js": "z"})
        project.pack(d / "app", d / "p.tgz")
        out = d / "clean"; out.mkdir()
        subprocess.run("tar xzf %s -C %s %s" % (d / "p.tgz", out, project.CLEAN_EXCLUDE), shell=True, check=True)
        got = sorted(str(q.relative_to(out)) for q in out.rglob("*") if q.is_file())
        self.assertEqual(got, ["app/index.html", "app/src/main.js"])


class ListenJudgeTest(unittest.TestCase):
    HEAD = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"

    def test_loopback_only_passes(self):
        out = self.HEAD + "node 12 admin 23u IPv4 0x1 0t0 TCP 127.0.0.1:5173 (LISTEN)\nnode 12 admin 24u IPv6 0x2 0t0 TCP [::1]:5173 (LISTEN)\n"
        j = project.listen_judge(out, 0, 5173)
        self.assertTrue(j["pass"], j)
        self.assertEqual(j["listen"], ["127.0.0.1:5173", "[::1]:5173"])

    def test_wildcard_and_vmnet_fail(self):
        for addr in ("*:5173", "[::]:5173", "192.168.128.5:5173"):
            j = project.listen_judge(self.HEAD + "node 1 admin 2u IPv4 0 0t0 TCP %s (LISTEN)\n" % addr, 0, 5173)
            self.assertFalse(j["pass"], addr)

    def test_no_listener_fails(self):
        self.assertFalse(project.listen_judge("", 1, 5173)["pass"])
        self.assertFalse(project.listen_judge(self.HEAD, 0, 5173)["pass"])

    def test_other_port_fails(self):
        self.assertFalse(project.listen_judge(self.HEAD + "node 1 admin 2u IPv4 0 0t0 TCP 127.0.0.1:5174 (LISTEN)\n", 0, 5173)["pass"])


class LoopbackRefusalTest(unittest.TestCase):
    def log(self, *ev):
        return "".join(json.dumps(e) + "\n" for e in ev)

    def test_counts_loopback_refusals_only(self):
        text = self.log({"ev": "refused", "host": "localhost", "why": "not CONNECT"},
                        {"ev": "refused", "host": "127.0.0.1", "port": "5173", "why": "not allowlisted"},
                        {"ev": "refused", "host": "::1", "why": "not allowlisted"},
                        {"ev": "refused", "host": "example.com", "why": "not allowlisted"},
                        {"ev": "tunnel", "host": "localhost"}) + "garbage\n"
        j = project.loopback_judge(text)
        self.assertFalse(j["pass"])
        self.assertEqual(j["refused"], 3)

    def test_clean_window_passes(self):
        j = project.loopback_judge(self.log({"ev": "refused", "host": "example.com", "why": "not allowlisted"}))
        self.assertTrue(j["pass"])
        self.assertEqual(j["refused"], 0)



class HomeCaptureTest(unittest.TestCase):
    """the guest-output design: the home listing (NUL-terminated stat records), per-run prunes, the cap and
    the ./ prefix on tar list entries."""
    def test_listing_parses_nul_records_with_spaces_in_names(self):
        text = "1 2 3 ./a dir/f 1.txt\n\0" + "4 5 6 ./x\n\0"
        self.assertEqual(project.parse_listing(text), ({"a dir/f 1.txt": (1, 2, 3), "x": (4, 5, 6)}, 0))

    def test_listing_counts_bad_names_and_keeps_the_rest(self):
        text = ("1 2 3 ./bad\nname\n\0" + "1 2 3 ./a\\b\n\0" + "1 2 3 ./ends-in-newline\n\n\0" + "garbage\n\0"
                + "7 8 9 ./good\n\0")
        self.assertEqual(project.parse_listing(text), ({"good": (7, 8, 9)}, 4))

    def test_empty_listing(self):
        self.assertEqual(project.parse_listing(""), ({}, 0))

    def test_classify_sees_a_ctime_only_change(self):
        self.assertEqual(project.classify({"f": (1, 1, 1), "g": (1, 1, 1)}, {"f": (1, 2, 1), "g": (1, 1, 1)}),
                         {"added": [], "modified": ["f"], "deleted": []})

    def test_prunes_follow_the_config(self):
        p = project.home_prunes({"results": "~/exp/other"})
        for fixed in ("./Library", "./.claude", "./.claude.json", "./.claude.json.backup", "./.cuvm", "./.npm",
                      "./.cache", "./.Trash", "./.local/state/claude", "./.codex", "./exp/other"):
            self.assertIn("-path %s " % fixed, p + " ")
        self.assertIn("-path './Pictures/Photos Library.photoslibrary' ", p + " ")  # quoted: the name has a space
        self.assertNotIn("-path ./Pictures", p)
        self.assertIn("-name node_modules", p)
        self.assertNotIn("./exp/results", p)
        self.assertNotIn("./exp/project", p)
        self.assertIn("-path ./exp/project ", project.home_prunes({"results": "~/exp/results", "project": "p.tgz"}) + " ")

    def test_listing_command_on_a_tree(self):
        """Host control with the guest's tools (zsh, find, perl): prunes drop whole subtrees (the Photos library
        with its space and Claude Code's state directory included) and .DS_Store, dotfiles and names with
        spaces come through, a name with a newline is refused alone, and a name such as -C is a name."""
        d = pathlib.Path(tempfile.mkdtemp())
        tree(d, {"Library/x": "1", "exp/results/r": "2", "exp/project/p": "3", "node_modules/m": "4", ".DS_Store": "5",
                 "Desktop/a b.txt": "hello", "keep/.hidden": "hi", "-C": "6", "bad\nname": "7", "sub/node_modules/n": "8",
                 "Pictures/Photos Library.photoslibrary/private/c.plist": "9", ".local/state/claude/locks/x.lock": "",
                 "Pictures/keep.jpg": "img"})
        cmd = project.HOME_LIST_CMD % (d, project.home_prunes({"results": "~/exp/results", "project": "p.tgz"}))
        r = subprocess.run(["zsh", "-c", cmd], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        entries, bad = project.parse_listing(r.stdout)
        self.assertEqual(bad, 1)
        self.assertEqual(sorted(entries), ["-C", "Desktop/a b.txt", "Pictures/keep.jpg", "keep/.hidden"])
        self.assertEqual(entries["Desktop/a b.txt"][2], 5)
        st = (d / "keep/.hidden").stat()
        self.assertEqual(entries["keep/.hidden"], (int(st.st_mtime), int(st.st_ctime), 2))

    def test_listing_command_propagates_a_failed_find(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        d = pathlib.Path(tempfile.mkdtemp())
        tree(d, {"ok/a": "x", "locked/b": "y"})
        cmd = project.HOME_LIST_CMD % (d, project.home_prunes({"results": "~/exp/results"}))
        try:
            (d / "locked").chmod(0)
            r = subprocess.run(["zsh", "-c", cmd], capture_output=True, text=True, timeout=30)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("./ok/a", r.stdout)
        finally:
            (d / "locked").chmod(0o755)

    def test_stat_command_skips_a_vanished_name_and_fails_on_others(self):
        """The lister fed names directly: a name that is gone (removed between find and lstat) is skipped, a
        present one is one record with the trailing newline and NUL parse_listing expects, and an lstat failure
        that is not ENOENT fails the listing."""
        d = pathlib.Path(tempfile.mkdtemp())
        tree(d, {"present": "abc", "locked/b": "x"})
        r = subprocess.run(["zsh", "-c", "cd %s && %s" % (d, project.HOME_STAT_CMD)], input="./present\0./missing\0",
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.count("\0"), 1)
        self.assertTrue(r.stdout.endswith("./present\n\0"), repr(r.stdout))
        self.assertEqual(project.parse_listing(r.stdout), ({"present": project.parse_listing(r.stdout)[0]["present"]}, 0))
        self.assertEqual(project.parse_listing(r.stdout)[0]["present"][2], 3)
        if os.geteuid() == 0:
            return  # root reads everything
        try:
            (d / "locked").chmod(0)
            r = subprocess.run(["zsh", "-c", "cd %s && %s" % (d, project.HOME_STAT_CMD)], input="./locked/b\0",
                               capture_output=True, text=True, timeout=30)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("lstat ./locked/b", r.stderr)
        finally:
            (d / "locked").chmod(0o755)

    def test_cap_keeps_the_whole_count_and_a_sorted_prefix(self):
        after = {"a": (0, 0, 300), "b": (0, 0, 300), "c": (0, 0, 300)}
        self.assertEqual(project.cap_changes(after, ["c", "a", "b"], limit=650),
                         ({"files": 3, "bytes": 900, "capped": True}, ["a", "b"]))
        self.assertEqual(project.cap_changes(after, ["c", "a"], limit=650),
                         ({"files": 2, "bytes": 600, "capped": False}, ["a", "c"]))
        self.assertEqual(project.cap_changes(after, ["a"], limit=10), ({"files": 1, "bytes": 300, "capped": True}, []))
        self.assertEqual(project.cap_changes(after, [], limit=10), ({"files": 0, "bytes": 0, "capped": False}, []))

    def test_list_input_prefixes_every_entry(self):
        self.assertEqual(project.list_input(["-C", "a b"]), "./-C\n./a b\n")

    def test_tar_list_with_a_dash_name(self):
        """Host control with bsdtar: a bare -C line is a directive (observed 2026-11-07), ./-C is a member."""
        d = pathlib.Path(tempfile.mkdtemp())
        tree(d / "h", {"-C": "x", "a b": "y"})
        (d / "list").write_text(project.list_input(["-C", "a b"]))
        subprocess.run(["tar", "czf", str(d / "o.tgz"), "-C", str(d / "h"), "-T", str(d / "list")], check=True, timeout=30,
                       env={**os.environ, "COPYFILE_DISABLE": "1"})  # no AppleDouble ._ entries from the host's xattrs
        with tarfile.open(d / "o.tgz") as t:
            self.assertEqual(sorted(t.getnames()), ["./-C", "./a b"])


if __name__ == "__main__":
    unittest.main()

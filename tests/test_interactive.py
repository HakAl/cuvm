"""Interactive mode (README, Interactive sessions): end detection, host prompt, dialog observer, deadline,
terminal restore, collection and CLI. Guest and host commands are stubbed (no VM, ssh or tart)."""
import io
import json
import os
import pathlib
import subprocess
import tempfile
import sys
import threading
import time
import unittest
from unittest import mock

from cuvm import __main__ as cli, config, dialog, interactive, run, session
from tests.test_run_lifecycle import bare, done

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = pathlib.Path(__file__).parent / "fixtures"
DIALOG = (FIX / "dialog-textedit.txt").read_text()
MENU = (FIX / "menu-mcp.txt").read_text()


class FakeSession:
    """attach() pops the next scripted outcome: it sets what alive() reports afterwards."""
    def __init__(self, after_attach, panes=()):
        self.after = list(after_attach)
        self.state = True
        self.attached = self.ended = 0
        self.panes = list(panes)
        self.keys_sent = []

    def attach(self):
        self.attached += 1
        nxt = self.after.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        self.state = nxt

    def alive(self):
        return self.state

    def end(self):
        self.ended += 1
        self.state = False

    def warn(self, msg):
        pass

    def pane(self, history=False):
        return self.panes.pop(0) if len(self.panes) > 1 else (self.panes[0] if self.panes else "")

    def keys(self, *k, literal=None):
        self.keys_sent.append((k, literal))


def never():
    return threading.Event()


class DriveTest(unittest.TestCase):
    def test_claude_exit_is_operator_exit(self):
        s = FakeSession([False])
        self.assertEqual(interactive.drive(s, never(), ask=lambda fired: self.fail("no prompt")), "operator_exit")
        self.assertEqual(s.ended, 0)

    def test_detach_then_end(self):
        s = FakeSession([True])
        self.assertEqual(interactive.drive(s, never(), ask=lambda fired: "e"), "operator_end")
        self.assertEqual(s.ended, 1)

    def test_detach_then_reattach_then_exit(self):
        s = FakeSession([True, False])
        answers = iter(["r"])
        self.assertEqual(interactive.drive(s, never(), ask=lambda fired: next(answers)), "operator_exit")
        self.assertEqual(s.attached, 2)

    def test_deadline_while_attached(self):
        fired = threading.Event()
        s = FakeSession([False])
        orig = s.attach
        s.attach = lambda: (fired.set(), orig())
        self.assertEqual(interactive.drive(s, fired, ask=lambda f: self.fail("no prompt")), "deadline")

    def test_deadline_while_prompting(self):
        s = FakeSession([True])
        self.assertEqual(interactive.drive(s, never(), ask=lambda fired: "deadline"), "deadline")

    def test_unreachable_guest_is_not_a_clean_exit(self):
        s = FakeSession([None])
        with mock.patch.object(interactive, "UNREACHABLE_S", 0.05), mock.patch.object(interactive, "RETRY_S", 0.01):
            self.assertEqual(interactive.drive(s, never(), ask=lambda f: "r"), "guest_unreachable")

    def test_transient_drop_recovers_to_prompt(self):
        s = FakeSession([None])
        calls = []

        def alive():
            calls.append(1)
            return None if len(calls) < 3 else True
        s.alive = alive
        with mock.patch.object(interactive, "RETRY_S", 0.01):
            self.assertEqual(interactive.drive(s, never(), ask=lambda f: "e"), "operator_end")


class AskTest(unittest.TestCase):
    def pipe(self, text=None):
        r, w = os.pipe()
        if text is not None:
            os.write(w, text.encode())
            os.close(w)
        else:
            self.addCleanup(os.close, w)
        f = os.fdopen(r)
        self.addCleanup(f.close)
        return f

    def ask(self, fired, stdin):
        """Bounded: a prompt that never returns fails the test instead of hanging the suite."""
        got = []
        t = threading.Thread(target=lambda: got.append(interactive.ask(fired, stdin=stdin, out=io.StringIO(), poll=0.02)),
                             daemon=True)
        t.start()
        t.join(2)
        return got

    def test_answers(self):
        for text, want in (("r\n", "r"), ("E\n", "e"), ("end\n", "e"), ("", "e"), ("x\nr\n", "r")):
            with self.subTest(text=text):
                self.assertEqual(self.ask(never(), self.pipe(text)), [want])

    def test_deadline_unblocks_a_waiting_prompt(self):
        fired = threading.Event()
        threading.Timer(0.1, fired.set).start()
        self.assertEqual(self.ask(fired, self.pipe()), ["deadline"])


class ObserverTest(unittest.TestCase):
    def test_one_record_per_dialog_cursor_moves_ignored_never_sends_keys(self):
        moved = DIALOG.replace("❯ ", "  ")
        moved = moved.replace("  Allow for this session", "❯ Allow for this session", 1)
        s = FakeSession([], panes=["idle", DIALOG, moved, DIALOG, "idle", "idle", DIALOG, "idle"])
        seen = []
        o = interactive.Observer(s, seen.append, every=0.01)
        o.start()
        time.sleep(0.3)
        o.stop()
        self.assertEqual(len(seen), 2, seen)
        self.assertEqual(seen[0]["decided_by"], "operator")
        self.assertEqual(seen[0]["apps"], ["TextEdit"])
        self.assertIsNotNone(seen[0]["last_seen"])
        self.assertEqual(s.keys_sent, [])

    def test_menus_are_not_recorded_as_dialogs(self):
        self.assertEqual(dialog.parse(MENU).kind, "unknown")  # the parser alone cannot tell
        s = FakeSession([], panes=["idle", MENU, MENU, "idle", DIALOG, "idle"])
        seen = []
        o = interactive.Observer(s, seen.append, every=0.01)
        o.start()
        time.sleep(0.3)
        o.stop()
        self.assertEqual([d["kind"] for d in seen], ["access"], seen)
        self.assertIsNotNone(seen[0]["last_seen"])

    def test_pane_errors_do_not_kill_the_observer(self):
        s = FakeSession([])
        n = []

        def pane(history=False):
            n.append(1)
            raise subprocess.TimeoutExpired("ssh", 1)
        s.pane = pane
        o = interactive.Observer(s, lambda d: None, every=0.01)
        o.start()
        time.sleep(0.1)
        o.stop()
        self.assertGreater(len(n), 2)


class DeadlineTest(unittest.TestCase):
    def test_cancel_waits_for_a_running_stop(self):  # agy candidate6-r5-work R5-W1
        done_ = []
        d = interactive.Deadline(0.05, warn=lambda: None, end=lambda: (time.sleep(0.5), done_.append(1)))
        d.start()
        self.assertTrue(d.fired.wait(2))  # fired is set before the stop runs, so a prompt unblocks at once
        d.cancel()
        self.assertEqual(done_, [1])

    def test_failing_callbacks_are_kept_not_raised(self):
        def boom():
            raise subprocess.TimeoutExpired("ssh", 20)
        d = interactive.Deadline(0.3, warn=boom, end=boom, warn_before=0.1)
        d.start()
        self.assertTrue(d.fired.wait(2))
        time.sleep(0.1)
        self.assertEqual(len(d.errors), 2, d.errors)
        self.assertIn("TimeoutExpired", d.errors[0])

    def test_fires_end_and_sets_event(self):
        ended, warned = [], []
        d = interactive.Deadline(0.05, warn=lambda: warned.append(1), end=lambda: ended.append(1))
        d.start()
        self.assertTrue(d.fired.wait(1))
        time.sleep(0.02)
        self.assertEqual(ended, [1])
        self.assertEqual(warned, [])  # no warning for a limit under the warning lead
        d.cancel()

    def test_warns_before_the_end(self):
        warned = []
        d = interactive.Deadline(0.3, warn=lambda: warned.append(1), end=lambda: None, warn_before=0.25)
        d.start()
        time.sleep(0.15)
        self.assertEqual(warned, [1])
        d.cancel()
        self.assertFalse(d.fired.wait(0.3))


class SessionTest(unittest.TestCase):
    def s(self, rc=0, exc=None):
        g = mock.Mock()
        g.run.side_effect = exc or (lambda *a, **k: done(rc))
        g.opts.return_value = ["-o", "BatchMode=yes"]
        g.user, g.ip = "admin", "192.0.2.5"
        return session.Session(g, "192.0.2.1", 1), g

    def test_alive_states(self):
        self.assertIs(self.s(0)[0].alive(), True)
        self.assertIs(self.s(1)[0].alive(), False)
        self.assertIsNone(self.s(255)[0].alive())
        self.assertIsNone(self.s(exc=subprocess.TimeoutExpired("ssh", 20))[0].alive())

    def test_attach_restores_the_terminal_even_when_interrupted(self):
        s, g = self.s()
        proc = mock.Mock(**{"wait.side_effect": [KeyboardInterrupt, 0], "poll.return_value": None})
        with mock.patch.object(session.subprocess, "Popen", return_value=proc), \
                mock.patch.object(session.sys, "stdout", io.StringIO()), \
                mock.patch.object(session.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(session.sys.stdin, "fileno", return_value=0), \
                mock.patch.object(session.termios, "tcgetattr", return_value=["saved"]), \
                mock.patch.object(session.termios, "tcsetattr") as tcset:
            with self.assertRaises(KeyboardInterrupt):
                s.attach()
        self.assertEqual(tcset.call_args[0][2], ["saved"])
        proc.kill.assert_called_once()  # an interrupted attach does not leave its ssh behind

    def test_attach_uses_pinned_options_and_a_tty(self):
        s, g = self.s()
        with mock.patch.object(session.subprocess, "Popen", return_value=mock.Mock(**{"wait.return_value": 0})) as r, \
                mock.patch.object(session.sys, "stdout", io.StringIO()), \
                mock.patch.object(session.sys.stdin, "isatty", return_value=False):
            s.attach()
        argv = r.call_args[0][0]
        self.assertEqual(argv[:2], ["ssh", "-t"])
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-1], "tmux attach -t cu")

    # operator run interactive-20261025T234032Z-012d showed "Connection to <ip> closed.t_attach"
    def test_attach_is_quiet_and_ends_its_line(self):
        s, g = self.s()
        out = io.StringIO()
        with mock.patch.object(session.subprocess, "Popen", return_value=mock.Mock(**{"wait.return_value": 0})) as r, \
                mock.patch.object(session.sys.stdin, "isatty", return_value=False), \
                mock.patch.object(session.sys, "stdout", out):
            s.attach()
        self.assertIn("-q", r.call_args[0][0])
        self.assertEqual(out.getvalue(), "\n")


class DeadlineReleaseTest(unittest.TestCase):
    """Lookout COMPUTERUSE-002 round 5 R5-F2: a failed guest stop must not keep the host attached."""

    def setUp(self):
        g = mock.Mock()
        g.opts.return_value, g.user, g.ip = ["-o", "BatchMode=yes"], "admin", "192.0.2.5"
        g.run.return_value = subprocess.CompletedProcess([], 255, "", "ssh: connect to host: timed out")
        self.s = session.Session(g, "192.0.2.1", 1)
        self.procs = []
        real = self.real_popen = subprocess.Popen

        def popen(argv, **k):  # stands in for the attach ssh: a live process that ignores the guest
            self.argv = argv
            p = real([sys.executable, "-c", "import time; time.sleep(60)"])
            self.procs.append(p)
            return p
        self.patches = [mock.patch.object(session.subprocess, "Popen", side_effect=popen),
                        mock.patch.object(session.sys.stdin, "isatty", return_value=False),
                        mock.patch.object(session.sys, "stdout", io.StringIO())]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in self.patches:
            x.stop()
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()

    def test_deadline_with_failed_stop_releases_the_attachment(self):
        fired = threading.Event()
        dl = interactive.Deadline(0.3, warn=lambda: None, end=lambda: (self.s.end(), self.s.release(grace=0.2)))
        out = []
        t0 = time.monotonic()
        dl.start()
        th = threading.Thread(target=lambda: out.append(interactive.drive(self.s, dl.fired, lambda f: "e")))
        th.start()
        th.join(10)
        self.assertFalse(th.is_alive(), "drive still attached after the deadline")
        self.assertEqual(out, ["deadline"])
        self.assertLess(time.monotonic() - t0, 8)
        self.assertIsNotNone(self.procs[0].poll())  # the attach process was reaped
        self.assertEqual(self.s.stops[0]["rc"], 255)  # the failed guest stop is recorded
        self.assertIn("timed out", self.s.stops[0]["stderr"])

    def test_stop_timeout_is_recorded_not_raised(self):
        self.s.g.run.side_effect = subprocess.TimeoutExpired("ssh", 20)
        rec = self.s.end()
        self.assertEqual((rec["rc"], rec["error"]), (None, "timed out"))
        self.assertEqual(self.s.stops, [rec])

    def test_attach_after_release_returns_at_once(self):
        self.s.release()
        self.assertIsNone(self.s.attach())
        self.assertEqual(self.procs, [])

    def test_normal_exit_returns_its_code(self):
        session.subprocess.Popen.side_effect = lambda argv, **k: self.real_popen([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(self.s.attach(), 3)
        self.assertEqual(self.s.release(), "not_attached")


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.r = bare(pathlib.Path(tempfile.mkdtemp()))
        self.r.mode = "interactive"
        self.r.r["mode"] = "interactive"
        self.r.session = mock.Mock()

    def guest(self, transcripts, listing=None, tar=None):
        g = mock.Mock()
        names = ["/u/.claude/projects/p/%d.jsonl" % i for i in range(len(transcripts))]
        g.cmds = []

        def run_(cmd, **k):
            g.cmds.append(cmd)
            if "/.claude/projects" in cmd:
                if isinstance(listing, BaseException):
                    raise listing
                return listing or done(0, "\n".join(names))
            if "tar czf" in cmd and tar is not None:
                return tar
            return done(0)
        g.run.side_effect = run_

        def pull(remote, local):
            local = pathlib.Path(local)
            local.parent.mkdir(parents=True, exist_ok=True)
            if remote in names:
                local.write_text("\n".join(json.dumps(l) for l in transcripts[names.index(remote)]))
            else:
                local.write_bytes(b"x")
            return True
        g.pull.side_effect = pull
        self.r.guest = g

    @staticmethod
    def use(name, result=None, ok=True):
        lines = [{"message": {"content": [{"type": "tool_use", "id": name, "name": name, "input": {}}]}}]
        if result is not None:
            lines.append({"message": {"content": [{"type": "tool_result", "tool_use_id": name, "is_error": not ok,
                                                    "content": [{"type": "text", "text": result}]}]}})
        return lines

    def test_zero_transcripts_is_not_a_failure(self):
        self.guest([])
        self.r.collect()
        a = self.r.r["artefacts"]
        self.assertEqual(self.r.r["fail_reasons"], [])
        self.assertEqual((a["transcript_files"], a["tools_used"], a["granted_outside_config"]), ([], [], []))
        self.assertNotIn("checks", a)

    # failed observation is not an empty session (Lookout COMPUTERUSE-002 round 5 R5-F1)
    def test_failed_transcript_listing_fails(self):
        self.guest([], listing=subprocess.CompletedProcess([], 255, "", "ssh: connect to host: Operation timed out"))
        self.r.collect()
        reasons = self.r.r["fail_reasons"]
        self.assertTrue(any("transcript listing failed" in f and "255" in f and "timed out" in f for f in reasons), reasons)

    def test_transcript_listing_timeout_fails(self):
        self.guest([], listing=subprocess.TimeoutExpired("ssh", 60))
        self.r.collect()
        self.assertTrue(any("transcript listing failed" in f for f in self.r.r["fail_reasons"]), self.r.r["fail_reasons"])

    def test_transcript_listing_tolerates_no_projects_dir(self):
        self.guest([])
        self.r.collect()
        cmd = next(c for c in self.r.guest.cmds if "/.claude/projects" in c)
        self.assertIn("[ -d", cmd)  # absent directory is an empty session, not an error
        self.assertEqual(self.r.r["fail_reasons"], [])

    # an archive failure is a failure, not an empty archive (round 5 R5-F3)
    def test_archive_failure_fails_and_is_not_replaced(self):
        self.guest([], tar=subprocess.CompletedProcess([], 1, "", "tar: ./secret: Permission denied"))
        self.r.collect()
        reasons = self.r.r["fail_reasons"]
        self.assertTrue(any("results archive" in f and "Permission denied" in f for f in reasons), reasons)
        self.assertFalse(any("/dev/null" in c for c in self.r.guest.cmds), self.r.guest.cmds)

    def test_transcript_listing_command_in_real_shells(self):
        for sh in ("/bin/zsh", "/bin/sh"):
            with self.subTest(sh), tempfile.TemporaryDirectory() as home:
                def ls():
                    return subprocess.run([sh, "-c", run.TRANSCRIPTS], env={"HOME": home, "PATH": "/usr/bin:/bin"},
                                          capture_output=True, text=True)
                self.assertEqual((ls().returncode, ls().stdout), (0, ""))  # no projects directory
                proj = pathlib.Path(home, ".claude/projects/-Users-admin-exp")
                proj.mkdir(parents=True)
                self.assertEqual((ls().returncode, ls().stdout), (0, ""))  # empty
                (proj / "a.jsonl").write_text("{}")
                (proj / "deeper").mkdir()
                (proj / "deeper/b.jsonl").write_text("{}")
                self.assertEqual(ls().stdout.split(), [str(proj / "a.jsonl")])
                proj.chmod(0)
                try:
                    self.assertNotEqual(ls().returncode, 0)  # unreadable: an error, not empty
                finally:
                    proj.chmod(0o755)

    def test_transcript_listing_fails_when_absence_is_not_established(self):  # Lookout round 6 R6-F1
        for sh in ("/bin/zsh", "/bin/sh"):
            with self.subTest(sh), tempfile.TemporaryDirectory() as home:
                def rc():
                    return subprocess.run([sh, "-c", run.TRANSCRIPTS], env={"HOME": home, "PATH": "/usr/bin:/bin"},
                                          capture_output=True, text=True).returncode
                claude = pathlib.Path(home, ".claude")
                self.assertEqual(rc(), 0)  # no .claude at all: Claude never ran
                claude.mkdir()
                self.assertEqual(rc(), 0)  # .claude without projects
                (claude / "projects").write_text("not a directory")
                self.assertNotEqual(rc(), 0)  # wrong object type
                (claude / "projects").unlink()
                (claude / "projects/p").mkdir(parents=True)
                (claude / "projects/p/audit.jsonl").write_text("{}")
                claude.chmod(0o600)  # ancestor not searchable: projects cannot be looked up
                try:
                    self.assertNotEqual(rc(), 0)
                finally:
                    claude.chmod(0o755)
                claude.rename(pathlib.Path(home, "moved"))
                claude.write_text("a file")
                self.assertNotEqual(rc(), 0)  # .claude is not a directory
                claude.unlink()
                claude.symlink_to(pathlib.Path(home, "nowhere"))
                self.assertNotEqual(rc(), 0)  # dangling link is not absence

    def test_transcripts_are_never_silently_skipped(self):  # agy candidate6-r6-work R6-W1
        """Listed (so a failed pull fails the run) or an error, never an empty success."""
        for sh in ("/bin/zsh", "/bin/sh"):
            with self.subTest(sh), tempfile.TemporaryDirectory() as home:
                def ls():
                    return subprocess.run([sh, "-c", run.TRANSCRIPTS], env={"HOME": home, "PATH": "/usr/bin:/bin"},
                                          capture_output=True, text=True)

                def seen(name):
                    r = ls()
                    return r.returncode != 0 or any(l.endswith(name) for l in r.stdout.split())
                c = pathlib.Path(home, ".claude")
                real = pathlib.Path(home, "elsewhere")
                (real / "p").mkdir(parents=True)
                (real / "p/linked-root.jsonl").write_text("{}")
                c.mkdir()
                (c / "projects").symlink_to(real)
                self.assertTrue(seen("linked-root.jsonl"))  # projects is a link
                (c / "projects").unlink()
                proj = c / "projects/p"
                proj.mkdir(parents=True)
                (c / "projects/q").symlink_to(real / "p")
                self.assertTrue(seen("q/linked-root.jsonl"))  # a project directory is a link
                (proj / "linked.jsonl").symlink_to(real / "p/linked-root.jsonl")
                self.assertTrue(seen("p/linked.jsonl"))  # a transcript is a link
                (proj / "audit.jsonl").write_text("{}")
                proj.chmod(0o600)  # readable, not searchable
                try:
                    self.assertTrue(seen("p/audit.jsonl"))
                finally:
                    proj.chmod(0o755)

    def test_listed_but_unpullable_transcript_fails(self):
        self.guest([], listing=done(0, "/u/.claude/projects/p/audit.jsonl"))
        self.r.guest.pull.side_effect = lambda remote, local: False if remote.endswith(".jsonl") else True
        self.r.collect()
        self.assertTrue(any("could not be pulled" in f for f in self.r.r["fail_reasons"]), self.r.r["fail_reasons"])

    def test_unsearchable_claude_dir_fails_the_record(self):  # R6-F1 end to end: real shell result, real collector
        with tempfile.TemporaryDirectory() as home:
            c = pathlib.Path(home, ".claude")
            (c / "projects/p").mkdir(parents=True)
            (c / "projects/p/audit.jsonl").write_text("{}")
            c.chmod(0o600)
            try:
                res = subprocess.run(["/bin/zsh", "-c", run.TRANSCRIPTS], env={"HOME": home, "PATH": "/usr/bin:/bin"},
                                     capture_output=True, text=True)
            finally:
                c.chmod(0o755)
        self.guest([], listing=res)
        self.r.collect()
        reasons = self.r.r["fail_reasons"]
        self.assertTrue(any("transcript listing failed" in f and "searchable" in f for f in reasons), reasons)

    def test_union_and_outside_config_recorded_not_failed(self):
        grant = json.dumps({"granted": [{"displayName": "Notes"}]})
        self.guest([self.use("mcp__computer-use__request_access", grant),
                    self.use("mcp__computer-use__screenshot")])
        self.r.collect()
        a = self.r.r["artefacts"]
        self.assertEqual(self.r.r["fail_reasons"], [])
        self.assertEqual(a["request_access_granted"], ["Notes"])
        self.assertEqual(a["granted_outside_config"], ["Notes"])
        self.assertEqual(a["tools_used"], ["mcp__computer-use__request_access", "mcp__computer-use__screenshot"])
        self.assertEqual(len(a["transcript_files"]), 2)

    def test_forbidden_tool_executed_still_fails(self):
        self.guest([self.use("Bash", "ran", ok=True)])
        self.r.collect()
        self.assertTrue(any("forbidden tools ran" in f for f in self.r.r["fail_reasons"]))

    def test_pane_log_pulled(self):
        self.guest([])
        self.r.collect()
        pulled = [c[0][0] for c in self.r.guest.pull.call_args_list]
        self.assertIn("~/.cuvm/pane.log", pulled)


class RunInteractiveTest(unittest.TestCase):
    def setUp(self):
        self.r = bare(pathlib.Path(tempfile.mkdtemp()))
        self.r.mode = "interactive"
        self.r.cfg["deadline_s"] = 3600
        self.r.session = mock.Mock(stops=[])

    def test_observer_and_deadline_stopped_when_drive_raises(self):
        obs, dl = mock.Mock(), mock.Mock(errors=[])
        with mock.patch.object(interactive, "Observer", return_value=obs), \
                mock.patch.object(interactive, "Deadline", return_value=dl), \
                mock.patch.object(interactive, "drive", side_effect=KeyboardInterrupt), \
                mock.patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                self.r.interactive()
        obs.stop.assert_called_once()
        dl.cancel.assert_called_once()

    def test_deadline_is_recorded_not_failed(self):
        with mock.patch.object(interactive, "Observer"), mock.patch.object(interactive, "Deadline"), \
                mock.patch.object(interactive, "drive", return_value="deadline"), mock.patch("builtins.print"):
            self.r.interactive()
        self.assertEqual(self.r.r["end_reason"], "deadline")
        self.assertEqual(self.r.r["fail_reasons"], [])

    def test_unreachable_fails(self):
        with mock.patch.object(interactive, "Observer"), mock.patch.object(interactive, "Deadline"), \
                mock.patch.object(interactive, "drive", return_value="guest_unreachable"), mock.patch("builtins.print"):
            self.r.interactive()
        self.assertTrue(self.r.r["fail_reasons"])

    def test_t_attach_marked_before_the_banner(self):
        lines = []
        with mock.patch.object(interactive, "Observer"), mock.patch.object(interactive, "Deadline"), \
                mock.patch.object(interactive, "drive", return_value="operator_exit"), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(map(str, a)))):
            self.r.interactive()
        at = next(i for i, l in enumerate(lines) if "t_attach" in l)
        banner = next(i for i, l in enumerate(lines) if "Exit Claude (/exit)" in l)
        self.assertLess(at, banner, lines)

    def test_deadline_releases_even_when_the_stop_raises(self):
        dl = {}
        self.r.session.end.side_effect = RuntimeError("boom")
        self.r.session.stops = [{"at": "x", "rc": 255, "stderr": "no route"}]
        with mock.patch.object(interactive, "Observer"), \
                mock.patch.object(interactive, "Deadline", side_effect=lambda *a, **k: dl.update(k) or mock.Mock(errors=[])), \
                mock.patch.object(interactive, "drive", return_value="deadline"), mock.patch("builtins.print"):
            self.r.interactive()
            with self.assertRaises(RuntimeError):
                dl["end"]()
        self.r.session.release.assert_called_once()
        self.assertEqual(self.r.r["session_stops"], [{"at": "x", "rc": 255, "stderr": "no route"}])

    def test_pane_logging_started(self):
        with mock.patch.object(interactive, "Observer"), mock.patch.object(interactive, "Deadline"), \
                mock.patch.object(interactive, "drive", return_value="operator_exit"), mock.patch("builtins.print"):
            self.r.interactive()
        self.r.session.log_pane.assert_called_once()


class ConfigTest(unittest.TestCase):
    def test_default_interactive_config_loads_only_in_interactive_mode(self):
        c, _ = config.load(cli.DEFAULT_INTERACTIVE, interactive=True)
        self.assertEqual(c["apps"], ["TextEdit"])
        self.assertNotIn("prompt", c)
        with self.assertRaises(config.ConfigError):
            config.load(cli.DEFAULT_INTERACTIVE)

    def test_scripted_keys_tolerated_in_interactive_mode(self):
        c, _ = config.load(ROOT / "experiments/smoke/config.json", interactive=True)
        self.assertIn("prompt", c)


class CliTest(unittest.TestCase):
    def runs(self, argv, tty=True):
        made = []

        class FakeRun:
            def __init__(self, path, **k):
                made.append((path, k))
                self.r = {}

            def execute(self):
                return True
        with mock.patch.object(run, "Run", FakeRun), mock.patch("builtins.print"), \
                mock.patch.object(cli.sys.stdin, "isatty", return_value=tty), \
                mock.patch.object(cli.sys.stdout, "isatty", return_value=tty):
            code = cli.main(argv)
        return code, made

    def test_default_config_and_flag_passed(self):
        code, made = self.runs(["run", "--interactive"])
        self.assertEqual(code, 0)
        self.assertEqual(pathlib.Path(made[0][0]), cli.DEFAULT_INTERACTIVE)
        self.assertIs(made[0][1]["interactive"], True)

    def test_refuses_repeat_and_no_tty(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            self.runs(["run", "--interactive", "--repeat", "2"])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            self.runs(["run", "--interactive"], tty=False)

    def test_scripted_still_needs_a_config(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            self.runs(["run"])
        code, made = self.runs(["run", "c.json"])
        self.assertIs(made[0][1]["interactive"], False)


if __name__ == "__main__":
    unittest.main()

"""Session.watch against a scripted terminal: approve, stop, deny and continue (the deny-and-continue design)."""
import pathlib
import subprocess
import unittest
from unittest import mock

from cuvm import codex, session

FIX = pathlib.Path(__file__).parent / "fixtures"
ONE = (FIX / "dialog-textedit.txt").read_text()          # [TextEdit], Deny selected
TWO = (FIX / "dialog-two-apps.txt").read_text()          # [Calculator, TextEdit], Deny selected
WORKING = (FIX / "pane-after-deny-working.txt").read_text()
IDLE = (FIX / "pane-after-deny-idle.txt").read_text()     # prompt suggestion on the input line
WARN = (FIX / "dialog-finder-warning.txt").read_text()     # [TextEdit, Finder], Finder row carries a warning


def on_allow(text):
    """Same dialog with the cursor on the allow option."""
    return text.replace("❯ Deny", "  Deny").replace("  Allow for this session", "❯ Allow for this session")


def reworded(text, n):
    return text.replace("Multiply two numbers", "Multiply two numbers (ask %d)" % n)


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Term:
    """A fake guest terminal. screen is the current pane; keys move between named states;
    each state may advance by itself after some polls."""

    def __init__(self, states, start, on_key=None, auto=None, done_in=()):
        self.states, self.state = states, start
        self.on_key, self.auto = on_key or {}, auto or {}
        self.done_in = set(done_in)
        self.polls = 0
        self.sent = []

    def pane(self):
        self.polls += 1
        nxt = self.auto.get(self.state)
        if nxt and self.polls > nxt[0]:
            self.state, self.polls = nxt[1], 0
        return self.states[self.state]

    def key(self, k):
        self.sent.append(k)
        nxt = self.on_key.get((self.state, k))
        if nxt:
            self.state, self.polls = nxt, 0


class FakeGuest:
    def __init__(self, term):
        self.term = term

    def ok(self, cmd):
        return cmd.startswith("test -s") and self.term.state in self.term.done_in


class FakeSession(session.Session):
    def __init__(self, term):
        super().__init__(FakeGuest(term), "192.168.64.1", 8888)
        self.term = term

    def pane(self, history=False):
        return self.term.pane()

    def keys(self, *keys, literal=None):
        if literal is not None:
            self.term.key("literal:" + literal)
        for k in keys:
            self.term.key(k)


def cfg(**kw):
    c = {"apps": ["TextEdit"], "grants": [], "allow_fullscreen": False}
    c.update(kw)
    return c


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(session, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.dialogs, self.reprompts = [], []

    def watch(self, term, c, deadline=600, reprompt=None):
        s = FakeSession(term)
        end = s.watch(c, "~/Desktop/x.txt", deadline, lambda d, ok, why, action: self.dialogs.append((d.apps, ok, why, action)),
                      reprompt=reprompt, on_reprompt=lambda text: self.reprompts.append(text))
        return end, term.sent

    def test_approve_in_config_then_complete(self):
        t = Term({"d": ONE, "a": on_allow(ONE), "w": WORKING, "i": IDLE}, "d",
                 on_key={("d", "Down"): "a", ("a", "Enter"): "w"}, auto={"w": (3, "i")}, done_in={"i"})
        end, sent = self.watch(t, cfg())
        self.assertEqual(end, "completed")
        self.assertEqual(sent, ["Down", "Enter"])
        self.assertEqual(self.dialogs, [(["TextEdit"], True, "apps ['TextEdit'] within config", "approved")])

    def test_approve_that_leaves_the_dialog_open_is_left_open(self):
        t = Term({"d": ONE, "a": on_allow(ONE)}, "d", on_key={("d", "Down"): "a"})
        end, sent = self.watch(t, cfg())
        self.assertEqual(end, "blocked_on_approval")
        self.assertEqual(sent, ["Down", "Enter"])
        self.assertEqual(self.dialogs[0][1:], (False, "dialog still open after allow", "left_open"))

    def test_default_stop_sends_no_keys(self):
        t = Term({"d": TWO}, "d")
        end, sent = self.watch(t, cfg())
        self.assertEqual((end, sent), ("blocked_on_approval", []))
        self.assertEqual(self.dialogs[0][3], "left_open")
        self.assertIn("apps outside config", self.dialogs[0][2])

    def test_deny_then_idle_without_reprompt(self):
        t = Term({"d": TWO, "w": WORKING, "i": IDLE}, "d", on_key={("d", "Enter"): "w"}, auto={"w": (3, "i")})
        end, sent = self.watch(t, cfg(on_refusal="deny"))
        self.assertEqual(end, "idle_after_denial")
        self.assertEqual(sent, ["Enter"])
        self.assertEqual(self.dialogs[0][1], False)
        self.assertEqual(self.dialogs[0][3], "denied")
        self.assertEqual(self.reprompts, [])
        self.assertGreaterEqual(self.clock.t, 20, "grace period before idle is judged")

    def test_deny_then_agent_completes(self):
        t = Term({"d": TWO, "w": WORKING, "i": IDLE}, "d", on_key={("d", "Enter"): "w"}, auto={"w": (3, "i")}, done_in={"i"})
        end, _ = self.watch(t, cfg(on_refusal="deny"))
        self.assertEqual(end, "completed")

    def test_reprompt_sent_once_then_idle_ends(self):
        t = Term({"d": TWO, "w": WORKING, "i": IDLE, "w2": WORKING, "i2": IDLE}, "d",
                 on_key={("d", "Enter"): "w", ("i", "Enter"): "w2"}, auto={"w": (3, "i"), "w2": (3, "i2")})
        end, sent = self.watch(t, cfg(on_refusal="deny"), reprompt="Carry on without it.")
        self.assertEqual(end, "idle_after_denial")
        self.assertEqual(sent, ["Enter", "literal:Carry on without it.", "Enter"])
        self.assertEqual(self.reprompts, ["Carry on without it."])

    def test_reprompt_then_new_ask_is_denied_and_counted(self):
        t = Term({"d": TWO, "w": WORKING, "i": IDLE, "d2": reworded(TWO, 2), "w2": WORKING, "i2": IDLE}, "d",
                 on_key={("d", "Enter"): "w", ("i", "Enter"): "d2", ("d2", "Enter"): "w2"},
                 auto={"w": (3, "i"), "w2": (3, "i2")})
        end, _ = self.watch(t, cfg(on_refusal="deny"), reprompt="Carry on.")
        self.assertEqual(end, "idle_after_denial")
        self.assertEqual([x[3] for x in self.dialogs], ["denied", "denied"])
        self.assertEqual(self.reprompts, ["Carry on."], "one reprompt per run")

    def test_denial_cap(self):
        states = {"d%d" % i: reworded(TWO, i) for i in range(5)}
        on_key = {("d%d" % i, "Enter"): "d%d" % (i + 1) for i in range(4)}
        end, sent = self.watch(Term(states, "d0", on_key=on_key), cfg(on_refusal="deny"))
        self.assertEqual(end, "blocked_on_approval")
        self.assertEqual([x[3] for x in self.dialogs], ["denied"] * 3 + ["left_open"])
        self.assertIn("denial cap", self.dialogs[-1][2])
        self.assertEqual(sent, ["Enter"] * 3)

    def test_malformed_outside_dialog_is_never_denied(self):
        bad = TWO.replace("◉ TextEdit", "◉ TextEdit\n   ⚠ not a known warning")
        end, sent = self.watch(Term({"d": bad}, "d"), cfg(on_refusal="deny"))
        self.assertEqual((end, sent), ("blocked_on_approval", []))
        self.assertIn("unrecognized lines", self.dialogs[0][2])

    def test_count_mismatch_outside_dialog_is_never_denied(self):
        bad = TWO.replace("(2 apps)", "(1 app)")
        end, sent = self.watch(Term({"d": bad}, "d"), cfg(on_refusal="deny"))
        self.assertEqual((end, sent), ("blocked_on_approval", []))

    def test_dialog_without_deny_line_is_never_denied(self):
        bad = "\n".join(l for l in TWO.splitlines() if "Deny, and tell" not in l)
        end, sent = self.watch(Term({"d": bad}, "d"), cfg(on_refusal="deny"))
        self.assertEqual((end, sent), ("blocked_on_approval", []))

    def test_fullscreen_with_deny_is_left_open(self):
        fs = "────\n  Claude wants to take over the full screen\n  ❯ Allow\n  Enter to confirm · Esc to cancel\n"
        end, sent = self.watch(Term({"d": fs}, "d"), cfg(on_refusal="deny"))
        self.assertEqual((end, sent), ("blocked_on_approval", []))

    def test_deny_not_selectable(self):
        stuck = on_allow(TWO)  # cursor on Allow, Up does nothing in this fake
        end, sent = self.watch(Term({"d": stuck}, "d"), cfg(on_refusal="deny"))
        self.assertEqual(end, "blocked_on_approval")
        self.assertNotIn("Enter", sent)
        self.assertEqual(self.dialogs[0][2:], ("deny option could not be selected", "left_open"))

    def test_deny_moves_cursor_up_to_deny(self):
        t = Term({"a": on_allow(TWO), "d": TWO, "w": WORKING, "i": IDLE}, "a",
                 on_key={("a", "Up"): "d", ("d", "Enter"): "w"}, auto={"w": (3, "i")})
        end, sent = self.watch(t, cfg(on_refusal="deny"))
        self.assertEqual(sent, ["Up", "Enter"])
        self.assertEqual(self.dialogs[0][3], "denied")

    def test_dialog_still_open_after_deny(self):
        end, sent = self.watch(Term({"d": TWO}, "d"), cfg(on_refusal="deny"))
        self.assertEqual((end, sent), ("blocked_on_approval", ["Enter"]))
        self.assertEqual(self.dialogs[0][2:], ("dialog still open after deny", "left_open"))

    def test_work_between_idle_samples_resets(self):
        # idle, working, idle, working ... never two idle samples 6 s apart: runs to the deadline
        t = Term({"d": TWO, "i": IDLE, "w": WORKING}, "d", on_key={("d", "Enter"): "i"},
                 auto={"i": (1, "w"), "w": (1, "i")})
        end, _ = self.watch(t, cfg(on_refusal="deny"), deadline=120)
        self.assertEqual(end, "deadline")

    def test_idle_needs_the_prompt_frame(self):
        blank = "\n".join(l for l in IDLE.splitlines() if not l.startswith("────"))
        t = Term({"d": TWO, "b": blank}, "d", on_key={("d", "Enter"): "b"})
        end, _ = self.watch(t, cfg(on_refusal="deny"), deadline=120)
        self.assertEqual(end, "deadline")



class ExecGuest:
    """A fake guest for watch_exec: each poll answers from the timeline (exit_full, exit_exists, done_full); the
    last entry repeats. Commands are kept so the tests can see what was asked and in what order."""
    def __init__(self, timeline):
        self.timeline, self.i, self.cmds = list(timeline), 0, []

    def run(self, cmd, timeout=120, input=None):
        self.cmds.append(cmd)
        if "kill-window" in cmd:
            return subprocess.CompletedProcess([], 0, "", "")
        st = self.timeline[min(self.i, len(self.timeline) - 1)]
        self.i += 1
        out = []
        if ("test -s %s" % codex.EXIT in cmd and st.get("exit_full")) or ("test -e %s" % codex.EXIT in cmd and (st.get("exit_exists") or st.get("exit_full"))):
            out.append("EXIT")
        if ("test -s %s" % DONE in cmd and st.get("done_full")) or ("test -e %s" % DONE in cmd and (st.get("done_exists") or st.get("done_full"))):
            out.append("DONE")
        return subprocess.CompletedProcess([], 0, "\n".join(out) + "\n", "")


DONE = "~/Desktop/codex.txt"


class WatchExecTest(unittest.TestCase):
    """Session.watch_exec (the codex design step 4, scripted codex): the exit file is created empty by the shell and
    filled when codex exits; the done file is the agent's."""
    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(session, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def watch(self, timeline, deadline=600):
        g = ExecGuest(timeline)
        s = session.Session(g, "192.168.64.1", 8888, agent="codex")
        return s.watch_exec(DONE, deadline), g

    def test_completed_when_exit_fills_and_done_is_non_empty(self):
        end, g = self.watch([{}, {}, {"exit_full": 1, "done_full": 1}])
        self.assertEqual(end, "completed")
        self.assertFalse(any("kill-window" in c for c in g.cmds))
        self.assertIn("test -s %s" % codex.EXIT, g.cmds[0])
        self.assertIn("test -s %s" % DONE, g.cmds[0])

    def test_done_filled_during_the_grace_is_completed(self):
        end, _ = self.watch([{"exit_full": 1}, {"exit_full": 1}, {"exit_full": 1, "done_full": 1}])
        self.assertEqual(end, "completed")

    def test_exit_without_done_after_the_grace(self):
        end, g = self.watch([{"exit_full": 1}])
        self.assertEqual(end, "exec_exited_without_done")
        self.assertGreaterEqual(self.clock.t, session.EXEC_GRACE_S)
        self.assertLess(self.clock.t, 60)
        self.assertFalse(any("kill-window" in c for c in g.cmds))

    def test_deadline_kills_the_agent_window(self):
        end, g = self.watch([{}], deadline=20)
        self.assertEqual(end, "deadline")
        self.assertIn("tmux kill-window -t cu:agent", g.cmds[-1])

    def test_empty_exit_file_is_still_running(self):
        """The shell opens the exit file before codex runs; only its content means the exec ended (test -s)."""
        end, _ = self.watch([{"exit_exists": 1}] * 5 + [{"exit_full": 1, "done_full": 1}])
        self.assertEqual(end, "completed")
        self.assertGreater(self.clock.t, session.EXEC_GRACE_S)

    def test_done_file_must_be_non_empty(self):
        end, _ = self.watch([{"exit_full": 1, "done_exists": 1}])
        self.assertEqual(end, "exec_exited_without_done")


class RecordingGuest:
    def __init__(self, answers=()):
        self.cmds, self.answers = [], dict(answers)

    def run(self, cmd, timeout=120, input=None):
        self.cmds.append((cmd, input))
        out = next((v for k, v in self.answers.items() if k in cmd), "")
        return subprocess.CompletedProcess([], 0, out, "")

    def ok(self, cmd, timeout=30):
        self.cmds.append((cmd, None))
        return True

    def wait(self, check, timeout, every=1.0):
        return check()


class CodexSessionTest(unittest.TestCase):
    """Session dispatch on agent (the codex design step 5); the claude strings are untouched."""
    def session(self, agent="codex", **answers):
        g = RecordingGuest(answers)
        return session.Session(g, "192.168.64.1", 8888, agent=agent), g

    def test_start_has_no_keychain_unlock(self):
        s, g = self.session()
        s.start(["--model", "gpt-6 x"])
        cmd = g.cmds[0][0]
        self.assertTrue(cmd.startswith("tmux new -d -s cu -c ~/exp "), cmd)
        self.assertIn("CODEX_CA_CERTIFICATE=", cmd)
        self.assertIn("exec codex --model 'gpt-6 x'", shlex_unquote(cmd))
        self.assertNotIn("security", cmd)
        s, g = self.session(agent="claude")
        s.start(["--model", "x"])
        self.assertIn("security unlock-keychain", g.cmds[0][0])
        self.assertIn("exec claude --model x", shlex_unquote(g.cmds[0][0]))

    def test_holding_and_exec_window(self):
        s, g = self.session()
        s.start_holding()
        s.exec_start("cd ~/exp && codex exec x")
        self.assertEqual(g.cmds[0][0], "tmux new -d -s cu -c ~/exp")
        self.assertTrue(g.cmds[1][0].startswith("tmux new-window -d -t cu -n agent "), g.cmds[1][0])
        self.assertIn("codex exec x", shlex_unquote(g.cmds[1][0]))

    def test_ready_and_idle_dispatch(self):
        s, _ = self.session()
        ready = (FIX / "codex/tui-ready.txt").read_text()
        self.assertTrue(s.ready(ready))
        self.assertTrue(s.idle(ready))
        self.assertFalse(s.ready("Claude Code ❯"))
        c, _ = self.session(agent="claude")
        self.assertTrue(c.ready("Claude Code\n❯ "))
        self.assertFalse(c.ready(ready))
        self.assertTrue(c.idle(IDLE))

    def test_reach_check_kills_the_window_before_removing_files(self):
        for agent in ("codex", "claude"):
            s, g = self.session(agent=agent, **{"kill-window": "PONG\n\n---cuvm-reach-events---\n{\"type\":\"thread.started\"}\n"})
            ok, out = s.reach_check(5)
            self.assertTrue(ok)
            start = g.cmds[0][0]
            self.assertTrue(start.startswith("rm -f ~/reach.txt"), start)
            self.assertIn("tmux new-window -d -t cu -n reach ", start)
            last = g.cmds[-1][0]
            self.assertLess(last.index("kill-window"), last.index("rm -f"))
            if agent == "codex":
                inner = shlex_unquote(start)
                for want in ("--ephemeral", "</dev/null", "~/reach-events.jsonl", "CODEX_CA_CERTIFICATE="):
                    self.assertIn(want, inner)
                self.assertIn("rm -f ~/reach.txt ~/reach-events.jsonl", last)
                self.assertEqual(out.strip(), "PONG")
                self.assertEqual(s.reach_events, "{\"type\":\"thread.started\"}\n")
            else:
                self.assertIn("claude -p", shlex_unquote(start))
                self.assertIsNone(s.reach_events)

    def test_reach_failure_output_has_the_events_tail(self):
        s, g = self.session(**{"kill-window": "\n---cuvm-reach-events---\n{\"type\":\"error\",\"message\":\"401\"}\n"})
        g.ok = lambda cmd, timeout=30: False
        ok, out = s.reach_check(5)
        self.assertFalse(ok)
        self.assertIn("401", out)

    def test_e2_e3_remove_and_inject_dispatch(self):
        s, g = self.session()
        s.e3(); s.e2(); s.remove_login(); s.inject_auth_json("{\"tokens\": {}}\n")
        self.assertEqual([c for c, _ in g.cmds[:3]], [codex.e3_cmd(), codex.e2_cmd(), codex.remove_cmd()])
        self.assertEqual(g.cmds[3], ("umask 077; mkdir -p ~/.codex && cat > ~/.codex/auth.json", "{\"tokens\": {}}\n"))


def shlex_unquote(cmd):
    import shlex
    return " ".join(shlex.split(cmd))


class ExperimentRecordTest(unittest.TestCase):
    """Run.experiment wires watch's callbacks into the record: action, reprompts, {run_id} in the reprompt."""

    def test_record_fields(self):
        from cuvm import run, dialog

        class S:
            def deliver(self, prompt):
                self.prompt = prompt

            def pane(self):
                return "pane"

            def watch(self, cfg, done, deadline, on_dialog, reprompt=None, on_reprompt=None):
                self.reprompt = reprompt
                on_dialog(dialog.parse(TWO), False, "apps outside config: ['Calculator']", "denied")
                on_dialog(dialog.parse(WARN), False, "apps outside config: ['Finder']", "denied")
                on_reprompt(reprompt)
                return "idle_after_denial"

        r = object.__new__(run.Run)
        r.id, r.session, r.r, saved = "run-1", S(), {"dialogs": [], "fail_reasons": []}, {}
        r.cfg = {"prompt": "p {run_id}", "done": {"guest_file": "~/x"}, "deadline_s": 60,
                 "on_refusal": "deny", "refusal_reprompt": "go on {run_id}"}
        r.log = lambda m: None
        r.mark = lambda *a, **k: None
        r.save = lambda name, text: saved.update({name: text})
        r.fail = lambda why: r.r["fail_reasons"].append(why)
        r.experiment()
        self.assertEqual(r.session.reprompt, "go on run-1")
        self.assertEqual(r.r["dialogs"][0]["action"], "denied")
        self.assertIs(r.r["dialogs"][0]["approved"], False)
        self.assertEqual([x["warnings"] for x in r.r["dialogs"]], [{}, {"Finder": "can read/write any file"}])
        self.assertEqual([x["text"] for x in r.r["reprompts"]], ["go on run-1"])
        self.assertEqual(saved, {"pane-reprompt-1.txt": "pane"})
        self.assertEqual(r.r["end_reason"], "idle_after_denial")
        self.assertEqual(r.r["fail_reasons"], ["end_reason idle_after_denial"])


if __name__ == "__main__":
    unittest.main()

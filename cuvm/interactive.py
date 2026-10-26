"""Interactive sessions (README, Interactive sessions): the operator is attached to the guest tmux pane.

drive() attaches until Claude exits (the session's only process, so the session ends), the
operator detaches and chooses to end, the deadline fires, or the guest stops answering. The
Observer records approval dialogs the operator answers; it never sends keys.
"""
import datetime
import select
import sys
import threading
import time

from cuvm import dialog

RETRY_S = 3
UNREACHABLE_S = 15
PROMPT = "cuvm: detached. [r]eattach or [e]nd the run? "


def now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def drive(session, fired, ask):
    """Returns end_reason: operator_exit, operator_end, deadline or guest_unreachable."""
    while True:
        session.attach()
        if fired.is_set():  # before any guest probe: the deadline does not wait on the guest
            return "deadline"
        state = session.alive()
        end = time.monotonic() + UNREACHABLE_S
        while state is None and time.monotonic() < end:
            time.sleep(RETRY_S)
            state = session.alive()
        if fired.is_set():
            return "deadline"
        if state is None:
            return "guest_unreachable"
        if state is False:
            return "operator_exit"
        answer = ask(fired)
        if answer == "deadline":
            return "deadline"
        if answer == "e":
            session.end()
            return "operator_end"


def ask(fired, stdin=None, out=None, poll=1.0):
    """r, e or deadline. Polls stdin so the deadline can end a forgotten prompt; EOF means end."""
    stdin, out = stdin or sys.stdin, out or sys.stdout
    out.write(PROMPT)
    out.flush()
    while True:
        if fired.is_set():
            out.write("\ncuvm: time limit reached, ending the run\n")
            return "deadline"
        ready, _, _ = select.select([stdin], [], [], poll)
        if not ready:
            continue
        line = stdin.readline()
        if line == "":
            return "e"
        c = line.strip().lower()[:1]
        if c in ("r", "e"):
            return c
        out.write(PROMPT)
        out.flush()


class Observer:
    """Polls the pane; one record per distinct open dialog, keyed so cursor moves do not split it.
    Only approval dialogs (access, fullscreen) are recorded: the operator also opens menus such as
    /mcp that share the Enter-to-confirm footer and would otherwise read as unknown dialogs."""

    KINDS = ("access", "fullscreen")

    def __init__(self, session, record, every=2.0):
        self.session, self.record, self.every = session, record, every
        self.stopped = threading.Event()
        self.open, self.open_key = None, None
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=30)
        self._close()

    def _close(self):
        if self.open is not None:
            self.open["last_seen"] = now()
        self.open, self.open_key = None, None

    def _loop(self):
        while not self.stopped.wait(self.every):
            try:
                d = dialog.parse(self.session.pane())
                if d is not None and d.kind not in self.KINDS:
                    d = None
            except Exception:  # a slow or failed capture is skipped, never fatal
                continue
            key = None if d is None else (d.kind, tuple(d.apps), tuple(d.grants), tuple(d.extra))
            if key == self.open_key:
                continue
            self._close()
            if d is not None:
                self.open, self.open_key = {
                    "at": now(), "first_seen": now(), "last_seen": None, "kind": d.kind, "apps": d.apps,
                    "grants": d.grants, "extra": d.extra, "approved": None, "why": "answered by the operator",
                    "decided_by": "operator", "text": d.text}, key
                self.record(self.open)


class Deadline:
    """Warns warn_before seconds ahead (only if the limit is longer), then ends the session."""

    def __init__(self, seconds, warn, end, warn_before=300):
        self.fired = threading.Event()
        self.errors = []  # a failing callback is kept here, never printed over the operator's pane
        self.timers = [threading.Timer(seconds, self._end, (end,))]
        if seconds > warn_before:
            self.timers.append(threading.Timer(seconds - warn_before, self._call, (warn,)))
        for t in self.timers:
            t.daemon = True

    def _call(self, f):
        try:
            f()
        except Exception as e:
            self.errors.append(repr(e)[:200])

    def _end(self, end):
        self.fired.set()
        self._call(end)

    def start(self):
        for t in self.timers:
            t.start()

    def cancel(self):
        """Stops pending timers and waits for one already running, so its stop is finished and
        recorded before collect (agy R5-W1). Bounded: end() 20 s plus release() 7 s."""
        for t in self.timers:
            t.cancel()
        for t in self.timers:
            if t.is_alive() and t is not threading.current_thread():
                t.join(timeout=40)

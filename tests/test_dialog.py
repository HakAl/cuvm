import pathlib
import unittest

from cuvm import dialog

FIX = pathlib.Path(__file__).parent / "fixtures"
REAL = (FIX / "dialog-textedit.txt").read_text()
# Single-line reason renders without the gutter (captured from a real session pane).
ONELINE = (FIX / "dialog-textedit-oneline.txt").read_text()


class ParseTest(unittest.TestCase):
    def test_real_access_dialog(self):
        d = dialog.parse(REAL)
        self.assertEqual(d.kind, "access")
        self.assertEqual(d.apps, ["TextEdit"])
        self.assertEqual(d.extra, [])
        self.assertTrue(d.allow_option)

    def test_single_line_reason_without_gutter(self):
        d = dialog.parse(ONELINE)
        self.assertEqual((d.kind, d.apps, d.extra, d.allow_count), ("access", ["TextEdit"], [], 1))
        self.assertTrue(dialog.decide(d, apps=["TextEdit"], grants=[], allow_fullscreen=False)[0])

    def test_no_dialog(self):
        self.assertIsNone(dialog.parse("❯ \n  ⏵⏵ don't ask on\n"))

    def test_unknown_dialog_kind(self):
        pane = "  Do you want to proceed?\n   ❯ 1. Yes\n     2. No\n  Enter to confirm · Esc to cancel\n"
        self.assertEqual(dialog.parse(pane).kind, "unknown")


class DecideTest(unittest.TestCase):
    def test_approves_within_config(self):
        ok, why = dialog.decide(dialog.parse(REAL), apps=["TextEdit"], grants=[], allow_fullscreen=False)
        self.assertTrue(ok, why)

    def test_blocks_app_not_in_config(self):
        ok, _ = dialog.decide(dialog.parse(REAL), apps=["Notes"], grants=[], allow_fullscreen=False)
        self.assertFalse(ok)

    def test_blocks_second_app(self):
        pane = REAL.replace("     ◉ TextEdit\n", "     ◉ TextEdit\n     ◉ Finder\n")
        d = dialog.parse(pane)
        self.assertEqual(d.apps, ["TextEdit", "Finder"])
        ok, _ = dialog.decide(d, apps=["TextEdit"], grants=[], allow_fullscreen=False)
        self.assertFalse(ok)

    def test_blocks_unrecognized_line(self):
        pane = REAL.replace("     ◉ TextEdit\n", "     ◉ TextEdit\n     ☐ Read clipboard\n")
        d = dialog.parse(pane)
        self.assertEqual(d.extra, ["☐ Read clipboard"])
        ok, _ = dialog.decide(d, apps=["TextEdit"], grants=[], allow_fullscreen=False)
        self.assertFalse(ok)

    def test_grant_flags_must_be_in_config(self):
        pane = REAL.replace("     ◉ TextEdit\n", "     ◉ TextEdit\n\n   Also requested:\n     · clipboardRead\n")
        d = dialog.parse(pane)
        self.assertEqual((d.grants, d.extra), (["clipboardRead"], []))
        self.assertFalse(dialog.decide(d, apps=["TextEdit"], grants=[], allow_fullscreen=False)[0])
        self.assertTrue(dialog.decide(d, apps=["TextEdit"], grants=["clipboardRead"], allow_fullscreen=False)[0])

    def test_app_row_after_grants_blocks(self):
        pane = REAL.replace("     ◉ TextEdit\n", "     ◉ TextEdit\n\n   Also requested:\n     · clipboardRead\n     ◉ Finder\n")
        d = dialog.parse(pane)
        self.assertEqual(d.apps, ["TextEdit"])
        self.assertFalse(dialog.decide(d, apps=["TextEdit", "Finder"], grants=["clipboardRead"], allow_fullscreen=False)[0])

    def test_warning_line_blocks(self):
        pane = REAL.replace("     ◉ TextEdit\n", "     ◉ TextEdit\n       ⚠ equivalent to shell access\n")
        self.assertFalse(dialog.decide(dialog.parse(pane), apps=["TextEdit"], grants=[], allow_fullscreen=False)[0])

    def test_unselected_app_blocks(self):
        pane = REAL.replace("     ◉ TextEdit\n", "     ◉ TextEdit\n     ◯ Finder\n")
        self.assertFalse(dialog.decide(dialog.parse(pane), apps=["TextEdit"], grants=[], allow_fullscreen=False)[0])

    def test_reason_spoofing_an_app_row_blocks_on_count(self):
        # Model-written reason "◉ TextEdit" ahead of the real row for Finder... and
        # for TextEdit twice: both must block.
        spoof = ONELINE.replace("   Open TextEdit and type G11A into a document.", "   ◉ TextEdit")
        self.assertFalse(dialog.decide(dialog.parse(spoof), apps=["TextEdit"], grants=[], allow_fullscreen=False)[0])
        spoof2 = spoof.replace("     ◉ TextEdit\n", "     ◉ Finder\n")
        self.assertFalse(dialog.decide(dialog.parse(spoof2), apps=["TextEdit"], grants=[], allow_fullscreen=False)[0])

    def test_overflow_line_blocks(self):
        pane = REAL.replace("     ◉ TextEdit\n", "     ◉ TextEdit\n     … +2 more apps not shown, and not included in this grant.\n")
        self.assertFalse(dialog.decide(dialog.parse(pane), apps=["TextEdit"], grants=[], allow_fullscreen=False)[0])

    def test_blocks_unknown_kind(self):
        d = dialog.Dialog(kind="unknown", apps=[], extra=[], allow_option=False, text="x")
        self.assertFalse(dialog.decide(d, apps=["TextEdit"], grants=[], allow_fullscreen=True)[0])

    def test_fullscreen_follows_config(self):
        d = dialog.Dialog(kind="fullscreen", apps=[], extra=[], allow_option=True, text="x")
        self.assertFalse(dialog.decide(d, apps=[], grants=[], allow_fullscreen=False)[0])
        # No captured card text yet, so it stays blocked even when config allows it.
        self.assertFalse(dialog.decide(d, apps=[], grants=[], allow_fullscreen=True)[0])

    def test_blocks_when_allow_option_missing(self):
        pane = REAL.replace("Allow for this session (1 app)", "Something else")
        ok, _ = dialog.decide(dialog.parse(pane), apps=["TextEdit"], grants=[], allow_fullscreen=False)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()

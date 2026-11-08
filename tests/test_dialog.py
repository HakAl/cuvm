import pathlib
import unittest

from cuvm import dialog

FIX = pathlib.Path(__file__).parent / "fixtures"
REAL = (FIX / "dialog-textedit.txt").read_text()
# Single-line reason renders without the gutter (captured from a real session pane).
ONELINE = (FIX / "dialog-textedit-oneline.txt").read_text()


TWO = (FIX / "dialog-two-apps.txt").read_text()
# Captured Haiku L3 run: Finder row carries Claude Code's risk annotation.
WARN = (FIX / "dialog-finder-warning.txt").read_text()


class ClassifyTest(unittest.TestCase):
    """Structure is checked before the config (the deny-and-continue design DC-F1)."""

    def test_captured_two_app_dialog(self):
        d = dialog.parse(TWO)
        self.assertEqual((d.apps, d.allow_count, d.extra, d.deny_option), (["Calculator", "TextEdit"], 2, [], True))
        self.assertEqual(dialog.classify(d, ["TextEdit"], [], False)[0], "outside_config")
        self.assertEqual(dialog.classify(d, ["TextEdit", "Calculator"], [], False)[0], "ok")

    def test_outside_app_with_extra_lines_is_malformed(self):
        d = dialog.parse(TWO.replace("◉ TextEdit", "◉ TextEdit\n   ✔ Finder (already granted)"))
        self.assertEqual(dialog.classify(d, ["TextEdit"], [], False)[0], "malformed")

    def test_outside_app_with_count_mismatch_is_malformed(self):
        d = dialog.parse(TWO.replace("(2 apps)", "(3 apps)"))
        self.assertEqual(dialog.classify(d, ["TextEdit"], [], False)[0], "malformed")

    def test_missing_deny_line_is_malformed(self):
        d = dialog.parse("\n".join(l for l in TWO.splitlines() if "Deny, and tell" not in l))
        self.assertFalse(d.deny_option)
        self.assertEqual(d.extra, [])
        self.assertEqual(dialog.classify(d, ["TextEdit", "Calculator"], [], False)[0], "malformed")

    def test_outside_grant_on_clean_dialog(self):
        pane = REAL.replace("     Allow for this session", "   Also requested:\n     · clipboardWrite\n     Allow for this session")
        d = dialog.parse(pane)
        self.assertEqual(d.grants, ["clipboardWrite"])
        self.assertEqual(dialog.classify(d, ["TextEdit"], [], False), ("outside_config", "grants outside config: ['clipboardWrite']"))

    def test_non_access_codes(self):
        fs = dialog.Dialog("fullscreen")
        self.assertEqual(dialog.classify(fs, ["TextEdit"], [], True)[0], "fullscreen")
        self.assertEqual(dialog.classify(dialog.Dialog("unknown"), ["TextEdit"], [], True)[0], "unknown")


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

    def test_known_warning_under_app_row_is_recorded(self):
        pane = REAL.replace("     ◉ TextEdit\n", "     ◉ TextEdit\n       ⚠ equivalent to shell access\n")
        d = dialog.parse(pane)
        self.assertEqual((d.extra, d.warnings), ([], {"TextEdit": "equivalent to shell access"}))

    def test_unknown_warning_text_blocks(self):
        pane = REAL.replace("     ◉ TextEdit\n", "     ◉ TextEdit\n       ⚠ can do anything at all\n")
        d = dialog.parse(pane)
        self.assertEqual((d.warnings, d.extra), ({}, ["⚠ can do anything at all"]))
        self.assertFalse(dialog.decide(d, apps=["TextEdit"], grants=[], allow_fullscreen=False)[0])

    def test_warning_not_directly_under_app_row_blocks(self):
        for pane in (
            # after the hidden-apps line
            WARN.replace("       ⚠ can read/write any file\n", "").replace(
                "while Claude works.\n", "while Claude works.\n   ⚠ can read/write any file\n"),
            # under a grant row
            REAL.replace("     Allow for this session", "   Also requested:\n     · clipboardWrite\n"
                         "       ⚠ can read/write any file\n     Allow for this session"),
            # a second warning under the same app
            WARN.replace("       ⚠ can read/write any file\n",
                         "       ⚠ can read/write any file\n       ⚠ can change system settings\n"),
        ):
            d = dialog.parse(pane)
            self.assertEqual(dialog.classify(d, ["TextEdit", "Finder"], ["clipboardWrite"], False)[0], "malformed", pane)

    def test_warning_text_in_reason_is_not_a_warning(self):
        # The reason is model-written free text ahead of the first app row.
        d = dialog.parse(ONELINE.replace("   Open TextEdit and type G11A into a document.", "   ⚠ can read/write any file"))
        self.assertEqual((d.apps, d.extra, d.warnings), (["TextEdit"], [], {}))


class WarningTest(unittest.TestCase):
    def test_captured_finder_warning_dialog(self):
        d = dialog.parse(WARN)
        self.assertEqual((d.kind, d.apps, d.extra, d.allow_count, d.deny_option),
                         ("access", ["TextEdit", "Finder"], [], 2, True))
        self.assertEqual(d.warnings, {"Finder": "can read/write any file"})
        self.assertEqual(dialog.classify(d, ["TextEdit"], [], False)[0], "outside_config")
        self.assertEqual(dialog.classify(d, ["TextEdit", "Finder"], [], False)[0], "ok")

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

import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cuvm import image


class ImageRecordTest(unittest.TestCase):
    def setUp(self):
        self.tart = pathlib.Path(tempfile.mkdtemp())
        vm = self.tart / "vms/gold"
        vm.mkdir(parents=True)
        (vm / "disk.img").write_bytes(b"disk")
        (vm / "config.json").write_bytes(b"{}")
        self.images = pathlib.Path(tempfile.mkdtemp())
        env = mock.patch.dict(os.environ, {"TART_HOME": str(self.tart)})
        env.start()
        self.addCleanup(env.stop)

    def state(self, running):
        return mock.patch.object(image, "tart_state", return_value={"Name": "gold", "Running": running, "Source": "oci-ref"})

    def test_refuses_running_vm(self):
        with self.state(True), self.assertRaises(SystemExit):
            image.record("gold", self.images)

    def test_records_hashes_and_unpinned_key(self):
        with self.state(False):
            path = image.record("gold", self.images)
        rec = json.loads(path.read_text())
        self.assertEqual(rec["image_sha256"], {"disk": hashlib.sha256(b"disk").hexdigest(),
                                               "config": hashlib.sha256(b"{}").hexdigest()})
        self.assertEqual((rec["source"], rec["guest_user"], rec["ssh_host_key_fp"]), ("oci-ref", "admin", None))
        self.assertEqual(image.load(self.images, "gold")[1], rec)

    def test_rerecord_keeps_pin_and_source_while_image_unchanged(self):
        with self.state(False):
            path = image.record("gold", self.images, source="ghcr.io/base@sha256:1")
        image.pin_fp(path, "SHA256:aaa")
        with self.state(False):
            image.record("gold", self.images)
        rec = json.loads(path.read_text())
        self.assertEqual((rec["ssh_host_key_fp"], rec["source"]), ("SHA256:aaa", "ghcr.io/base@sha256:1"))
        (self.tart / "vms/gold/disk.img").write_bytes(b"rebuilt")
        with self.state(False):
            image.record("gold", self.images)
        rec = json.loads(path.read_text())
        self.assertEqual((rec["ssh_host_key_fp"], rec["source"]), (None, "oci-ref"))  # new image: pin again

    def test_pin_first_use_then_enforce(self):
        with self.state(False):
            path = image.record("gold", self.images)
        self.assertEqual(image.pin_fp(path, "SHA256:aaa"), ("SHA256:aaa", "pinned"))
        self.assertEqual(image.pin_fp(path, "SHA256:aaa"), ("SHA256:aaa", "matched"))
        self.assertEqual(image.pin_fp(path, "SHA256:bbb"), ("SHA256:aaa", "mismatch"))
        self.assertEqual(json.loads(path.read_text())["ssh_host_key_fp"], "SHA256:aaa")
        os.utime(path, ns=(0, 0))
        image.pin_fp(path, "SHA256:aaa")
        self.assertEqual(path.stat().st_mtime_ns, 0)  # pinned records are never rewritten

    def test_browser_info_merged_and_kept_while_image_unchanged(self):
        info = self.images / "b.json"
        info.write_text(json.dumps({"node": "/n", "entry": "/e", "entry_sha256": "a" * 64, "engines": {"webkit": "1"}}))
        with self.state(False):
            path = image.record("gold", self.images, browser_info=info)
        self.assertEqual(json.loads(path.read_text())["browser"]["engines"], {"webkit": "1"})
        with self.state(False):
            image.record("gold", self.images)
        self.assertEqual(json.loads(path.read_text())["browser"]["entry"], "/e")
        (self.tart / "vms/gold/disk.img").write_bytes(b"rebuilt")
        with self.state(False):
            image.record("gold", self.images)
        self.assertNotIn("browser", json.loads(path.read_text()))  # new bytes: browser facts must be re-recorded

    def test_browser_info_needs_required_keys(self):
        info = self.images / "b.json"
        info.write_text(json.dumps({"node": "/n"}))
        with self.state(False), self.assertRaises(SystemExit):
            image.record("gold", self.images, browser_info=info)

    def codex_info(self, **over):
        info = self.images / "c.json"
        facts = {"bin": "/opt/homebrew/bin/codex", "version": "0.167.0",
                 "binary": "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex-aarch64-apple-darwin",
                 "binary_sha256": "c" * 64}
        facts.update(over)
        info.write_text(json.dumps({k: v for k, v in facts.items() if v is not None}))
        return info

    def test_codex_info_written_with_browser_info_and_both_kept_while_image_unchanged(self):
        binfo = self.images / "b.json"
        binfo.write_text(json.dumps({"node": "/n", "entry": "/e", "entry_sha256": "a" * 64, "engines": {"webkit": "1"}}))
        with self.state(False):
            path = image.record("gold", self.images, browser_info=binfo, codex_info=self.codex_info())
        rec = json.loads(path.read_text())
        self.assertEqual(rec["codex"]["version"], "0.167.0")
        self.assertEqual(rec["codex"]["binary_sha256"], "c" * 64)
        self.assertEqual(rec["browser"]["entry"], "/e")  # both flags together, both blocks written
        with self.state(False):
            image.record("gold", self.images)  # no flags: a re-record of the same bytes drops nothing
        rec = json.loads(path.read_text())
        self.assertEqual((rec["codex"]["bin"], rec["browser"]["entry"]), ("/opt/homebrew/bin/codex", "/e"))
        with self.state(False):
            image.record("gold", self.images, browser_info=binfo)  # one flag: the other block is kept too
        self.assertEqual(json.loads(path.read_text())["codex"]["version"], "0.167.0")
        with self.state(False):
            image.record("gold", self.images, codex_info=self.codex_info(version="0.168.0"))  # new facts replace
        rec = json.loads(path.read_text())
        self.assertEqual((rec["codex"]["version"], rec["browser"]["entry"]), ("0.168.0", "/e"))
        (self.tart / "vms/gold/disk.img").write_bytes(b"rebuilt")
        with self.state(False):
            image.record("gold", self.images)
        rec = json.loads(path.read_text())
        self.assertNotIn("codex", rec)  # new bytes: codex facts must be re-recorded, like the browser ones
        self.assertNotIn("browser", rec)

    def test_codex_info_needs_required_keys_and_names_the_missing_ones(self):
        info = self.codex_info(version=None, binary_sha256="")
        with self.state(False), self.assertRaises(SystemExit) as e:
            image.record("gold", self.images, codex_info=info)
        self.assertIn("codex info", str(e.exception))
        self.assertIn("'version'", str(e.exception))
        self.assertIn("'binary_sha256'", str(e.exception))
        self.assertNotIn("'bin'", str(e.exception).replace("'binary'", ""))
        self.assertFalse((self.images / "gold.json").exists())  # refused before anything is written

    def test_codex_keys_are_the_ones_check_image_wants(self):
        from cuvm import codex
        self.assertEqual(image.CODEX_KEYS, codex.CODEX_KEYS)

    def test_missing_record_says_how_to_make_one(self):
        with self.assertRaises(SystemExit) as e:
            image.load(self.images, "nope")
        self.assertIn("image record nope", str(e.exception))


class DataTest(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def test_example_record_carries_both_blocks_check_image_accepts(self):
        from cuvm import browser, codex
        rec = json.loads((self.ROOT / "images/example.json").read_text())
        self.assertEqual(sorted(rec["codex"]), sorted(image.CODEX_KEYS))
        self.assertEqual(sorted(rec["browser"]), sorted(image.BROWSER_KEYS))
        codex.check_image({"agent": "codex"}, rec)  # no raise
        browser.check_image({"browser": "webkit"}, rec)

    def test_codex_build_allowlist(self):
        golden = (image.DATA / "allow-imagebuild.txt").read_text().split()
        codex_list = (image.DATA / "allow-imagebuild-codex.txt").read_text().split()
        self.assertEqual(codex_list, golden + ["registry.npmjs.org"])  # the swap host stays first; npm added; no Playwright hosts
        self.assertNotIn("chatgpt.com", codex_list)  # the TUI is never started during the build
        self.assertNotIn("cdn.playwright.dev", codex_list)


class CliTest(unittest.TestCase):
    def test_proxy_allow_and_record_browser_info_reach_image(self):
        from cuvm import __main__ as cli
        with mock.patch.object(image, "build_proxy") as bp:
            cli.main(["image", "proxy", "vm1", "--allow", "x.txt"])
        self.assertEqual(bp.call_args.args, ("vm1", "runs", "x.txt"))
        with mock.patch.object(image, "build_proxy") as bp:
            cli.main(["image", "proxy", "vm1"])
        self.assertEqual(bp.call_args.args[2], str(image.DATA / "allow-imagebuild.txt"))
        with mock.patch.object(image, "record", return_value="p") as rec:
            cli.main(["image", "record", "vm1", "--browser-info", "b.json"])
        self.assertEqual((rec.call_args.kwargs["browser_info"], rec.call_args.kwargs["codex_info"]), ("b.json", None))
        with mock.patch.object(image, "record", return_value="p") as rec:
            cli.main(["image", "record", "vm1", "--browser-info", "b.json", "--codex-info", "c.json"])
        self.assertEqual((rec.call_args.kwargs["browser_info"], rec.call_args.kwargs["codex_info"]), ("b.json", "c.json"))


class BuildProxyTest(unittest.TestCase):
    def test_dummy_token_removed_when_proxy_fails_to_start(self):
        out = pathlib.Path(tempfile.mkdtemp())
        failed = mock.Mock(returncode=1)
        failed.stdout.readline.return_value = ""
        with mock.patch.object(image.subprocess, "run", return_value=mock.Mock(stdout="192.0.2.4\n")), \
                mock.patch.object(image.subprocess, "Popen", return_value=failed):
            with self.assertRaises(SystemExit):
                image.build_proxy("gold", out)
        self.assertFalse((out / "image-build-gold" / "dummy.token").exists())


if __name__ == "__main__":
    unittest.main()

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

    def test_missing_record_says_how_to_make_one(self):
        with self.assertRaises(SystemExit) as e:
            image.load(self.images, "nope")
        self.assertIn("image record nope", str(e.exception))


if __name__ == "__main__":
    unittest.main()

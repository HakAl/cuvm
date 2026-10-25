import json
import pathlib
import tempfile
import unittest

from cuvm import config

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = pathlib.Path(__file__).parent / "fixtures"


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        (self.dir / "inputs").mkdir()
        (self.dir / "inputs/known.txt").write_text("known\n")

    def write(self, d):
        path = self.dir / "config.json"
        path.write_text(json.dumps(d))
        return path

    def base(self):
        return json.loads((ROOT / "experiments/smoke/config.json").read_text())

    def test_smoke_config_valid(self):
        c, h = config.load(ROOT / "experiments/smoke/config.json")
        self.assertEqual(len(h), 64)
        self.assertEqual(c["apps"], ["TextEdit"])
        self.assertTrue(pathlib.Path(c["inputs"]).is_absolute())
        self.assertTrue(c["net_allow"].endswith("cuvm/data/allow-run.txt"))

    def test_paths_resolve_against_config_dir(self):
        d = self.base(); d["net_allow"] = "allow.txt"
        c, _ = config.load(self.write(d))
        self.assertEqual(pathlib.Path(c["inputs"]), self.dir.resolve() / "inputs")
        self.assertEqual(pathlib.Path(c["net_allow"]), self.dir.resolve() / "allow.txt")

    def test_rejects_inputs_without_known_txt(self):
        (self.dir / "inputs/known.txt").unlink()
        with self.assertRaises(config.ConfigError):
            config.load(self.write(self.base()))

    def test_rejects_missing_key(self):
        d = self.base(); del d["apps"]
        with self.assertRaises(config.ConfigError):
            config.load(self.write(d))

    def test_rejects_unknown_key(self):
        d = self.base(); d["inputs_rw"] = True
        with self.assertRaises(config.ConfigError):
            config.load(self.write(d))

    def test_rejects_bad_types(self):
        d = self.base(); d["allow_fullscreen"] = "no"
        with self.assertRaises(config.ConfigError):
            config.load(self.write(d))


if __name__ == "__main__":
    unittest.main()

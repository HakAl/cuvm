import hashlib
import tempfile
import unittest

from cuvm.image import sha256


class Sha256(unittest.TestCase):
    def test_streamed_matches_whole_file_hash_across_chunks(self):
        data = b"x" * ((1 << 24) + 7)
        with tempfile.NamedTemporaryFile() as f:
            f.write(data)
            f.flush()
            self.assertEqual(sha256(f.name), hashlib.sha256(data).hexdigest())


if __name__ == "__main__":
    unittest.main()

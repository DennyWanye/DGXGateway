import json
import pathlib
import sys
import tempfile
import unittest


CUSTOM_NODES = pathlib.Path(__file__).resolve().parents[2] / "custom_nodes"
sys.path.insert(0, str(CUSTOM_NODES))

from dgx_remote_h3.profile_control import ProfileControlError, load_control_token


class ProfileControlTests(unittest.TestCase):
    def test_control_token_must_be_private_regular_and_nonempty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "token"
            path.write_text("opaque-control-token\n")
            path.chmod(0o400)
            self.assertEqual(load_control_token(path), "opaque-control-token")
            path.chmod(0o644)
            with self.assertRaises(ProfileControlError):
                load_control_token(path)


if __name__ == "__main__":
    unittest.main()

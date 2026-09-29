import os
import pathlib
import stat
import sys
import tempfile
import unittest


CUSTOM_NODES = pathlib.Path(__file__).resolve().parents[2] / "custom_nodes"
sys.path.insert(0, str(CUSTOM_NODES))

from dgx_remote_h3.secret import SecretError, read_gateway_token


class SecretTests(unittest.TestCase):
    def _write(self, root: pathlib.Path, value: bytes, mode: int = 0o400) -> pathlib.Path:
        path = root / "token"
        path.write_bytes(value)
        path.chmod(mode)
        return path

    def test_reads_strict_private_token_and_one_terminal_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = self._write(root, b"opaque-gateway-token\n")
            self.assertEqual(read_gateway_token(path), "opaque-gateway-token")

    def test_rejects_missing_nonregular_symlink_and_permissive_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with self.assertRaisesRegex(SecretError, "missing"):
                read_gateway_token(root / "missing")

            target = self._write(root, b"safe-token")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(SecretError, "regular"):
                read_gateway_token(link)

            target.chmod(0o440)
            with self.assertRaisesRegex(SecretError, "0400"):
                read_gateway_token(target)

            directory_secret = root / "directory"
            directory_secret.mkdir()
            directory_secret.chmod(0o400)
            with self.assertRaisesRegex(SecretError, "regular"):
                read_gateway_token(directory_secret)

    def test_rejects_empty_oversize_nul_and_embedded_newline_without_echoing_value(self):
        cases = [
            b"",
            b"x" * 4097,
            b"abc\x00def",
            b"abc\ndef",
            b"not-valid-utf8-\xff",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for index, value in enumerate(cases):
                path = root / f"token-{index}"
                path.write_bytes(value)
                path.chmod(stat.S_IRUSR)
                with self.subTest(index=index), self.assertRaises(SecretError) as raised:
                    read_gateway_token(path)
                printable_value = value.decode("utf-8", errors="ignore")
                if printable_value:
                    self.assertNotIn(printable_value, str(raised.exception))


if __name__ == "__main__":
    unittest.main()

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "create-codex-agent-fixture.py"
SPEC = importlib.util.spec_from_file_location("create_codex_agent_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
PROFILES = MODULE.PROFILES
create_fixture = MODULE.create_fixture


class CodexAgentFixtureTests(unittest.TestCase):
    def test_every_fixture_starts_red_and_reference_patch_turns_it_green(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for profile in PROFILES:
                with self.subTest(profile=profile):
                    fixture = create_fixture(root, profile)
                    repo = fixture["repo"].resolve()
                    self.assertTrue(repo.is_relative_to(root))
                    self.assertTrue(fixture["reference_patch"].resolve().is_relative_to(root))
                    self.assertEqual(
                        fixture["acceptance_command"],
                        ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
                    )
                    initial = subprocess.run(
                        fixture["acceptance_command"], cwd=repo, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    )
                    self.assertNotEqual(initial.returncode, 0, initial.stdout)
                    applied = subprocess.run(
                        ["git", "apply", str(fixture["reference_patch"])], cwd=repo,
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    )
                    self.assertEqual(applied.returncode, 0, applied.stdout)
                    final = subprocess.run(
                        fixture["acceptance_command"], cwd=repo, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    )
                    self.assertEqual(final.returncode, 0, final.stdout)

    def test_fixtures_have_no_network_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for profile in PROFILES:
                fixture = create_fixture(root, profile)
                source = "\n".join(
                    path.read_text()
                    for path in fixture["repo"].rglob("*.py")
                )
                for forbidden in ("urllib", "requests", "socket", "http://", "https://"):
                    self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

"""Credential resolution: environment first, then the macOS Keychain.

The real Keychain is never touched here — `security` is stubbed — so the
suite neither needs a key nor can leak one.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

from .support import load_script


class KeyCase(unittest.TestCase):
    def setUp(self):
        self.based = load_script("based-answers.py")
        self.based.API_KEY_ENV = "TEST_KEY_ENV"
        self.based.KEYCHAIN_SERVICE = "test-service"
        self._env = dict(os.environ)
        os.environ.pop("TEST_KEY_ENV", None)
        self._run = subprocess.run
        self.based.api_key.cache_clear()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        subprocess.run = self._run

    def stub_security(self, *, returncode=0, stdout=""):
        calls = []

        def fake(cmd, *a, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode, stdout, "")

        # based-answers.py does `import subprocess`, so one assignment
        # covers both names — they are the same module attribute.
        subprocess.run = fake
        return calls


class TestApiKey(KeyCase):
    def test_environment_wins_and_keychain_is_not_consulted(self):
        os.environ["TEST_KEY_ENV"] = "sk-from-env"
        calls = self.stub_security(stdout="sk-from-keychain\n")
        self.assertEqual(self.based.api_key(), "sk-from-env")
        self.assertEqual(calls, [], "Keychain was queried despite an env key")

    def test_falls_back_to_keychain(self):
        calls = self.stub_security(stdout="sk-from-keychain\n")
        self.assertEqual(self.based.api_key(), "sk-from-keychain")
        self.assertEqual(
            calls[0],
            ["security", "find-generic-password", "-s", "test-service", "-w"])

    def test_missing_keychain_item_is_none_not_empty_string(self):
        self.stub_security(returncode=44, stdout="")
        self.assertIsNone(self.based.api_key())

    def test_blank_keychain_value_is_none(self):
        """An empty item must not be passed on as a credential."""
        self.stub_security(returncode=0, stdout="\n")
        self.assertIsNone(self.based.api_key())

    def test_security_missing_is_not_fatal(self):
        def boom(*a, **kw):
            raise FileNotFoundError("no security binary")

        subprocess.run = boom
        self.based.subprocess.run = boom
        self.assertIsNone(self.based.api_key())


class TestPiEnv(KeyCase):
    def test_key_is_passed_to_the_subprocess(self):
        os.environ["TEST_KEY_ENV"] = "sk-xyz"
        env = self.based.pi_env("some-slug")
        self.assertEqual(env["TEST_KEY_ENV"], "sk-xyz")

    def test_env_carries_the_slug_and_interpreter(self):
        self.stub_security(returncode=1)
        env = self.based.pi_env("some-slug")
        self.assertEqual(env["ANSWER_SLUG"], "some-slug")
        self.assertEqual(env["BA_PYTHON"], sys.executable)
        self.assertEqual(env["BA_SKILL_DIR"], str(self.based.SKILL_DIR))

    def test_absent_key_is_omitted_rather_than_set_empty(self):
        """An empty OPENROUTER_API_KEY would mask pi's own 'no key' error."""
        self.stub_security(returncode=1)
        self.assertNotIn("TEST_KEY_ENV", self.based.pi_env("s"))


class TestEveryAgentIsAuthenticated(KeyCase):
    """Both the searcher and the checkers must receive credentials.

    The key is read from the Keychain, so it is absent from os.environ and
    is NOT inherited by a subprocess: any session built with a hand-written
    env dict instead of pi_env() runs unauthenticated. That failed silently
    for the checkers — the searcher worked, so the pipeline looked healthy
    while every semantic and coherence check errored out.
    """

    def envs_passed_to_pi(self, fn):
        """Run fn with PiSession stubbed; return each env dict it was given."""
        seen = []

        class FakeSession:
            def __init__(self, **kw):
                seen.append(kw.get("env") or {})

            def prompt(self, *a, **kw):
                return {"settled": True}

            def close(self, *a, **kw):
                pass

        real = self.based.pi_rpc.PiSession
        self.based.pi_rpc.PiSession = FakeSession
        try:
            fn()
        finally:
            self.based.pi_rpc.PiSession = real
        return seen

    def test_checker_session_carries_the_key(self):
        os.environ["TEST_KEY_ENV"] = "sk-abc"
        envs = self.envs_passed_to_pi(
            lambda: self.based.run_checker(
                "rubric", self.based.TokenLedger(None, emit_fn=lambda *a: None),
                agent="coherence"))
        self.assertTrue(envs, "run_checker built no pi session")
        for env in envs:
            self.assertEqual(env.get("TEST_KEY_ENV"), "sk-abc",
                             "checker session was built without credentials")

    def test_search_session_carries_the_key(self):
        os.environ["TEST_KEY_ENV"] = "sk-abc"
        envs = self.envs_passed_to_pi(
            lambda: self.based.open_search_session("some-slug"))
        self.assertTrue(envs)
        for env in envs:
            self.assertEqual(env.get("TEST_KEY_ENV"), "sk-abc")


if __name__ == "__main__":
    unittest.main()

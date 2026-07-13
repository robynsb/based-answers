"""End-to-end over real fixtures: the RP2040 datasheet plus answer YAMLs from
previously asked questions. Exercises indexing, deterministic verification,
answer-context building, and serving through the web app."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .support import SKILL_DIR, load_script

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_NAME = "RP-008371-DS-1-rp2040-datasheet.pdf"

pdf_search = load_script("pdf-search.py")
format_answers = load_script("format-answers.py")
live_server = load_script("live-server.py")


class TestEndToEnd(unittest.TestCase):
    """Shares one temp working directory (and one PDF index) across tests."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls._tmp.name)
        cls._old_cwd = os.getcwd()
        os.chdir(cls.work)

        shutil.copy2(FIXTURES / PDF_NAME, cls.work / PDF_NAME)
        shutil.copytree(FIXTURES / "answers", cls.work / "answers")
        # a draft-round YAML (fails the concatenation check, but renders):
        # its short quotes fit single PDF spans, so it exercises highlights
        cls.draft_yaml = cls.work / "draft-what-is-the-frequency-of-rp2040.yml"
        shutil.copy2(FIXTURES / "draft-what-is-the-frequency-of-rp2040.yml", cls.draft_yaml)
        # Build the indexed-pdfs cache the same way the pipeline does
        pdf_search.load_or_extract(str(cls.work / PDF_NAME))

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._old_cwd)
        cls._tmp.cleanup()

    def yaml_paths(self):
        paths = sorted((self.work / "answers").glob("*.yml"))
        self.assertTrue(paths, "no fixture YAMLs found")
        return paths

    def test_fixture_yamls_pass_deterministic_verification(self):
        for yaml_path in self.yaml_paths():
            with self.subTest(yaml=yaml_path.name):
                result = subprocess.run(
                    [sys.executable, str(SKILL_DIR / "verify-citations.py"),
                     "--pdf-dir", ".", str(yaml_path)],
                    capture_output=True, text=True,
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"verification failed:\n{result.stdout}\n{result.stderr}",
                )

    def test_build_context_default_uses_file_urls(self):
        context = format_answers.build_context(self.work / "answers" / "rp2040-pull-blocking-detection.yml")
        self.assertFalse(context["unable"])
        self.assertIn("<sup>", context["concatenation"])
        self.assertTrue(context["all_references"])
        for ref in context["all_references"]:
            self.assertTrue(ref["pdf_url"].startswith("file:///"), ref["pdf_url"])

    def test_draft_answer_renders_with_highlights(self):
        # what the live answer view shows mid-run, before checks pass
        context = format_answers.build_context(self.draft_yaml)
        self.assertTrue(context["all_references"])
        # quotes short enough to sit inside one PDF text span get bboxes
        self.assertTrue(any(ref["highlights"] for ref in context["all_references"]))

    def test_build_context_pdf_url_override(self):
        context = format_answers.build_context(
            self.work / "answers" / "rp2040-pull-blocking-detection.yml",
            pdf_url_for=lambda name, path: f"/pdf/{Path(path).name}",
        )
        for ref in context["all_references"]:
            self.assertEqual(ref["pdf_url"], f"/pdf/{PDF_NAME}")
        self.assertIn(f'href="/pdf/{PDF_NAME}#page=', context["concatenation"])

    def test_multi_claim_answer_builds_all_claims(self):
        import yaml
        yaml_path = self.work / "answers" / "in-rp2040-can-one-pio-machine-stall-untill-another-tells-it-to-start-10.yml"
        context = format_answers.build_context(yaml_path)
        with open(yaml_path) as f:
            claims = [a for a in yaml.safe_load(f)["answers"] if a.get("claim") and a.get("citations")]
        self.assertGreater(len(claims), 1)
        self.assertEqual(context["concatenation"].count("<sup>"), len(claims))
        self.assertGreater(len(context["all_references"]), 3)

    def test_server_serves_everything(self):
        server = live_server.PipelineServer(
            db_path=str(self.work / "citation-qa.db"),
            max_rounds=5,
            build_context=format_answers.build_context,
        )
        server.register_pdf(self.work / PDF_NAME)
        yaml_path = self.work / "answers" / "rp2040-pull-blocking-detection.yml"
        run_id = server.create_run("RP2040 pull blocking detection?", "rp2040-pull-blocking-detection")

        fragment = server.render_answer_fragment(yaml_path)
        self.assertIn("ref-card", fragment)
        self.assertIn(f"/pdf/{PDF_NAME}", fragment)
        server.emit(run_id, "answer", {"html": fragment})
        server.emit(run_id, "phase", {"phase": "passed", "round": 1})
        server.set_status(run_id, "passed", str(yaml_path))

        client = server.app.test_client()
        index = client.get("/").get_data(as_text=True)
        self.assertIn("RP2040 pull blocking detection?", index)
        self.assertIn("passed", index)

        run_page = client.get(f"/run/{run_id}").get_data(as_text=True)
        self.assertIn("RP2040 pull blocking detection?", run_page)

        pdf_resp = client.get(f"/pdf/{PDF_NAME}")
        self.assertEqual(pdf_resp.status_code, 200)
        self.assertEqual(pdf_resp.data[:5], b"%PDF-")

        events = server.events_after(run_id, 0)
        self.assertEqual([e["event"] for e in events], ["answer", "phase"])


if __name__ == "__main__":
    unittest.main()

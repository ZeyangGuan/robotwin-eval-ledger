import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from robotwin_eval_ledger import Ledger, load_run
from robotwin_eval_ledger.cli import main
from robotwin_eval_ledger.demo import make_demo
from robotwin_eval_ledger.report import write_summary


class CliReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_demo_artifacts_and_exact_denominators(self):
        make_demo(self.root / "demo")
        comparison = json.loads((self.root / "demo/comparison/comparison.json").read_text())
        self.assertEqual(comparison["same_observed_policy_set"], "no")
        self.assertEqual(comparison["metadata_matched_pairs"], 80)
        self.assertEqual(comparison["paired_left_success"], 80)
        self.assertEqual(comparison["paired_right_success"], 64)
        for name, successes, completed, rejected in [("run-a", 80, 100, 0), ("run-b", 64, 80, 20)]:
            report = self.root / "demo" / f"{name}-report"
            summary = json.loads((report / "summary.json").read_text())
            self.assertEqual(summary["latest_attempts"]["policy_success"], successes)
            self.assertEqual(summary["latest_attempts"]["policy_completed"], completed)
            self.assertEqual(summary["latest_attempts"]["expert_rejected"], rejected)
            self.assertEqual(summary["official"][0]["denominator"], completed)
            with (report / "attempts.csv").open(newline="") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 100)
            html = (report / "report.html").read_text()
            self.assertIn("SYNTHETIC DATA", html)
            self.assertNotIn("<script", html)
            self.assertNotIn("<link", html)
        recovery = json.loads((self.root / "demo/recovery-report/summary.json").read_text())
        self.assertEqual(recovery["duplicate_events"], 1)
        self.assertEqual(recovery["retry_seeds"], 1)
        self.assertEqual(recovery["all_attempts"]["infra_error"], 1)
        self.assertEqual(recovery["latest_attempts"]["infra_error"], 0)
        self.assertEqual(recovery["latest_attempts"]["unfinished"], 1)
        self.assertEqual(recovery["official"], [])
        self.assertTrue(any("truncated" in x for x in recovery["warnings"]))

    def test_cli_summarize_compare_and_parse_failure_exit(self):
        make_demo(self.root / "demo")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["summarize", str(self.root / "demo/run-b"), "--out", str(self.root / "summary")]), 0)
            self.assertEqual(main(["compare", str(self.root / "demo/run-a"), str(self.root / "demo/run-b"), "--out", str(self.root / "comparison")]), 0)
        bad = self.root / "bad.jsonl"
        bad.write_text("this is not JSON\n")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["summarize", str(bad), "--out", str(self.root / "invalid-report")]), 2)
        self.assertIn("malformed JSON", stderr.getvalue())
        self.assertFalse((self.root / "invalid-report").exists())

    def test_html_escaping_and_csv_formula_safety(self):
        log = self.root / "log.jsonl"
        task = '=HYPERLINK("https://invalid.example")'
        with Ledger(log, run_id='<script>alert("x")</script>', worker_id="0", config={}) as writer:
            episode = writer.start(task, 0)
            episode.infra("setup", '<img src=x onerror="alert(1)">')
        write_summary(load_run(log), self.root / "report")
        html = (self.root / "report/report.html").read_text()
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img src=x', html)
        self.assertIn('&lt;script&gt;', html)
        with (self.root / "report/attempts.csv").open(newline="") as f:
            row = next(csv.DictReader(f))
        self.assertEqual(row["task"], "'" + task)
        self.assertEqual(row["variant"], "unknown")

    def test_official_only_report_is_renderable(self):
        with Ledger(self.root / "log.jsonl", run_id="coordinator", worker_id="0", config={}) as w:
            w.official("task", 2, 3, label="reported upstream denominator")
        result = write_summary(load_run(self.root / "log.jsonl"), self.root / "report")
        self.assertIsNone(result["latest_attempts"]["observed_policy_rate"])
        self.assertTrue((self.root / "report/report.html").is_file())

    def test_demo_does_not_overwrite_existing_content(self):
        folder = self.root / "demo"
        folder.mkdir()
        sentinel = folder / "important.txt"
        sentinel.write_text("preserve me")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["demo", "--out", str(folder)]), 2)
        self.assertEqual(sentinel.read_text(), "preserve me")

    def test_no_output_claims_actual_benchmark_performance(self):
        make_demo(self.root / "demo")
        for html in (self.root / "demo").glob("*/report.html"):
            self.assertIn("SYNTHETIC DATA · NOT BENCHMARK RESULTS", html.read_text())


if __name__ == "__main__":
    unittest.main()

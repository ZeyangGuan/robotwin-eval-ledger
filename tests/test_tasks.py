"""Per-task accounting stays diagnostic, without inventing benchmark scores."""
import csv
import html
import json
from pathlib import Path
import tempfile
import unittest

from robotwin_eval_ledger import Ledger, load_run, summarize
from robotwin_eval_ledger.demo import make_multitask_demo
from robotwin_eval_ledger.report import write_summary


class TaskSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def writer(self, worker="0"):
        return Ledger(self.root / "run" / f"worker-{worker}.jsonl", run_id="synthetic-tasks",
                      worker_id=worker, config={}, synthetic=True)

    def complete(self, writer, task, seed, success=True, attempt=0):
        a = writer.start(task, seed, attempt)
        a.expert("accepted")
        a.policy_start()
        a.policy(success)

    def test_multitask_demo_preserves_zero_completion_tasks(self):
        result = make_multitask_demo(self.root)
        tasks = {t["task"]: t for t in result["tasks"]}
        self.assertEqual(list(tasks), sorted(tasks))
        self.assertEqual(len(tasks), 5)
        expected = {"synthetic_success": (2, 2, 1.0), "synthetic_fail": (0, 2, 0.0),
                    "synthetic_rejected": (0, 0, None), "synthetic_infra": (0, 0, None),
                    "synthetic_unfinished": (0, 0, None)}
        for name, (success, completed, rate) in expected.items():
            for scope in ("all_attempts", "latest_attempts"):
                counts = tasks[name][scope]
                self.assertEqual((counts["policy_success"], counts["policy_completed"],
                                  counts["observed_policy_rate"]), (success, completed, rate))
                self.assertGreater(counts["attempts"], 0)
        self.assertEqual(tasks["synthetic_rejected"]["latest_attempts"]["expert_rejected"], 2)
        self.assertEqual(tasks["synthetic_infra"]["latest_attempts"]["infra_error"], 1)
        self.assertEqual(tasks["synthetic_unfinished"]["latest_attempts"]["unfinished"], 1)
        for scope in ("all_attempts", "latest_attempts"):
            for key, value in result[scope].items():
                if key != "observed_policy_rate":
                    self.assertEqual(sum(t[scope][key] for t in tasks.values()), value)
        self.assertEqual(result["latest_attempts"]["observed_policy_rate"], 0.5)

    def test_global_rate_is_episode_weighted_not_task_macro(self):
        with self.writer() as w:
            self.complete(w, "success", 0)
            self.complete(w, "success", 1)
            self.complete(w, "fail", 0, success=False)
            w.official("success", 7, 10, label="fabricated upstream scheduled episodes")
        result = summarize(load_run(self.root / "run"))
        self.assertEqual(result["latest_attempts"]["observed_policy_rate"], 2 / 3)
        macro = sum(t["latest_attempts"]["observed_policy_rate"] for t in result["tasks"]) / 2
        self.assertEqual(macro, 0.5)
        self.assertNotEqual(macro, result["latest_attempts"]["observed_policy_rate"])
        tasks = {t["task"]: t for t in result["tasks"]}
        self.assertEqual(tasks["success"]["latest_attempts"]["policy_completed"], 2)
        self.assertEqual(tasks["success"]["official"]["denominator"], 10)
        self.assertIsNone(tasks["fail"]["official"])

    def test_official_only_task_keeps_explicit_zero_and_unknown_observations(self):
        with self.writer() as w:
            w.official("only-official", 0, 0, label="explicit upstream zero denominator")
        result = write_summary(load_run(self.root / "run"), self.root / "report")
        task = result["tasks"][0]
        self.assertEqual(task["task"], "only-official")
        self.assertEqual(task["official"], result["official"][0])
        self.assertEqual(task["official"]["denominator"], 0)
        self.assertEqual((task["retry_seeds"], task["superseded_attempts"]), (0, 0))
        for scope in ("all_attempts", "latest_attempts"):
            self.assertEqual(task[scope]["attempts"], 0)
            self.assertIsNone(task[scope]["observed_policy_rate"])
        report = (self.root / "report/report.html").read_text()
        self.assertIn("only-official", report)
        self.assertNotIn("0.0%", report)

    def test_retries_are_per_task_and_latest_can_be_unfinished(self):
        with self.writer("older") as w:
            self.complete(w, "alpha", 0)
            w.start("alpha", 1).infra("setup", "fabricated error")
            self.complete(w, "beta", 0, success=False)
        with self.writer("newer") as w:
            w.start("alpha", 0, 2).policy_start()
            self.complete(w, "alpha", 1, attempt=3)
        result = summarize(load_run(self.root / "run"))
        alpha, beta = result["tasks"]
        self.assertEqual((alpha["retry_seeds"], alpha["superseded_attempts"]), (2, 2))
        self.assertEqual((beta["retry_seeds"], beta["superseded_attempts"]), (0, 0))
        self.assertEqual(alpha["all_attempts"]["attempts"], 4)
        self.assertEqual(alpha["all_attempts"]["policy_completed"], 2)
        self.assertEqual(alpha["all_attempts"]["infra_error"], 1)
        self.assertEqual(alpha["latest_attempts"]["attempts"], 2)
        self.assertEqual(alpha["latest_attempts"]["policy_completed"], 1)
        self.assertEqual(alpha["latest_attempts"]["unfinished"], 1)
        self.assertEqual(alpha["latest_attempts"]["infra_error"], 0)
        self.assertEqual(result["retry_seeds"], sum(t["retry_seeds"] for t in result["tasks"]))
        self.assertEqual(result["superseded_attempts"], sum(t["superseded_attempts"] for t in result["tasks"]))

    def test_latest_per_task_zero_denominator_does_not_fall_back_to_old_success(self):
        with self.writer() as w:
            self.complete(w, "retry", 0)
            self.complete(w, "retry", 0, success=False, attempt=1)
            w.start("retry", 0, 2).policy_start()
        task = summarize(load_run(self.root / "run"))["tasks"][0]
        self.assertEqual(task["all_attempts"]["observed_policy_rate"], 0.5)
        self.assertIsNone(task["latest_attempts"]["observed_policy_rate"])
        self.assertEqual(task["retry_seeds"], 1)
        self.assertEqual(task["superseded_attempts"], 2)

    def test_csv_and_json_task_views_share_all_counts(self):
        make_multitask_demo(self.root)
        result = json.loads((self.root / "multitask-report/summary.json").read_text())
        with (self.root / "multitask-report/tasks.csv").open(newline="") as f:
            rows = {(r["task"], r["scope"]): r for r in csv.DictReader(f)}
        self.assertEqual(len(rows), len(result["tasks"]) * 2)
        for task in result["tasks"]:
            for scope in ("all_attempts", "latest_attempts"):
                row = rows[(task["task"], scope)]
                for key, value in task[scope].items():
                    self.assertEqual(row[key], "unknown" if value is None else str(value))
                for key in ("retry_seeds", "superseded_attempts"):
                    self.assertEqual(row[key], str(task[key]))

    def test_task_table_escaping_and_csv_formula_safety(self):
        names = ['<script>alert("task")</script>', '=HYPERLINK("https://invalid.example")']
        with self.writer() as w:
            for name in names:
                w.start(name, 0).expert("rejected")
        result = write_summary(load_run(self.root / "run"), self.root / "report")
        report = (self.root / "report/report.html").read_text()
        section = report.split("<h2>Per-task diagnostics</h2>")[1].split("</section>")[0]
        for name in names:
            self.assertEqual(section.count(html.escape(name, quote=True)), 2)
        self.assertNotIn("<script>", report)
        self.assertIn("<td>unknown</td>", section)
        self.assertNotIn("0.0%", section)
        with (self.root / "report/tasks.csv").open(newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["task"] for r in rows], [names[0], names[0], "'" + names[1], "'" + names[1]])
        self.assertEqual([t["task"] for t in result["tasks"]], names)

    def test_empty_run_exports_task_csv_header_and_no_invented_task(self):
        with self.writer():
            pass
        result = write_summary(load_run(self.root / "run"), self.root / "report")
        self.assertEqual(result["tasks"], [])
        with (self.root / "report/tasks.csv").open(newline="") as f:
            reader = csv.DictReader(f)
            self.assertIn("observed_policy_rate", reader.fieldnames)
            self.assertIn("policy_completed", reader.fieldnames)
            self.assertEqual(list(reader), [])


if __name__ == "__main__":
    unittest.main()

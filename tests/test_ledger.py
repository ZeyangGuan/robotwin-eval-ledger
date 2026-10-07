"""Conservative denominator, retry, provenance, and log-integrity tests."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from robotwin_eval_ledger import Ledger, LedgerError, compare, fingerprint, load_run, summarize


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def writer(self, name="run", worker="worker", **kwargs):
        return Ledger(
            self.root / name / (worker + ".jsonl"),
            run_id=name,
            worker_id=worker,
            config=kwargs.pop("config", {"policy": "test"}),
            environment=kwargs.pop("environment", {"simulator": "test-v1"}),
            synthetic=kwargs.pop("synthetic", True),
            **kwargs,
        )

    def case(self, writer, seed=0, attempt=0, outcome="success", *, metadata=True,
             task="stack", variant="clean", expert="accepted"):
        case = writer.start(task, seed, attempt, variant=variant)
        if metadata:
            case.metadata(scene_fingerprint=fingerprint({"scene": seed}),
                          instruction_fingerprint=fingerprint({"instruction": task}))
        if outcome == "unfinished":
            return case
        case.expert("rejected" if outcome == "rejected" else expert)
        if outcome == "rejected":
            return case
        if outcome == "infra":
            case.infra("reset", "fixture reset failed")
            return case
        case.policy_start()
        case.policy(outcome == "success")
        return case

    def read(self, name="run"):
        return load_run(self.root / name)

    def records(self, name="run", worker="worker"):
        path = self.root / name / (worker + ".jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def raw(self, records, filename="raw.jsonl", *, terminated=True):
        path = self.root / filename
        text = "\n".join(json.dumps(record, allow_nan=True) for record in records)
        path.write_text(text + ("\n" if terminated else ""), encoding="utf-8")
        return path

    def one_complete(self, name="run", **kwargs):
        with self.writer(name, **kwargs) as writer:
            self.case(writer)
        return self.read(name)

    def test_explicit_denominators_do_not_turn_expert_rejection_into_failure(self):
        with self.writer("baseline") as writer:
            for seed in range(100):
                self.case(writer, seed, outcome="success" if seed < 80 else "fail")
            writer.official("stack", 80, 100, label="all scheduled scenes")
        with self.writer("filtered") as writer:
            for seed in range(100):
                outcome = "success" if seed < 64 else "fail" if seed < 80 else "rejected"
                self.case(writer, seed, outcome=outcome)
            writer.official("stack", 64, 80, label="expert-accepted scenes")
        baseline, filtered = self.read("baseline"), self.read("filtered")
        a, b = summarize(baseline), summarize(filtered)
        self.assertEqual(a["latest_attempts"]["observed_policy_rate"], 0.8)
        self.assertEqual(b["latest_attempts"]["observed_policy_rate"], 0.8)
        self.assertEqual(b["latest_attempts"]["attempts"], 100)
        self.assertEqual(b["latest_attempts"]["policy_completed"], 80)
        self.assertEqual(b["latest_attempts"]["policy_fail"], 16)
        self.assertEqual(b["latest_attempts"]["expert_rejected"], 20)
        self.assertEqual(a["official"][0]["denominator"], 100)
        self.assertEqual(b["official"][0]["denominator"], 80)
        self.assertEqual(b["official"][0]["successes"], 64)
        comparison = compare(baseline, filtered)
        self.assertFalse(comparison["policy_seed_sets_equal"])
        self.assertEqual(comparison["same_observed_policy_set"], "no")
        self.assertEqual(comparison["shared_policy_seeds"], 80)

    def test_no_official_denominator_is_invented(self):
        report = summarize(self.one_complete())
        self.assertEqual(report["official"], [])

    def test_retry_selects_latest_declared_even_when_unfinished(self):
        with self.writer() as writer:
            self.case(writer, attempt=0)
            self.case(writer, attempt=1, outcome="unfinished")
        run = self.read()
        report = summarize(run)
        self.assertEqual(len(run.attempts), 2)
        self.assertEqual(run.latest()[("stack", 0)].attempt, 1)
        self.assertEqual(report["all_attempts"]["policy_success"], 1)
        self.assertEqual(report["latest_attempts"]["policy_success"], 0)
        self.assertEqual(report["latest_attempts"]["unfinished"], 1)
        self.assertIsNone(report["latest_attempts"]["observed_policy_rate"])
        self.assertEqual(report["retry_seeds"], 1)
        self.assertEqual(report["superseded_attempts"], 1)
        reference = self.one_complete("reference")
        comparison = compare(run, reference)
        self.assertEqual(comparison["metadata_matched_pairs"], 0)
        self.assertEqual(comparison["rows"][0]["left_attempt"], 1)
        self.assertEqual(comparison["rows"][0]["left_outcome"], "unfinished")

    def test_latest_attempt_uses_numeric_attempt_not_worker_or_file_order(self):
        with self.writer(worker="a_newer") as writer:
            self.case(writer, attempt=10, outcome="fail")
        with self.writer(worker="z_older") as writer:
            self.case(writer, attempt=2)
        run = self.read()
        self.assertEqual(run.latest()[("stack", 0)].attempt, 10)
        self.assertEqual(summarize(run)["latest_attempts"]["policy_fail"], 1)

    def test_merge_worker_logs_without_double_counting(self):
        with self.writer(worker="a") as writer:
            self.case(writer, seed=0)
        with self.writer(worker="b") as writer:
            self.case(writer, seed=1, outcome="fail")
        report = summarize(self.read())
        self.assertEqual(report["workers"], 2)
        self.assertEqual(report["files"], 2)
        self.assertEqual(report["latest_attempts"]["attempts"], 2)
        self.assertEqual(report["latest_attempts"]["observed_policy_rate"], 0.5)

    def test_same_task_seed_attempt_cannot_belong_to_two_workers(self):
        for worker in ("a", "b"):
            with self.writer(worker=worker) as writer:
                self.case(writer)
        with self.assertRaisesRegex(LedgerError, "duplicate attempt"):
            self.read()

    def test_worker_id_collision_fails_closed(self):
        with self.writer() as writer:
            self.case(writer)
        records = self.records()
        for event in records:
            event["event_id"] += "-different-worker-process"
        second = self.raw(records)
        with self.assertRaisesRegex(LedgerError, "duplicate seq"):
            load_run([self.root / "run", second])

    def test_official_only_worker_is_allowed(self):
        with self.writer(worker="summary") as writer:
            writer.official("stack", 80, 100, label="published official denominator")
        run = self.read()
        report = summarize(run)
        self.assertEqual(report["latest_attempts"]["attempts"], 0)
        self.assertIsNone(report["latest_attempts"]["observed_policy_rate"])
        self.assertEqual(report["official"][0]["denominator"], 100)
        with self.writer(worker="rollouts") as writer:
            self.case(writer)
        report = summarize(self.read())
        self.assertEqual(report["workers"], 2)
        self.assertEqual(report["latest_attempts"]["attempts"], 1)

    def test_zero_official_denominator_remains_explicit(self):
        with self.writer() as writer:
            writer.official("stack", 0, 0, label="no accepted scenes")
        self.assertEqual(summarize(self.read())["official"][0]["denominator"], 0)

    def test_exact_duplicate_events_are_deduplicated(self):
        original = self.one_complete()
        duplicate = self.raw(self.records())
        run = load_run([self.root / "run", duplicate])
        self.assertEqual(len(run.attempts), 1)
        self.assertEqual(run.duplicate_events, len(self.records()))
        self.assertEqual(summarize(run)["latest_attempts"], summarize(original)["latest_attempts"])

    def test_same_event_id_with_changed_payload_fails(self):
        self.one_complete()
        records = self.records()
        records[0]["config_fingerprint"] = "sha256:changed"
        conflicting = self.raw(records)
        with self.assertRaisesRegex(LedgerError, "conflicting duplicate"):
            load_run([self.root / "run", conflicting])

    def test_duplicate_ids_require_identical_json_types(self):
        # Python considers True == 1 and 1 == 1.0, but they are distinct evidence.
        for first, second in ((True, 1), (1, 1.0), ({"x": True}, {"x": 1})):
            with self.subTest(first=first, second=second):
                run_name = "types_" + str(len(list(self.root.iterdir())))
                with self.writer(run_name) as writer:
                    self.case(writer, variant=first)
                records = self.records(run_name)
                next(e for e in records if e["kind"] == "attempt_start")["variant"] = second
                conflicting = self.raw(records, filename=run_name + "-copy.jsonl")
                with self.assertRaisesRegex(LedgerError, "conflicting duplicate"):
                    load_run([self.root / run_name, conflicting])

    def test_unterminated_truncated_last_line_is_reported_and_tolerated(self):
        writer = self.writer()
        self.case(writer, outcome="unfinished")
        writer.close()
        with writer.path.open("ab") as stream:
            stream.write(b'{"schema":1,"kind":"policy_res')
        run = self.read()
        self.assertEqual(summarize(run)["latest_attempts"]["unfinished"], 1)
        self.assertTrue(any("truncated final line" in warning for warning in run.warnings))
        self.assertTrue(any("no worker_end" in warning for warning in run.warnings))

    def test_truncated_multibyte_final_line_is_reported(self):
        self.one_complete()
        path = self.root / "run" / "worker.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"reason":"\xe4\xb8')
        run = self.read()
        self.assertEqual(len(run.attempts), 1)
        self.assertTrue(any("truncated final line" in warning for warning in run.warnings))

    def test_malformed_newline_terminated_last_line_fails(self):
        self.one_complete()
        path = self.root / "run" / "worker.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"incomplete":\n')
        with self.assertRaisesRegex(LedgerError, "malformed JSON"):
            self.read()

    def test_malformed_middle_line_fails(self):
        self.one_complete()
        path = self.root / "run" / "worker.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(lines[0] + b'{broken}\n' + b"".join(lines[1:]))
        with self.assertRaisesRegex(LedgerError, "malformed JSON"):
            self.read()

    def test_valid_unterminated_last_event_is_not_discarded(self):
        self.one_complete()
        path = self.raw(self.records(), terminated=False)
        run = load_run(path)
        self.assertTrue(run.workers["worker"]["ended"])
        self.assertEqual(run.warnings, [])

    def test_schema_invalid_unterminated_event_is_not_tolerated_as_truncated(self):
        self.one_complete()
        path = self.root / "run" / "worker.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"schema":2}')
        with self.assertRaises(LedgerError):
            self.read()

    def test_duplicate_json_keys_and_nonfinite_numbers_fail(self):
        self.one_complete()
        header = self.records()[0]
        for index, suffix in enumerate(('"schema":1', '"extra":NaN', '"extra":Infinity')):
            with self.subTest(suffix=suffix):
                path = self.root / ("invalid-" + str(index) + ".jsonl")
                path.write_text(json.dumps(header)[:-1] + "," + suffix + "}\n")
                with self.assertRaises(LedgerError):
                    load_run(path)

    def test_invalid_json_enum_types_raise_ledger_error(self):
        self.one_complete()
        original = self.records()
        for kind, field in (("worker_start", "kind"), ("expert_result", "outcome"),
                            ("policy_result", "outcome")):
            for value in ([], {}):
                with self.subTest(kind=kind, field=field, value=value):
                    records = copy.deepcopy(original)
                    next(e for e in records if e["kind"] == kind)[field] = value
                    with self.assertRaises(LedgerError):
                        load_run(self.raw(records))

    def test_missing_pairing_metadata_is_unknown(self):
        with self.writer("a") as writer:
            self.case(writer, metadata=False)
        b = self.one_complete("b")
        result = compare(self.read("a"), b)
        self.assertTrue(result["policy_seed_sets_equal"])
        self.assertEqual(result["pairing_unknown"], 1)
        self.assertEqual(result["metadata_matched_pairs"], 0)
        self.assertEqual(result["same_observed_policy_set"], "unknown (missing metadata)")
        self.assertIn("scene_fingerprint", result["rows"][0]["reason"])
        self.assertIn("instruction_fingerprint", result["rows"][0]["reason"])

    def test_mismatched_environment_precedes_missing_metadata(self):
        with self.writer("a", environment={"simulator": "a"}) as writer:
            self.case(writer, metadata=False)
        b = self.one_complete("b", environment={"simulator": "b"})
        result = compare(self.read("a"), b)
        self.assertEqual(result["pairing_mismatch"], 1)
        self.assertEqual(result["pairing_unknown"], 0)
        self.assertEqual(result["same_observed_policy_set"], "no")
        self.assertIn("environment_fingerprint", result["rows"][0]["reason"])

    def test_each_pairing_field_is_required_and_must_match(self):
        fields = ("environment_fingerprint", "variant", "scene_fingerprint", "instruction_fingerprint")
        a = self.one_complete("a")
        original = self.one_complete("b")
        for field in fields:
            for replacement, pairing in ((None, "unknown"), ("different", "mismatch")):
                with self.subTest(field=field, replacement=replacement):
                    b = copy.deepcopy(original)
                    setattr(b.attempts[0], field, replacement)
                    result = compare(a, b)
                    self.assertEqual(result["rows"][0]["pairing"], pairing)
                    self.assertIn(field, result["rows"][0]["reason"])
                    self.assertEqual(result["metadata_matched_pairs"], 0)

    def test_matching_metadata_pairs_different_policy_configs_and_outcomes(self):
        a = self.one_complete("a", config={"policy": "left"})
        with self.writer("b", config={"policy": "right"}) as writer:
            self.case(writer, outcome="fail")
        result = compare(a, self.read("b"))
        self.assertEqual(result["same_observed_policy_set"], "yes (declared metadata)")
        self.assertEqual(result["metadata_matched_pairs"], 1)
        self.assertEqual(result["paired_left_success"], 1)
        self.assertEqual(result["paired_right_success"], 0)
        self.assertIn("not a simulator determinism guarantee", result["rows"][0]["reason"])

    def test_zero_policy_outcomes_do_not_claim_equivalence(self):
        for name in ("a", "b"):
            with self.writer(name) as writer:
                self.case(writer, outcome="rejected")
        result = compare(self.read("a"), self.read("b"))
        self.assertEqual(result["same_observed_policy_set"], "unknown (no policy outcomes)")
        self.assertEqual(result["metadata_matched_pairs"], 0)

    def test_latest_seed_identity_includes_task_and_variant_groups_use_latest(self):
        with self.writer() as writer:
            self.case(writer, seed=0, task="stack", attempt=0, variant="clean")
            self.case(writer, seed=0, task="stack", attempt=1, variant="random", outcome="fail")
            self.case(writer, seed=0, task="place", variant="clean")
        report = summarize(self.read())
        self.assertEqual(report["latest_attempts"]["attempts"], 2)
        self.assertEqual(report["all_attempts"]["attempts"], 3)
        self.assertEqual(report["retry_seeds"], 1)
        groups = {(group["task"], group["variant"]): group for group in report["variants"]}
        self.assertEqual(set(groups), {("stack", "random"), ("place", "clean")})
        self.assertEqual(groups[("stack", "random")]["policy_fail"], 1)
        self.assertEqual(groups[("place", "clean")]["policy_success"], 1)

    def test_absent_attempt_is_reported(self):
        a = self.one_complete("a")
        with self.writer("b") as writer:
            self.case(writer, seed=9)
        result = compare(a, self.read("b"))
        self.assertEqual(result["left_only_attempted"], 1)
        self.assertEqual(result["right_only_attempted"], 1)
        self.assertEqual(result["shared_attempted_seeds"], 0)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["right_outcome"], "absent")

    def test_infrastructure_error_is_not_policy_failure(self):
        with self.writer() as writer:
            self.case(writer, outcome="infra")
        counts = summarize(self.read())["latest_attempts"]
        self.assertEqual(counts["infra_error"], 1)
        self.assertEqual(counts["policy_fail"], 0)
        self.assertEqual(counts["policy_completed"], 0)
        self.assertIsNone(counts["observed_policy_rate"])

    def test_expert_skipped_is_distinct_from_unknown(self):
        with self.writer() as writer:
            self.case(writer, expert="skipped")
            case = writer.start("stack", 1)
            case.policy_start()
            case.policy(False)
        counts = summarize(self.read())["latest_attempts"]
        self.assertEqual(counts["expert_skipped"], 1)
        self.assertEqual(counts["expert_unknown"], 1)
        self.assertEqual(counts["expert_accepted"], 0)
        self.assertEqual(counts["policy_completed"], 2)

    def test_worker_error_preserves_unfinished_attempt_and_reason(self):
        with self.writer() as writer:
            self.case(writer, outcome="unfinished")
            writer.emit("worker_error", stage="process", reason="executor stopped")
        run = self.read()
        self.assertEqual(summarize(run)["worker_errors"], 1)
        self.assertEqual(summarize(run)["latest_attempts"]["unfinished"], 1)
        self.assertEqual(run.worker_errors[0]["reason"], "executor stopped")

    def test_invalid_lifecycle_transitions_fail_closed(self):
        transitions = {
            "policy_result_without_start": lambda w, a: a.policy(True),
            "repeated_expert": lambda w, a: (a.expert("accepted"), a.expert("accepted")),
            "expert_after_policy_start": lambda w, a: (a.policy_start(), a.expert("accepted")),
            "repeated_policy_start": lambda w, a: (a.policy_start(), a.policy_start()),
            "policy_after_rejection": lambda w, a: (a.expert("rejected"), a.policy_start()),
            "event_after_infra": lambda w, a: (a.infra("reset", "failed"), a.policy_start()),
            "event_after_success": lambda w, a: (a.policy_start(), a.policy(True), a.metadata(variant="late")),
            "duplicate_attempt": lambda w, a: w.start("stack", 0),
            "conflicting_metadata": lambda w, a: (a.metadata(variant="a"), a.metadata(variant="b")),
        }
        for name, transition in transitions.items():
            with self.subTest(name=name):
                # The reader is the final validator even for handcrafted logs.
                writer = self.writer(name)
                case = writer.start("stack", 0)
                try:
                    transition(writer, case)
                    writer.finish()
                    with self.assertRaises(LedgerError):
                        self.read(name)
                except LedgerError:
                    # Early writer validation is also a valid fail-closed result.
                    pass
                finally:
                    if not writer.ended:
                        writer.close()

    def test_sequence_gaps_and_events_after_end_fail(self):
        self.one_complete()
        records = self.records()
        with self.assertRaisesRegex(LedgerError, "sequence gap"):
            load_run(self.raw(records[:2] + records[3:]))
        records.append(dict(records[-1], event_id="after-end", seq=records[-1]["seq"] + 1))
        with self.assertRaisesRegex(LedgerError, "events after worker_end"):
            load_run(self.raw(records))

    def test_missing_start_and_repeated_official_summary_fail(self):
        with self.writer() as writer:
            writer.official("stack", 1, 2, label="test")
            writer.official("stack", 1, 2, label="test")
        with self.assertRaisesRegex(LedgerError, "repeated official"):
            self.read()
        with self.writer("missing") as writer:
            writer.emit("policy_start", task="stack", seed=0, attempt=0)
        with self.assertRaisesRegex(LedgerError, "missing attempt_start"):
            self.read("missing")

    def test_mixed_runs_and_synthetic_status_fail(self):
        self.one_complete("a")
        self.one_complete("b")
        with self.assertRaisesRegex(LedgerError, "mixed run_id"):
            load_run([self.root / "a", self.root / "b"])
        with self.writer("mixed", "a", synthetic=True) as writer:
            self.case(writer)
        with self.writer("mixed", "b", synthetic=False) as writer:
            self.case(writer, seed=1)
        with self.assertRaisesRegex(LedgerError, "mixed synthetic"):
            self.read("mixed")

    def test_mixed_configuration_is_preserved_and_warned(self):
        for seed, worker in enumerate(("a", "b")):
            with self.writer(worker=worker, config={"policy": worker}) as writer:
                self.case(writer, seed=seed)
        run = self.read()
        self.assertEqual(len(run.attempts), 2)
        self.assertTrue(any("Multiple config fingerprints" in warning for warning in run.warnings))

    def test_existing_log_is_never_overwritten(self):
        self.one_complete()
        path = self.root / "run" / "worker.jsonl"
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.writer()
        self.assertEqual(path.read_bytes(), original)

    def test_exception_context_does_not_claim_worker_completion(self):
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            with self.writer() as writer:
                self.case(writer, outcome="unfinished")
                raise RuntimeError("simulated crash")
        report = summarize(self.read())
        self.assertEqual(report["workers_without_end"], 1)
        self.assertEqual(report["latest_attempts"]["unfinished"], 1)

    def test_invalid_official_counts_and_label_are_rejected(self):
        invalid = ((True, 1, "test"), (1, True, "test"), (2, 1, "test"),
                   (-1, 1, "test"), (0, -1, "test"), (0, 1, ""), (0, 1, " "))
        with self.writer() as writer:
            for successes, denominator, label in invalid:
                with self.subTest(values=(successes, denominator, label)):
                    with self.assertRaises(LedgerError):
                        writer.official("stack", successes, denominator, label=label)

    def test_policy_success_must_be_bool(self):
        with self.writer() as writer:
            case = writer.start("stack", 0)
            for value in (1, 0, "success", None):
                with self.subTest(value=value):
                    with self.assertRaises(LedgerError):
                        case.policy(value)

    def test_fingerprint_is_order_invariant_and_type_sensitive(self):
        self.assertEqual(fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1}))
        self.assertNotEqual(fingerprint(True), fingerprint(1))
        self.assertNotEqual(fingerprint(1), fingerprint(1.0))

    def test_empty_inputs_fail_without_inventing_run(self):
        with self.assertRaises(LedgerError):
            load_run(self.root)
        empty = self.root / "empty.jsonl"
        empty.touch()
        with self.assertRaisesRegex(LedgerError, "no valid events"):
            load_run(empty)


if __name__ == "__main__":
    unittest.main()

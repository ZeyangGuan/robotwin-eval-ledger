"""Append-only, per-worker records with conservative accounting."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class LedgerError(ValueError):
    """Invalid or ambiguous evidence; no partial report should be trusted."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(value: Any) -> str:
    """Hash explicit JSON-compatible evidence, not a filename or inferred state."""
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class Ledger:
    """One writer per NEW file. On restart use a new worker ID and attempt number.

    Each event is flushed; durable=True also fsyncs. No global lock, simulator,
    exception swallowing, or automatic success/failure classification.
    Config is hashed locally and is not included verbatim in the log.
    """

    def __init__(self, path, *, run_id: str, worker_id: str, config: Any,
                 environment: Any = None, synthetic: bool = False, durable: bool = False):
        if not isinstance(run_id, str) or not run_id or not isinstance(worker_id, str) or not worker_id:
            raise LedgerError("run_id and worker_id must be nonempty strings")
        config_fp = fingerprint(config)
        env_fp = None if environment is None else fingerprint(environment)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("x", encoding="utf-8")
        self.run_id, self.worker_id = run_id, worker_id
        self.seq, self.durable, self.ended = 0, durable, False
        self.emit("worker_start", config_fingerprint=config_fp,
                  environment_fingerprint=env_fp, synthetic=synthetic)

    def emit(self, kind: str, **fields) -> dict:
        if self.ended:
            raise LedgerError("worker has ended")
        if {"schema", "event_id", "run_id", "worker_id", "seq", "kind"} & fields.keys():
            raise LedgerError("event envelope cannot be overridden")
        event = dict(schema=1, event_id=str(uuid.uuid4()), run_id=self.run_id,
                     worker_id=self.worker_id, seq=self.seq + 1, kind=kind, **fields)
        _validate_event(event)
        line = canonical(event)
        self._file.write(line + "\n")
        self._file.flush()
        if self.durable:
            os.fsync(self._file.fileno())
        self.seq += 1
        return event

    def start(self, task: str, seed: int, attempt: int = 0, *, variant=None):
        key = dict(task=task, seed=seed, attempt=attempt)
        self.emit("attempt_start", **key, variant=variant)
        return AttemptWriter(self, key)

    def official(self, task: str, successes: int, denominator: int, *, label: str):
        """Copy an explicit upstream result verbatim, once per task/run."""
        self.emit("official_summary", task=task, successes=successes,
                  denominator=denominator, label=label)

    def finish(self):
        self.emit("worker_end")
        self.ended = True
        self._file.close()

    def close(self):
        """Close without claiming completion (e.g. exception/interruption)."""
        self._file.close()
        self.ended = True

    def __enter__(self):
        return self

    def __exit__(self, typ, value, traceback):
        if not self.ended:
            self.finish() if typ is None else self.close()


@dataclass
class AttemptWriter:
    ledger: Ledger
    key: dict

    def metadata(self, *, variant=None, scene_fingerprint=None, instruction_fingerprint=None):
        self.ledger.emit("case_metadata", **self.key, variant=variant,
                         scene_fingerprint=scene_fingerprint,
                         instruction_fingerprint=instruction_fingerprint)

    def expert(self, outcome: str):
        """accepted, rejected, or skipped (gate disabled)."""
        self.ledger.emit("expert_result", **self.key, outcome=outcome)

    def policy_start(self):
        self.ledger.emit("policy_start", **self.key)

    def policy(self, success: bool):
        if type(success) is not bool:
            raise LedgerError("policy success must be a bool")
        self.ledger.emit("policy_result", **self.key, outcome="success" if success else "fail")

    def infra(self, stage: str, reason: str):
        self.ledger.emit("infra_error", **self.key, stage=stage, reason=reason)


ATTEMPT_KINDS = {"attempt_start", "case_metadata", "expert_result", "policy_start", "policy_result", "infra_error"}
KINDS = ATTEMPT_KINDS | {"worker_start", "worker_end", "official_summary", "worker_error"}


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _validate_event(e):
    if not isinstance(e, dict) or type(e.get("schema")) is not int or e["schema"] != 1:
        raise LedgerError("expected event object with schema=1")
    for k in ("event_id", "run_id", "worker_id"):
        if not _nonempty(e.get(k)):
            raise LedgerError(f"{k} must be a nonempty string")
    if not _integer(e.get("seq"), 1):
        raise LedgerError("seq must be a positive integer")
    kind = e.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise LedgerError(f"unknown event kind {kind!r}")
    if kind in ATTEMPT_KINDS or kind == "official_summary":
        if not _nonempty(e.get("task")):
            raise LedgerError("task must be a nonempty string")
    if kind in ATTEMPT_KINDS:
        if type(e.get("seed")) is not int or not _integer(e.get("attempt")):
            raise LedgerError("seed must be integer and attempt must be nonnegative integer")
    if kind == "worker_start":
        if not _nonempty(e.get("config_fingerprint")) or type(e.get("synthetic")) is not bool:
            raise LedgerError("worker_start needs config_fingerprint and synthetic bool")
    for key in ("environment_fingerprint", "scene_fingerprint", "instruction_fingerprint"):
        if e.get(key) is not None and not _nonempty(e[key]):
            raise LedgerError(f"{key} must be null or nonempty string")
    if kind == "expert_result" and e.get("outcome") not in ("accepted", "rejected", "skipped"):
        raise LedgerError("expert outcome must be accepted/rejected/skipped")
    if kind == "policy_result" and e.get("outcome") not in ("success", "fail"):
        raise LedgerError("policy outcome must be success/fail")
    if kind in {"infra_error", "worker_error"}:
        if not _nonempty(e.get("stage")) or not _nonempty(e.get("reason")):
            raise LedgerError("error needs a nonempty stage and reason")
    if kind == "official_summary":
        if not _integer(e.get("successes")) or not _integer(e.get("denominator")) or e["successes"] > e["denominator"]:
            raise LedgerError("official result needs 0 <= successes <= denominator integers")
        if not _nonempty(e.get("label")):
            raise LedgerError("official result needs an explicit denominator label")
    try:
        canonical(e).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"not finite JSON: {exc}") from exc


@dataclass
class Attempt:
    task: str
    seed: int
    attempt: int
    worker_id: str
    config_fingerprint: str
    environment_fingerprint: str | None
    variant: Any = None
    scene_fingerprint: str | None = None
    instruction_fingerprint: str | None = None
    expert: str = "unknown"
    policy_started: bool = False
    outcome: str = "unfinished"
    error_stage: str | None = None
    error_reason: str | None = None

    @property
    def key(self):
        return self.task, self.seed

    def row(self):
        return dict(self.__dict__)


@dataclass
class Run:
    run_id: str
    attempts: list[Attempt]
    workers: dict
    official: list[dict]
    warnings: list[str] = field(default_factory=list)
    duplicate_events: int = 0
    files: int = 0
    worker_errors: list[dict] = field(default_factory=list)

    def latest(self):
        selected = {}
        for a in self.attempts:
            if a.key not in selected or a.attempt > selected[a.key].attempt:
                selected[a.key] = a
        return selected


def _pairs_no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LedgerError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _bad_constant(value):
    raise LedgerError(f"nonfinite JSON number {value}")


def load_run(paths) -> Run:
    """Merge one run; only a syntactically broken, unterminated last line is recoverable.

    Exact duplicate event IDs are de-duplicated. Conflicts, mid-file damage,
    sequence gaps, mixed runs and cross-worker attempt collisions fail closed.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    files = []
    for p in map(Path, paths):
        files.extend(sorted(p.glob("*.jsonl")) if p.is_dir() else [p])
    files = sorted(set(files))
    if not files:
        raise LedgerError("no .jsonl files found")
    events, warnings, duplicates = {}, [], 0
    for path in files:
        try:
            lines = path.read_bytes().splitlines(keepends=True)
        except OSError as exc:
            raise LedgerError(f"cannot read {path}: {exc}") from exc
        if not lines:
            warnings.append(f"{path.name}: empty file; no worker or attempt can be inferred")
        for index, line in enumerate(lines):
            where = f"{path.name}:{index + 1}"
            try:
                e = json.loads(line.decode("utf-8"), object_pairs_hook=_pairs_no_duplicate_keys, parse_constant=_bad_constant)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if index == len(lines) - 1 and not line.endswith(b"\n"):
                    warnings.append(f"{where}: ignored truncated final line; lost event contents are unknown")
                    continue
                raise LedgerError(f"{where}: malformed JSON: {exc}") from exc
            except LedgerError as exc:
                raise LedgerError(f"{where}: {exc}") from exc
            try:
                _validate_event(e)
            except LedgerError as exc:
                raise LedgerError(f"{where}: {exc}") from exc
            eid = e["event_id"]
            if eid in events:
                if canonical(e) != canonical(events[eid]):
                    raise LedgerError(f"{where}: conflicting duplicate event_id {eid}")
                duplicates += 1
            else:
                events[eid] = e
    if not events:
        raise LedgerError("no valid events; no run can be inferred")
    run_ids = {e["run_id"] for e in events.values()}
    if len(run_ids) != 1:
        raise LedgerError("mixed run_id values; summarize runs separately")
    by_worker = defaultdict(list)
    for e in events.values():
        by_worker[e["worker_id"]].append(e)
    attempts, workers, official, worker_errors = {}, {}, {}, []
    for worker, records in sorted(by_worker.items()):
        records.sort(key=lambda e: e["seq"])
        if [e["seq"] for e in records] != list(range(1, len(records) + 1)):
            raise LedgerError(f"worker {worker}: duplicate seq or sequence gap")
        if records[0]["kind"] != "worker_start":
            raise LedgerError(f"worker {worker}: missing initial worker_start")
        header = records[0]
        workers[worker] = dict(header, ended=False)
        for e in records[1:]:
            kind = e["kind"]
            if workers[worker]["ended"]:
                raise LedgerError(f"worker {worker}: events after worker_end")
            if kind == "worker_start":
                raise LedgerError(f"worker {worker}: repeated worker_start; use a new ID on restart")
            if kind == "worker_end":
                workers[worker]["ended"] = True
                continue
            if kind == "worker_error":
                worker_errors.append(e)
                continue
            if kind == "official_summary":
                if e["task"] in official:
                    raise LedgerError(f"repeated official result for task {e['task']}")
                official[e["task"]] = e
                continue
            key = e["task"], e["seed"], e["attempt"]
            if kind == "attempt_start":
                if key in attempts:
                    raise LedgerError(f"duplicate attempt {key}; retries need distinct attempt numbers")
                attempts[key] = Attempt(*key, worker, header["config_fingerprint"],
                                        header.get("environment_fingerprint"), variant=e.get("variant"))
                continue
            if key not in attempts or attempts[key].worker_id != worker:
                raise LedgerError(f"{key}: missing attempt_start in worker {worker}")
            a = attempts[key]
            if a.outcome != "unfinished":
                raise LedgerError(f"{key}: event after terminal outcome {a.outcome}")
            if kind == "case_metadata":
                for name in ("variant", "scene_fingerprint", "instruction_fingerprint"):
                    new = e.get(name)
                    old = getattr(a, name)
                    if new is not None:
                        if old is not None and canonical(old) != canonical(new):
                            raise LedgerError(f"{key}: conflicting {name}")
                        setattr(a, name, new)
            elif kind == "expert_result":
                if a.expert != "unknown" or a.policy_started:
                    raise LedgerError(f"{key}: expert result repeated or after policy_start")
                a.expert = e["outcome"]
                if a.expert == "rejected":
                    a.outcome = "expert_rejected"
            elif kind == "policy_start":
                if a.policy_started:
                    raise LedgerError(f"{key}: repeated policy_start")
                a.policy_started = True
            elif kind == "policy_result":
                if not a.policy_started:
                    raise LedgerError(f"{key}: policy_result without policy_start")
                a.outcome = "policy_" + e["outcome"]
            elif kind == "infra_error":
                a.outcome, a.error_stage, a.error_reason = "infra_error", e["stage"], e["reason"]
    if len({w["synthetic"] for w in workers.values()}) > 1:
        raise LedgerError("mixed synthetic and non-synthetic workers")
    config_fps = {w["config_fingerprint"] for w in workers.values()}
    if len(config_fps) > 1:
        warnings.append("Multiple config fingerprints within this run; review worker provenance before combining results")
    if len({w.get("environment_fingerprint") for w in workers.values()}) > 1:
        warnings.append("Different or missing environment fingerprints across workers; review provenance before combining results")
    for worker, w in workers.items():
        if not w["ended"]:
            warnings.append(f"Worker {worker}: no worker_end observed; run coverage may be incomplete")
    return Run(next(iter(run_ids)), sorted(attempts.values(), key=lambda a: (a.task, a.seed, a.attempt)),
               workers, list(official.values()), warnings, duplicates, len(files), worker_errors)


def _counts(attempts):
    attempts = list(attempts)
    counts = Counter(a.outcome for a in attempts)
    expert = Counter(a.expert for a in attempts)
    completed = counts["policy_success"] + counts["policy_fail"]
    return dict(attempts=len(attempts), expert_accepted=expert["accepted"],
                expert_rejected=expert["rejected"], expert_skipped=expert["skipped"], expert_unknown=expert["unknown"],
                policy_started=sum(a.policy_started for a in attempts), policy_completed=completed,
                policy_success=counts["policy_success"], policy_fail=counts["policy_fail"],
                infra_error=counts["infra_error"], unfinished=counts["unfinished"],
                observed_policy_rate=counts["policy_success"] / completed if completed else None)


def summarize(run: Run):
    latest = list(run.latest().values())
    variants = defaultdict(list)
    all_by_task, latest_by_task = defaultdict(list), defaultdict(list)
    for a in run.attempts:
        all_by_task[a.task].append(a)
    for a in latest:
        variants[(a.task, canonical(a.variant))].append(a)
        latest_by_task[a.task].append(a)
    seeds = Counter(a.key for a in run.attempts)
    retry_seeds = Counter(task for (task, seed), n in seeds.items() if n > 1)
    official = [{k: e[k] for k in ("task", "successes", "denominator", "label")} for e in run.official]
    official_by_task = {e["task"]: e for e in official}
    # Include official-only tasks; no observed attempts is evidence of neither
    # policy failure nor the upstream evaluation's coverage.
    tasks = [dict(task=task, all_attempts=_counts(all_by_task[task]),
                  latest_attempts=_counts(latest_by_task[task]),
                  retry_seeds=retry_seeds[task],
                  superseded_attempts=len(all_by_task[task]) - len(latest_by_task[task]),
                  official=official_by_task.get(task))
             for task in sorted(all_by_task.keys() | official_by_task.keys())]
    return dict(run_id=run.run_id, synthetic=all(w["synthetic"] for w in run.workers.values()),
                files=run.files, workers=len(run.workers), workers_without_end=sum(not w["ended"] for w in run.workers.values()),
                duplicate_events=run.duplicate_events, retry_seeds=sum(n > 1 for n in seeds.values()),
                superseded_attempts=len(run.attempts) - len(latest), worker_errors=len(run.worker_errors),
                all_attempts=_counts(run.attempts), latest_attempts=_counts(latest),
                official=official, tasks=tasks,
                variants=[dict(task=task, variant=json.loads(variant), **_counts(ats))
                          for (task, variant), ats in sorted(variants.items())], warnings=run.warnings)


def compare(left: Run, right: Run):
    """Latest declared attempt only; equal seeds are candidates, never proof."""
    l, r = left.latest(), right.latest()
    rows = []
    fields = ("environment_fingerprint", "variant", "scene_fingerprint", "instruction_fingerprint")
    for key in sorted(l.keys() | r.keys()):
        a, b = l.get(key), r.get(key)
        row = dict(task=key[0], seed=key[1], left_attempt=a.attempt if a else None,
                   right_attempt=b.attempt if b else None, left_outcome=a.outcome if a else "absent",
                   right_outcome=b.outcome if b else "absent", pairing="not_policy_pair", reason="")
        if a and b and a.outcome.startswith("policy_") and b.outcome.startswith("policy_"):
            missing = [k for k in fields if getattr(a, k) is None or getattr(b, k) is None]
            different = [k for k in fields if getattr(a, k) is not None and getattr(b, k) is not None
                         and canonical(getattr(a, k)) != canonical(getattr(b, k))]
            if different:
                row.update(pairing="mismatch", reason="different: " + ", ".join(different))
            elif missing:
                row.update(pairing="unknown", reason="missing: " + ", ".join(missing))
            else:
                row.update(pairing="metadata_matched", reason="equal declared metadata; not a simulator determinism guarantee")
        elif not a or not b:
            row["reason"] = "task/seed absent in one run"
        else:
            row["reason"] = "one or both latest attempts have no completed policy outcome"
        rows.append(row)
    lp = {k for k, a in l.items() if a.outcome.startswith("policy_")}
    rp = {k for k, a in r.items() if a.outcome.startswith("policy_")}
    matched = [x for x in rows if x["pairing"] == "metadata_matched"]
    tally = Counter(x["pairing"] for x in rows)
    same = bool(lp) and lp == rp and len(matched) == len(lp)
    return dict(left_run=left.run_id, right_run=right.run_id,
                synthetic=any(w["synthetic"] for run in (left, right) for w in run.workers.values()),
                shared_attempted_seeds=len(l.keys() & r.keys()), left_only_attempted=len(l.keys() - r.keys()),
                right_only_attempted=len(r.keys() - l.keys()), shared_policy_seeds=len(lp & rp),
                left_policy_seeds=len(lp), right_policy_seeds=len(rp),
                policy_seed_sets_equal=lp == rp, metadata_matched_pairs=len(matched),
                pairing_unknown=tally["unknown"], pairing_mismatch=tally["mismatch"],
                same_observed_policy_set="yes (declared metadata)" if same else (
                    "unknown (no policy outcomes)" if not lp and not rp else
                    "unknown (missing metadata)" if lp == rp and tally["unknown"] and not tally["mismatch"] else "no"),
                paired_left_success=sum(x["left_outcome"] == "policy_success" for x in matched),
                paired_right_success=sum(x["right_outcome"] == "policy_success" for x in matched),
                warnings=left.warnings + right.warnings, rows=rows)

"""Fabricated examples, deliberately not actual RoboTwin measurements."""
from pathlib import Path

from .ledger import Ledger, LedgerError, fingerprint, load_run
from .report import write_comparison, write_summary


def make_demo(out):
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise LedgerError(f"demo output must be empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    environment = {"simulator": "synthetic-only", "asset_revision": "fabricated-v1", "protocol": "demo-v1"}
    for name in ("run-a", "run-b"):
        workers = [Ledger(out / name / f"worker-{i}.jsonl", run_id=name, worker_id=str(i),
                          config={"policy": name}, environment=environment, synthetic=True) for i in range(2)]
        for seed in range(100):
            a = workers[seed % 2].start("synthetic_pick", seed, variant={"synthetic_object_variant": seed // 20})
            if name == "run-b" and seed >= 80:
                a.expert("rejected")
                continue
            a.expert("accepted")
            a.metadata(scene_fingerprint=fingerprint({"synthetic_scene": seed}),
                       instruction_fingerprint=fingerprint("synthetic pick instruction"))
            a.policy_start()
            a.policy(seed < (80 if name == "run-a" else 64))
        workers[0].official("synthetic_pick", 80 if name == "run-a" else 64, 100 if name == "run-a" else 80,
                            label="Synthetic upstream-style completed-policy denominator; not a benchmark result")
        for w in workers:
            w.finish()
        write_summary(load_run(out / name), out / f"{name}-report")
    w = Ledger(out / "recovery" / "worker-0.jsonl", run_id="synthetic-recovery", worker_id="0",
               config={"policy": "synthetic"}, synthetic=True)
    a = w.start("synthetic_pick", 0, 0)
    a.infra("setup", "synthetic renderer timeout")
    a = w.start("synthetic_pick", 0, 1)
    a.expert("accepted")
    a.policy_start()
    a.policy(True)
    a = w.start("synthetic_pick", 2)
    a.expert("rejected")
    a = w.start("synthetic_pick", 3)
    a.policy_start()  # Unknown expert status is intentional, never inferred.
    a.policy(True)
    a = w.start("synthetic_pick", 1)
    a.expert("accepted")
    a.policy_start()
    w.close()  # Deliberately omit worker_end, then append a crash-truncated event.
    with w.path.open("a", encoding="utf-8") as f:
        f.write('{"schema":1,"kind":"policy_result","outcome":"succ')
    first_line = w.path.read_bytes().splitlines(keepends=True)[0]
    (out / "recovery" / "duplicate-copy.jsonl").write_bytes(first_line)
    write_summary(load_run(out / "recovery"), out / "recovery-report")
    make_multitask_demo(out)
    write_comparison(load_run(out / "run-a"), load_run(out / "run-b"), out / "comparison")
    return out


def make_multitask_demo(out):
    """Small fabricated task mix; zero-completion tasks must remain visible."""
    out = Path(out)
    with Ledger(out / "multitask" / "worker-0.jsonl", run_id="synthetic-multitask", worker_id="0",
                config={"policy": "synthetic"}, synthetic=True) as w:
        for task, success in [("synthetic_success", True), ("synthetic_fail", False)]:
            for seed in range(2):
                a = w.start(task, seed)
                a.expert("accepted")
                a.policy_start()
                a.policy(success)
        w.official("synthetic_success", 2, 2, label="Fabricated upstream completed-policy denominator")
        for seed in range(2):
            w.start("synthetic_rejected", seed).expert("rejected")
        w.official("synthetic_rejected", 0, 0, label="Fabricated upstream completed-policy denominator")
        w.start("synthetic_infra", 0).infra("setup", "Fabricated setup interruption")
        w.start("synthetic_unfinished", 0).policy_start()
    return write_summary(load_run(out / "multitask"), out / "multitask-report")

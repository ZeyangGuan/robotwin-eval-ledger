# Optional integration: pinned, manual, additive

Reference: [`scripts/eval_policy_xpolicylab.py`](https://github.com/RoboTwin-Platform/RoboTwin/blob/ea8b21121ebb3cd201ff5b3fe361944ac94eda3f/scripts/eval_policy_xpolicylab.py) at commit `ea8b21121ebb3cd201ff5b3fe361944ac94eda3f`.

This recipe identifies instrumentation points only. It was reviewed against public source, not tested in RoboTwin. Keep upstream control flow, counters, output files and seed scheduling unchanged. Test the instrumented evaluator in your own environment before relying on it.

1. Create a `Ledger` per worker, with a shared run ID and unique worker ID/file. Capture the actual configuration and environment manifests. In batch mode, initialize it inside `batch_eval_worker`, not before multiprocessing spawn.
2. Before each seed attempt (`run_one_batch_episode` entry; or the single-run loop), call `log.start(task_name, seed, attempt)`. Maintain attempt numbers across restarts externally.
3. Immediately after setup, capture actual object-slot variants with `episode.metadata(variant=...)`. Read task-specific actor metadata before teardown. There is no assumed universal variant accessor.
4. At the expert gate, emit `accepted`, `rejected`, or `skipped` explicitly. In existing exception branches, emit `infra_error` with the observed stage/reason. Do not infer expert rejection from setup or transport errors.
5. After policy scene reset, record its actual initial-state and instruction fingerprints. Emit `policy_start` before policy preparation/rollout. At normal completion emit `policy_result`. At caught rollout exceptions emit `infra_error` instead, while preserving the upstream counter/result behavior separately.
6. If a batch episode slot is exhausted, leave the attempted seed without a policy result. Do not label it rejected or failed. The ledger will show it unfinished. A process killed without a catchable exception also remains unfinished.
7. Copy the upstream final numerator/denominator into one `official_summary`, with a descriptive label. Do not derive it from this ledger. Emit `worker_end` during clean worker teardown; use `close()` on abnormal exit.

The batch function catches some rollout exceptions and later returns an `episode_done` result. A wrapper that sees only this final result cannot distinguish every interrupted rollout from a normal policy failure; instrument the exception branch itself. Likewise, a scalar `_result.txt` cannot reconstruct variants or scene pairing evidence.

The library records infrastructure interruptions separately without changing what the upstream evaluator reports. A fresh worker may retry with a higher attempt number, but this package never schedules that retry or kills a process.

Minimal event calls and a synthetic exception/retry example are in [`examples/record_one_episode.py`](../examples/record_one_episode.py). Leave unavailable evidence null; do not fill metadata with placeholders just to obtain matched pairs.

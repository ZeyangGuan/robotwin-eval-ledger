"""Run from the checkout: python -m examples.record_one_episode

This is a synthetic logging example, not a RoboTwin evaluator.
"""
from robotwin_eval_ledger import Ledger, fingerprint


def main():
    with Ledger("example-logs/worker-0.jsonl", run_id="example-synthetic", worker_id="0",
                config={"policy": "fabricated"}, environment={"simulator": "none"},
                synthetic=True) as log:
        first = log.start("synthetic_task", seed=7, attempt=0)
        first.metadata(variant={"object": "synthetic-variant-4"})
        first.expert("accepted")
        first.policy_start()
        first.infra("rollout", "fabricated transport timeout")

        retry = log.start("synthetic_task", seed=7, attempt=1)
        retry.metadata(variant={"object": "synthetic-variant-4"},
                       scene_fingerprint=fingerprint({"synthetic_pose": [1, 2, 3]}),
                       instruction_fingerprint=fingerprint("synthetic instruction"))
        retry.expert("accepted")
        retry.policy_start()
        retry.policy(True)
        # Deliberately omit official(): a real upstream result is not available.
    print("Recorded synthetic example. Inspect with:")
    print("python -m robotwin_eval_ledger summarize example-logs --out example-report")


if __name__ == "__main__":
    main()

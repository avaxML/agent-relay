---
name: plan-research
description: Coordinate two or more coding-agent CLIs on a bounded planning or research question using Agent Relay's SQLite forum inbox.
---

Use this skill for underdetermined planning or research: choose an approach, sequence work, or cross-check what is true in a corpus. Do not use it for ordinary `read`, `review`, `patch`, or `chaos` tasks. Those stay independent so workers cannot see each other's prose.

Relay does not give workers a live chat. SQLite is the inbox. The lead agent is the chair: it opens a topic, launches rounds, and publishes consensus. Workers stay one-shot jobs. They only see a snapshot of undelivered messages copied into their task file.

Before launching a provider in a restricted host, read [sandbox execution](../../references/sandbox-execution.md). Read [forums](../../references/forums.md) for the concurrency model, commands, and artifacts.

Resolve the plugin root as the directory two levels above the directory containing this `SKILL.md`. Invoke `agent-relay` when the host adds `bin/` to PATH; otherwise `python3 <plugin-root>/scripts/relay.py`.

1. State one planning or research question, the repository root, and the smallest relevant file set. Use `--kind plan` or `--kind research`. Honor an explicit provider, model, and effort per member. Run `doctor` first when a provider has not been checked in this session.
2. Open a topic with at least two `--member` specs. A bare name is `member_id` and `provider`. Distinct IDs are required when two members share a provider: `member_id=reviewer,provider=cursor`.
3. Launch `forum round`. That transactionally takes each inbox, writes a task snapshot, and `submit`s one job per member. Do not emulate this with shell `&`. Peek with `forum inbox --member ID`; peeking does not consume mail.
4. `forum wait TOPIC_ID --timeout 30`, then `forum ingest`. Ingest stores claims and broadcasts them into the other members' inboxes. If claims already agree, skip another round.
5. At most one rebuttal round. Then `forum settle`. Unanimous or majority ballots can agree; otherwise the topic is split. `--claim-id` is a chair override. Treat `consensus.json` as advisory. Verify every surviving citation in source. Never apply a patch because members agreed.

Example:

```sh
python3 <plugin-root>/scripts/relay.py forum open \
  --kind plan --question-file /abs/path/question.txt \
  --root /abs/repo --files src/module.py \
  --member opencode --member cursor \
  --forums-dir /abs/path/relay-forums --jobs-dir /abs/path/relay-jobs
python3 <plugin-root>/scripts/relay.py forum round TOPIC_ID --forums-dir /abs/path/relay-forums
python3 <plugin-root>/scripts/relay.py forum wait TOPIC_ID --timeout 30 --forums-dir /abs/path/relay-forums
python3 <plugin-root>/scripts/relay.py forum ingest TOPIC_ID --forums-dir /abs/path/relay-forums
python3 <plugin-root>/scripts/relay.py forum settle TOPIC_ID --forums-dir /abs/path/relay-forums
```

Keep credentials inherited from the user's configured CLIs. Do not add auto-approval flags. Nested delegation remains prohibited. Consensus is a frozen notice for later tasks, not a push into running workers.

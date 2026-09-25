---
name: plan-research
description: Coordinate two or more coding-agent CLIs on a bounded planning or research question using Agent Relay's SQLite forum inbox.
---

Use this skill for underdetermined planning or research: choose an approach, sequence work, or cross-check what is true in a corpus. Do not use it for ordinary `read`, `review`, `patch`, or `chaos` tasks. Those stay independent so workers cannot see each other's prose.

Relay does not give workers a live chat. SQLite is the inbox. The lead agent is the chair: it opens a topic, launches rounds, and publishes consensus. Workers stay one-shot jobs. They only see a snapshot of undelivered messages copied into their task file.

Before launching a provider in a restricted host, read [sandbox execution](../../references/sandbox-execution.md). Read [forums](../../references/forums.md) for the concurrency model, commands, and artifacts.

Resolve the plugin root as the directory two levels above the directory containing this `SKILL.md`. Invoke `agent-relay` when the host adds `bin/` to PATH; otherwise `python3 <plugin-root>/scripts/relay.py`.

1. State one planning or research question, the repository root, and the smallest relevant file set. `forum open` copies those files, so later source edits do not change the topic's evidence. Use `--kind plan` or `--kind research`. Honor an explicit provider, model, and effort per member. Run `doctor` first when a provider has not been checked in this session.
2. Open a topic with at least two `--member` specs. A bare name is `member_id` and `provider`. Distinct IDs are required when two members share a provider: `member_id=reviewer,provider=cursor`.
3. Launch `forum round`. It reserves each member's inbox and job ID, then starts detached jobs. Do not emulate this with shell `&`. If launch stops midway, run `forum round` again to resume those IDs. Peek with `forum inbox --member ID`; peeking does not consume mail.
4. Run `forum wait TOPIC_ID --timeout 30`, then `forum ingest`. The first round records proposals without counting ballots. Ingest broadcasts claims to peers and puts the complete prior candidate set in every next-round task, including each member's own claim. If a member fails, ingest restores that member's consumed messages. If all jobs disappear, ingest abandons the round and restores all mail.
5. Run one more round for ballots if the chair wants automatic agreement. Then run `forum settle`. A member may affirm at most one unambiguous `claim_id` from the candidate set it received. A split leaves the topic open so the chair can choose an unambiguous latest claim with `--claim-id` or close. SQLite is authoritative for consensus; `forum export` repairs `consensus.json` after a publication failure. Verify every surviving citation against the source copy for the topic. Never apply a patch because members agreed.

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

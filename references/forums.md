# Plan and research forums

Planning and research questions are often underdetermined by the files. Agent Relay coordinates them with a **SQLite inbox** and the existing **file-backed jobs**. Workers never open the database, resume a CLI session, or call each other.

Jobs keep snapshots, logs, and `answer.txt` on disk because those artifacts are large and already have a lifecycle. The forum database holds membership, undelivered mail, claims, ballots, and consensus. Python's standard-library `sqlite3` supplies WAL for concurrent readers and `BEGIN IMMEDIATE` for round ownership. The topic directory holds a copy of the selected source files taken at `forum open`.

Maildir-style JSON files can make an inbox, but "unread for member M", "fan-out one claim to every other member", and "exactly one round is launching" are easy to get wrong with `rename`. SQLite makes those one transaction.

## Store

The database is `AGENT_RELAY_FORUMS_DIR/forum.sqlite` when set, otherwise `$XDG_STATE_HOME/agent-relay/forums/forum.sqlite` or `~/.local/state/agent-relay/forums/forum.sqlite`. Pass `--forums-dir` to select another directory. The directory is created with mode `0700` and the database file with mode `0600`. Symlinked forum directories or database files are rejected. WAL sidecar files (`forum.sqlite-wal`, `forum.sqlite-shm`) live next to the database.

Busy timeout is 5 seconds. Writers use `BEGIN IMMEDIATE`. `forum round` reserves every job ID and marks its input messages delivered in one transaction. It then prepares and launches those known jobs. If the chair stops during launch, run `forum round` again to resume the same IDs. To discard a pending round, run `forum abandon`. That command claims the round before cancelling jobs, restores its input messages, and returns the topic to `open`. Peek (`forum inbox`) never marks mail delivered.

Running workers have no inbox socket. Delivery means that the chair copies reserved messages into a member's next `task.txt` as an untrusted JSON block. Every member also gets the same complete set of prior claims, including its own claim. Round 1 has no candidates to vote on.

## Commands

```sh
python3 scripts/relay.py forum open --kind plan|research --question-file FILE --root DIR --files ... --member SPEC [--member SPEC ...]
python3 scripts/relay.py forum status TOPIC_ID
python3 scripts/relay.py forum inbox TOPIC_ID --member MEMBER
python3 scripts/relay.py forum post TOPIC_ID --body-file FILE [--to MEMBER | --broadcast]
python3 scripts/relay.py forum round TOPIC_ID
python3 scripts/relay.py forum wait TOPIC_ID [--timeout 30]
python3 scripts/relay.py forum ingest TOPIC_ID
python3 scripts/relay.py forum abandon TOPIC_ID
python3 scripts/relay.py forum settle TOPIC_ID [--claim-id ID] [--threshold unanimous|majority]
python3 scripts/relay.py forum export TOPIC_ID [--output FILE]
python3 scripts/relay.py forum close TOPIC_ID
```

`--member opencode` sets both `member_id` and `provider`. Use `member_id=reviewer,provider=cursor,effort=high,adapter_file=/abs/adapter.json` for distinct IDs or per-member settings. Open validates each adapter and rejects member IDs that are not `[a-z0-9-]{1,32}`. A forum needs at least two members. `--max-rounds` defaults to 2 and cannot exceed 4.

`round` submits one detached job per member with `--kind plan` or `--kind research`. Each job reads the source copy taken at `forum open`. `wait` waits on the round's jobs and does not cancel them. Missing job directories are reported as `missing` instead of raising. If a round remains in `launching`, retry `forum round` before waiting. `ingest` reads completed `result.json` files, stores claims and eligible ballots, and inserts new undelivered messages for the other members. A second ingest of the same round is rejected inside the write lock. Concatenated or non-object JSON answers, leftover JSON, oversized claim bodies, and unreadable `result.json` files are per-member `malformed` failures. A leading UTF-8 BOM on an otherwise valid object is ignored. If every job record is gone, ingest abandons the round and restores its inboxes. Other failed members regain the exact messages consumed by their attempt. If a peer would otherwise have an empty inbox, ingest posts a chair note so the next round can start. Ingest of a still-running round exits 2 with `pending`.

`forum post` is chair-only and accepts `note` or `task`. Claims, rebuttals, ballots, and consensus are produced by ingest and settle. Round 1 ballots are ignored because members have not seen a shared proposal set. Later ballots must name a `claim_id` in the prior candidate set. An unknown or conflicting ID, or two affirmative choices from one member, makes that member's answer malformed. Automatic settlement counts ballots for the reviewed candidate set, even if a member's latest rebuttal has a different claim ID. A `claim_id` is eligible only when every prior claim with that ID has the same position. Split consensus leaves the topic `open` so the chair can choose an unambiguous latest claim with `--claim-id` or run another round. Agreed topics become `settled`. Relay never applies a change because members agreed.

SQLite is the authority for consensus. `settle` and `close` commit their rows before writing `consensus.json`. A crash between those actions can leave the file absent or stale. `forum export` reads the committed row while holding the write lock and repairs the file. `forum close` writes `{status: closed}` to the database and then publishes that payload.

## Concurrency

Independent forum commands may run in different processes. Typical races:

- Two `forum post` calls: both insert; WAL serializes writers; no lost notes.
- Two `forum round` calls: one reserves the inbox and job IDs; the other may resume the same launching round or see that it is already running. Neither creates a second job ID.
- `forum inbox` during a round: peek sees remaining undelivered rows; it cannot steal the taken batch.
- `ingest` while a job is running: returns pending; it does not broadcast a partial claim set.
- Two `forum ingest` calls: one commits claims; the other sees the topic is no longer `round_pending` and does not duplicate mail.
- `forum abandon` versus `forum ingest`: abandonment marks the round failed before cancelling jobs; ingest rejects that round.
- `forum close` versus `forum round`: close checks status under the write lock and rejects a pending round.

There is still no global concurrency limiter for provider CLIs. Keep member count and overlapping topics within quota, as with ordinary `submit`.

## Boundaries

Workers remain bounded: no tools, no nested delegation, no shared conversation reuse. Inbox content and candidate claims are untrusted data in the task text. The lead agent verifies citations against the topic's source copy. Forum records, source copies, and job snapshots may contain sensitive material and should be retained deliberately. The SQLite file is local state, not an OS isolation boundary.

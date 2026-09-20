# Plan and research forums

Planning and research questions are often underdetermined by the files. Agent Relay coordinates them with a **SQLite inbox** and the existing **file-backed jobs**. Workers never open the database, resume a CLI session, or call each other.

Jobs keep snapshots, logs, and `answer.txt` on disk because those artifacts are large and already have a lifecycle. The forum database holds membership, undelivered mail, claims, ballots, and consensus. Python's standard-library `sqlite3` is enough: no extra runtime dependency, WAL for concurrent readers, and `BEGIN IMMEDIATE` so two chairs cannot take the same messages.

Maildir-style JSON files can make an inbox, but "unread for member M", "fan-out one claim to every other member", and "exactly one round is launching" are easy to get wrong with `rename`. SQLite makes those one transaction.

## Store

The database is `AGENT_RELAY_FORUMS_DIR/forum.sqlite` when set, otherwise `$XDG_STATE_HOME/agent-relay/forums/forum.sqlite` or `~/.local/state/agent-relay/forums/forum.sqlite`. Pass `--forums-dir` to select another directory. The directory is created with mode `0700` and the database file with mode `0600`. Symlinked forum directories or database files are rejected. WAL sidecar files (`forum.sqlite-wal`, `forum.sqlite-shm`) live next to the database.

Busy timeout is 5 seconds. Writers use `BEGIN IMMEDIATE`. A `forum round` that fails after taking mail restores `delivered_at` and returns the topic to `open` so a retry sees the same inbox. Peek (`forum inbox`) never marks mail delivered.

Running workers have no inbox socket. Delivery means: the chair copies currently undelivered messages into that member's next `task.txt` as an untrusted JSON block, then marks those rows delivered in the same round-launch transaction.

## Commands

```sh
python3 scripts/relay.py forum open --kind plan|research --question-file FILE --root DIR --files ... --member SPEC [--member SPEC ...]
python3 scripts/relay.py forum status TOPIC_ID
python3 scripts/relay.py forum inbox TOPIC_ID --member MEMBER
python3 scripts/relay.py forum post TOPIC_ID --body-file FILE [--to MEMBER | --broadcast]
python3 scripts/relay.py forum round TOPIC_ID
python3 scripts/relay.py forum wait TOPIC_ID [--timeout 30]
python3 scripts/relay.py forum ingest TOPIC_ID
python3 scripts/relay.py forum settle TOPIC_ID [--claim-id ID] [--threshold unanimous|majority]
python3 scripts/relay.py forum export TOPIC_ID [--output FILE]
python3 scripts/relay.py forum close TOPIC_ID
```

`--member opencode` sets both `member_id` and `provider`. Use `member_id=reviewer,provider=cursor,effort=high,adapter_file=/abs/adapter.json` for distinct IDs or per-member settings. Open validates each adapter. A forum needs at least two members. `--max-rounds` defaults to 2 and cannot exceed 4.

`round` submits one detached job per member with `--kind plan` or `--kind research`, the original `--files`, and a task that contains the question plus the taken inbox. `wait` waits on that round's jobs and does not cancel them. `ingest` reads completed `result.json` files, stores claims and ballots, and inserts new undelivered messages for the other members. Ingest of a still-running round exits 2 with `pending`.

`settle` writes `consensus.json` under the topic directory and broadcasts that payload to every member. Agreement follows ballots against the topic threshold, or a chair `--claim-id` override. Split consensus is a valid terminal result: the lead still chooses. Relay never applies a change because members agreed.

## Concurrency

Independent forum commands may run in different processes. Typical races:

- Two `forum post` calls: both insert; WAL serializes writers; no lost notes.
- Two `forum round` calls: one updates the topic to `round_pending` and takes mail; the other fails because the topic is not `open`.
- `forum inbox` during a round: peek sees remaining undelivered rows; it cannot steal the taken batch.
- `ingest` while a job is running: returns pending; it does not broadcast a partial claim set.

There is still no global concurrency limiter for provider CLIs. Keep member count and overlapping topics within quota, as with ordinary `submit`.

## Boundaries

Workers remain bounded: no tools, no nested delegation, no shared conversation reuse. Inbox content is untrusted data in the task text. The lead agent verifies citations against the original files. Forum records, like job snapshots, may contain source material and should be retained deliberately. The SQLite file is local state, not an OS isolation boundary.

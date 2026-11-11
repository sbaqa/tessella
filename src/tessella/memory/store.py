"""SQLite store for Cases and decisions. One local file; Postgres later for the team edition."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from tessella.models import Action, Case, Decision, RejectReason

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    origin TEXT NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL,          -- Case as JSON
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    by TEXT NOT NULL,
    at TEXT NOT NULL,
    board TEXT,
    ticket_key TEXT,
    ticket_url TEXT,
    linked_existing INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0  -- approved offline, not filed yet
);
CREATE TABLE IF NOT EXISTS reopened (   -- rejections undone by a re-check; never imported again
    signature TEXT NOT NULL,
    rejected_at TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (signature, rejected_at)
);
CREATE INDEX IF NOT EXISTS idx_decisions_signature ON decisions(signature);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    failures INTEGER NOT NULL,
    clusters INTEGER NOT NULL,
    recalled INTEGER NOT NULL,
    drafted INTEGER NOT NULL,
    detail TEXT NOT NULL,           -- JSON {signature: failures in that cluster}
    duration_s REAL,                -- wall-clock time of the whole triage
    model_s REAL,                   -- the part of it spent waiting on models
    git_commit TEXT                 -- HEAD of inputs.repo when the run was triaged
);
CREATE TABLE IF NOT EXISTS case_events (
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,             -- 'shown': first time the review queue displayed the case
    at TEXT NOT NULL,
    PRIMARY KEY (case_id, kind)
);
CREATE TABLE IF NOT EXISTS manual_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    seconds REAL NOT NULL,          -- how long a person took to investigate without Tessella
    filed INTEGER NOT NULL,         -- 1 if that investigation ended in a ticket
    signature TEXT,
    note TEXT,
    by TEXT NOT NULL,
    source TEXT NOT NULL            -- 'timer' (stopwatch) or 'imported' (a time from elsewhere)
);
CREATE TABLE IF NOT EXISTS ticket_outcomes (
    ticket_key TEXT PRIMARY KEY,
    signature TEXT,
    status TEXT,                    -- the board's own wording, e.g. "Done"
    resolution TEXT,                -- e.g. "Won't Do"; null while open
    outcome TEXT NOT NULL,          -- open | fixed | not_a_bug | duplicate | other
    created TEXT,
    resolved TEXT,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS probe_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    flow TEXT NOT NULL,
    steps INTEGER NOT NULL,
    linked INTEGER NOT NULL,        -- IDs found flowing from one step into a later one
    mutations INTEGER NOT NULL,     -- planned, not replayed
    kinds TEXT NOT NULL             -- JSON {mutator: count}
);
"""


def _edited(payload: str) -> bool:
    """Has a reviewer changed this case's draft? Their words are not replaced by a merge."""
    draft = json.loads(payload).get("draft") or {}
    return bool(draft.get("original"))


class Store:
    def __init__(self, path: str | Path):
        p = Path(path)
        self._path = None if str(p) == ":memory:" else p
        if str(p) != ":memory:":
            p.parent.mkdir(parents=True, exist_ok=True)
        # One shared connection (check_same_thread=False) served to FastAPI's threadpool;
        # a lock serializes access so concurrent requests can't interleave on one cursor.
        self.db = sqlite3.connect(str(p), check_same_thread=False, timeout=30)
        self._lock = threading.Lock()
        if self._path is not None:
            # A shared queue reads while a triage (another process) writes: WAL lets them
            # overlap, and the timeout waits out a write instead of failing at once.
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self._migrate()
        self._dedupe_decisions()

    def _migrate(self) -> None:
        """Add columns that older databases lack: CREATE TABLE IF NOT EXISTS leaves an
        existing table exactly as it was. The wanted columns come from _SCHEMA itself, so a
        column added there can never be missing from an older store."""
        ref = sqlite3.connect(":memory:")
        ref.executescript(_SCHEMA)
        tables = [r[0] for r in ref.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        with self._lock:
            for table in tables:  # from _SCHEMA, never user input
                have = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for _cid, name, ctype, notnull, default, pk in ref.execute(
                        f"PRAGMA table_info({table})"):
                    if name in have or pk:
                        continue
                    # ADD COLUMN takes NOT NULL only with a default; without one, the column
                    # is added nullable (old rows have no value, new inserts always give one).
                    decl = ctype
                    if default is not None:
                        decl += f"{' NOT NULL' if notnull else ''} DEFAULT {default}"
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            # Stores from before boards were generic kept the Jira key in decisions.jira_key.
            if "jira_key" in {row[1] for row in self.db.execute("PRAGMA table_info(decisions)")}:
                self.db.execute(
                    "UPDATE decisions SET ticket_key = jira_key, board = COALESCE(board, 'jira') "
                    "WHERE ticket_key IS NULL AND jira_key IS NOT NULL AND jira_key != ''")
            self.db.commit()
        ref.close()

    def _dedupe_decisions(self) -> None:
        """Collapse duplicate approve/reject rows left by older builds: a double-submitted
        approval (or a retry after a slow/half-failed board call) wrote two identical
        decisions for one case. Keep the earliest per case and failure (signature); delete rows
        are left alone."""
        with self._lock:
            self.db.execute(
                "DELETE FROM decisions WHERE action != 'delete' AND id NOT IN "
                "(SELECT MIN(id) FROM decisions WHERE action != 'delete' "
                "GROUP BY case_id, signature)"
            )
            self.db.commit()

    def save_case(self, case: Case) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO cases VALUES (?, ?, ?, ?, ?, ?)",
                (case.id, case.signature, case.origin, case.title, case.model_dump_json(),
                 datetime.now().isoformat()),
            )
            self.db.commit()

    def get_case(self, case_id: str) -> Case | None:
        with self._lock:
            row = self.db.execute("SELECT payload FROM cases WHERE id = ?", (case_id,)).fetchone()
        return Case.model_validate_json(row[0]) if row else None

    def case_with_signature(self, signature: str) -> Case | None:
        """The most recent stored case for a failure, whatever test it came from: a renamed
        or moved test keeps its signature but gets a new case id."""
        with self._lock:
            row = self.db.execute("SELECT payload FROM cases WHERE signature = ? "
                                  "ORDER BY created_at DESC LIMIT 1", (signature,)).fetchone()
        return Case.model_validate_json(row[0]) if row else None

    def open_cases(self) -> list[Case]:
        """Cases whose current failure has no decision. A decision belongs to one failure: when
        the same test later fails differently (a new signature), that is a new question."""
        with self._lock:
            rows = self.db.execute(
                "SELECT payload FROM cases c WHERE NOT EXISTS (SELECT 1 FROM decisions d "
                "WHERE d.case_id = c.id AND d.signature = c.signature)"
            ).fetchall()
        return [Case.model_validate_json(r[0]) for r in rows]

    def decided(self, case_id: str) -> bool:
        """Has this case already been approved/rejected? Delete rows don't count — a deleted
        ticket is meant to re-surface the case for a fresh decision (see `forget`). Only a
        decision on the case's current failure counts (see `open_cases`)."""
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM decisions d JOIN cases c ON c.id = d.case_id "
                "WHERE d.case_id = ? AND d.signature = c.signature AND d.action != 'delete' "
                "LIMIT 1", (case_id,),
            ).fetchone()
        return row is not None

    def record(self, case: Case, decision: Decision) -> bool:
        """Record a decision. Idempotent per case and failure: a case whose current signature
        already has an approve/reject
        keeps its first decision and this returns False without inserting. A double-submit, or
        a retry after a slow/half-failed board call, must not write a second row — the board
        side is already deduped by signature label, and the local store has to match. The
        existence check and insert share one lock hold, so concurrent requests can't both pass.
        Returns True when a new row was written. (Re-deciding after `forget` works: forget
        clears the rows first, so there is nothing to collide with.)"""
        with self._lock:
            existing = self.db.execute(
                "SELECT 1 FROM decisions WHERE case_id = ? AND signature = ? "
                "AND action != 'delete' LIMIT 1", (case.id, case.signature),
            ).fetchone()
            if existing:
                return False
            self.db.execute(
                "INSERT INTO decisions (case_id, signature, action, reason, by, at, "
                "board, ticket_key, ticket_url, linked_existing, pending) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (case.id, case.signature, decision.action, decision.reason, decision.by,
                 decision.at.isoformat(), decision.board, decision.ticket_key, decision.ticket_url,
                 int(decision.linked_existing), int(decision.pending)),
            )
            self.db.commit()
            return True

    def last_decision(self, signature: str) -> Decision | None:
        """Most recent approve/reject for a signature. Deletes are not training signal."""
        with self._lock:
            row = self.db.execute(
                "SELECT action, reason, by, at, board, ticket_key, ticket_url, pending FROM decisions "
                "WHERE signature = ? AND action != 'delete' ORDER BY id DESC LIMIT 1",
                (signature,),
            ).fetchone()
        if not row:
            return None
        action, reason, by, at, board, ticket_key, ticket_url, pending = row
        return Decision(
            action=Action(action),
            reason=RejectReason(reason) if reason else None,
            by=by,
            at=datetime.fromisoformat(at),
            board=board,
            ticket_key=ticket_key,
            ticket_url=ticket_url,
            pending=bool(pending),
        )

    def forget(self, signature: str) -> None:
        """Drop all decisions for a signature, so it stops being recalled and re-enters the
        queue. Used when a filed ticket is confirmed deleted from the tracker."""
        with self._lock:
            self.db.execute("DELETE FROM decisions WHERE signature = ?", (signature,))
            # Its cards are shown afresh, so review time restarts from the next display.
            self.db.execute(
                "DELETE FROM case_events WHERE case_id IN "
                "(SELECT id FROM cases WHERE signature = ?)", (signature,))
            self.db.execute("DELETE FROM ticket_outcomes WHERE signature = ?", (signature,))
            self.db.commit()

    def reopen(self, signature: str, rejected: Decision) -> None:
        """A re-check found new evidence against a rejection: back to the queue, and that
        rejection is remembered so the decisions file cannot bring it back."""
        self.forget(signature)
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO reopened (signature, rejected_at, at) VALUES (?, ?, ?)",
                            (signature, rejected.at.isoformat(), datetime.now().isoformat()))
            self.db.commit()

    def import_decisions(self, entries: list[dict]) -> int:
        """Add decisions from the committed decisions file. What this store already decided
        for a failure stands; a rejection a re-check undid stays undone. Returns rows added."""
        added = 0
        with self._lock:
            have = {tuple(r) for r in self.db.execute(
                "SELECT case_id, signature FROM decisions WHERE action != 'delete'")}
            undone = {tuple(r) for r in self.db.execute("SELECT signature, rejected_at FROM reopened")}
            for e in entries:
                if (e["case_id"], e["signature"]) in have or (e["signature"], e["at"]) in undone:
                    continue
                self.db.execute(
                    "INSERT INTO decisions (case_id, signature, action, reason, by, at, board, "
                    "ticket_key, ticket_url, linked_existing, pending) VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, 0, ?)",
                    (e["case_id"], e["signature"], e["action"], e.get("reason"), e["at"], e.get("board"),
                     e.get("ticket_key"), e.get("ticket_url"), int(bool(e.get("pending")))))
                added += 1
            self.db.commit()
        return added

    def pending_filings(self) -> list[tuple[Case, Decision]]:
        """Approvals made while the board was out of reach, oldest first."""
        return [pair for pair in reversed(self.recent_decisions(limit=1000)) if pair[1].pending]

    def mark_filed(self, case: Case, decision: Decision) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE decisions SET pending = 0, board = ?, ticket_key = ?, ticket_url = ?, "
                "linked_existing = ? WHERE case_id = ? AND signature = ? AND pending = 1",
                (decision.board, decision.ticket_key, decision.ticket_url, int(decision.linked_existing),
                 case.id, case.signature))
            self.db.commit()

    def snapshot(self, target: str | Path) -> None:
        """A complete copy of this database in one file, safe to hand to another machine."""
        with self._lock:
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            self.db.execute("VACUUM INTO ?", (str(target),))

    def recent_decisions(self, limit: int = 20, current: bool = False) -> list[tuple[Case, Decision]]:
        """Most recently decided cases, newest first, for the queue's history view. `current`
        keeps only decisions on each case's current failure: a test that has since failed
        another way shows once, as an open case, not again under an old decision."""
        with self._lock:
            rows = self.db.execute(
                "SELECT c.payload, d.action, d.reason, d.by, d.at, d.board, d.ticket_key, d.ticket_url, d.pending "
                "FROM decisions d JOIN cases c ON c.id = d.case_id "
                + ("WHERE d.signature = c.signature " if current else "")
                + "ORDER BY d.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for payload, action, reason, by, at, board, ticket_key, ticket_url, pending in rows:
            case = Case.model_validate_json(payload)
            decision = Decision(
                action=Action(action),
                reason=RejectReason(reason) if reason else None,
                by=by,
                at=datetime.fromisoformat(at),
                board=board,
                ticket_key=ticket_key,
                ticket_url=ticket_url,
                pending=bool(pending),
            )
            out.append((case, decision))
        return out

    # -- run history and read-only views, used by `tessella report` ----------------------------

    def record_run(self, failures: int, clusters: int, recalled: int, drafted: int,
                   detail: dict[str, int], at: datetime | None = None,
                   duration_s: float | None = None, model_s: float | None = None,
                   git_commit: str | None = None) -> None:
        """One row per triage run. `cases.created_at` is overwritten on every save, so this
        table is the only history of how many failures each run saw and which clusters
        came back."""
        with self._lock:
            self.db.execute(
                "INSERT INTO runs (at, failures, clusters, recalled, drafted, detail, "
                "duration_s, model_s, git_commit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ((at or datetime.now()).isoformat(), failures, clusters, recalled, drafted,
                 json.dumps(detail), duration_s, model_s, git_commit),
            )
            self.db.commit()

    def last_green_commit(self) -> str | None:
        """The commit of the most recent triage that saw no failures: what `git diff`
        compares the failing code against."""
        with self._lock:
            row = self.db.execute("SELECT git_commit FROM runs WHERE failures = 0 AND "
                                  "git_commit IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def runs(self) -> list[dict]:
        """Every recorded triage run, oldest first."""
        with self._lock:
            rows = self.db.execute(
                "SELECT at, failures, clusters, recalled, drafted, detail, duration_s, model_s "
                "FROM runs ORDER BY id"
            ).fetchall()
        out = []
        for at, failures, clusters, recalled, drafted, detail, duration_s, model_s in rows:
            try:
                parsed = json.loads(detail)
            except ValueError:
                parsed = {}
            out.append({"at": at, "failures": failures, "clusters": clusters,
                        "recalled": recalled, "drafted": drafted, "detail": parsed,
                        "duration_s": duration_s, "model_s": model_s})
        return out

    def all_cases(self) -> list[Case]:
        """Every stored case, decided or not, oldest first."""
        with self._lock:
            rows = self.db.execute("SELECT payload FROM cases ORDER BY created_at, id").fetchall()
        return [Case.model_validate_json(r[0]) for r in rows]

    def all_decisions(self) -> list[tuple[str, Decision]]:
        """Every approve/reject/edit as (signature, decision), oldest first. Deletes are
        not decisions, so they are left out."""
        with self._lock:
            rows = self.db.execute(
                "SELECT signature, action, reason, by, at, board, ticket_key, ticket_url, "
                "linked_existing, pending FROM decisions WHERE action != 'delete' ORDER BY id"
            ).fetchall()
        return [
            (sig, Decision(action=Action(action), reason=RejectReason(reason) if reason else None,
                           by=by, at=datetime.fromisoformat(at), board=board,
                           ticket_key=ticket_key, ticket_url=ticket_url,
                           linked_existing=bool(linked), pending=bool(pending)))
            for sig, action, reason, by, at, board, ticket_key, ticket_url, linked, pending in rows
        ]

    # -- measurements behind "time saved" -------------------------------------------------------

    def mark_shown(self, case_ids: list[str], at: datetime | None = None) -> None:
        """Remember when the review queue first displayed each case. Only the first display
        counts (INSERT OR IGNORE), so reloading the page does not restart the clock."""
        if not case_ids:
            return
        stamp = (at or datetime.now()).isoformat()
        with self._lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO case_events (case_id, kind, at) VALUES (?, 'shown', ?)",
                [(cid, stamp) for cid in case_ids])
            self.db.commit()

    def review_timeline(self) -> list[dict]:
        """Every approve/reject/edit in order, with when its case was first shown (or None
        for decisions made before that was recorded). Raw material for review durations."""
        with self._lock:
            rows = self.db.execute(
                "SELECT d.case_id, d.signature, d.action, d.by, d.at, e.at "
                "FROM decisions d LEFT JOIN case_events e "
                "ON e.case_id = d.case_id AND e.kind = 'shown' "
                "WHERE d.action != 'delete' ORDER BY d.id").fetchall()
        return [{"case_id": cid, "signature": sig, "action": action, "by": by,
                 "decided_at": datetime.fromisoformat(at),
                 "shown_at": datetime.fromisoformat(shown) if shown else None}
                for cid, sig, action, by, at, shown in rows]

    def add_manual_sample(self, seconds: float, filed: bool, signature: str | None = None,
                          note: str = "", by: str = "sdet", source: str = "timer",
                          at: datetime | None = None) -> None:
        """One timed manual investigation: how long a person took without Tessella."""
        with self._lock:
            self.db.execute(
                "INSERT INTO manual_samples (at, seconds, filed, signature, note, by, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ((at or datetime.now()).isoformat(), float(seconds), int(filed), signature,
                 note, by, source))
            self.db.commit()

    def manual_samples(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT at, seconds, filed, signature, note, by, source FROM manual_samples "
                "ORDER BY id").fetchall()
        return [{"at": at, "seconds": seconds, "filed": bool(filed), "signature": sig,
                 "note": note or "", "by": by, "source": source}
                for at, seconds, filed, sig, note, by, source in rows]

    def shown_ids(self) -> set[str]:
        """Ids of every case the review queue has displayed."""
        with self._lock:
            rows = self.db.execute(
                "SELECT case_id FROM case_events WHERE kind = 'shown'").fetchall()
        return {r[0] for r in rows}

    # -- ticket outcomes: what happened to the tickets Tessella filed -----------------------------

    def save_outcome(self, key: str, outcome: str, signature: str | None = None,
                     status: str | None = None, resolution: str | None = None,
                     created: str | None = None, resolved: str | None = None,
                     at: datetime | None = None) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO ticket_outcomes (ticket_key, signature, status, resolution, "
                "outcome, created, resolved, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, signature, status, resolution, outcome, created, resolved,
                 (at or datetime.now()).isoformat()))
            self.db.commit()

    def outcomes(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT ticket_key, signature, status, resolution, outcome, created, resolved, "
                "checked_at FROM ticket_outcomes ORDER BY ticket_key").fetchall()
        return [dict(zip(("key", "signature", "status", "resolution", "outcome", "created",
                          "resolved", "checked_at"), r)) for r in rows]

    def filed_tickets(self) -> list[dict]:
        """Distinct Jira tickets that decisions point at, with the signature each one covers."""
        with self._lock:
            rows = self.db.execute(
                "SELECT ticket_key, MIN(signature) FROM decisions WHERE action = 'approve' "
                "AND ticket_key IS NOT NULL AND ticket_key != '' AND COALESCE(board, '') = 'jira' "
                "GROUP BY ticket_key ORDER BY MIN(id)").fetchall()
        return [{"key": k, "signature": sig} for k, sig in rows]

    # -- probe plans -----------------------------------------------------------------------------

    def record_probe(self, flow: str, steps: int, linked: int, kinds: dict[str, int],
                     at: datetime | None = None) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO probe_plans (at, flow, steps, linked, mutations, kinds) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ((at or datetime.now()).isoformat(), flow, steps, linked, sum(kinds.values()),
                 json.dumps(kinds)))
            self.db.commit()

    def probes(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT at, flow, steps, linked, mutations, kinds FROM probe_plans ORDER BY id"
            ).fetchall()
        out = []
        for at, flow, steps, linked, mutations, kinds in rows:
            try:
                parsed = json.loads(kinds)
            except ValueError:
                parsed = {}
            out.append({"at": at, "flow": flow, "steps": steps, "linked": linked,
                        "mutations": mutations, "kinds": parsed})
        return out

    # -- merge: bring another database (a CI run, a teammate) into this one ----------------------

    def merge_from(self, other_path: str | Path) -> dict[str, int]:
        """Add what `other_path` knows and this store does not. Returns rows added per table.

        Nothing here is overwritten except: a case with no verdict here takes the other copy's
        verdict, a newer run's copy replaces a case that fails a new way or that nobody decided or
        edited here, an earlier
        first-shown time wins, and the newer ticket check wins. Decisions
        are matched on case, action and time, so merging the same file twice adds nothing.
        The other file is read through a temporary copy and is never modified."""
        import shutil
        import tempfile

        src_path = Path(other_path)
        if not src_path.is_file():
            raise FileNotFoundError(f"no such database: {src_path}")
        if self._path is not None and src_path.resolve() == self._path.resolve():
            raise ValueError("that is this database")
        added = {"cases": 0, "decisions": 0, "runs": 0, "samples": 0, "outcomes": 0,
                 "probes": 0, "events": 0}
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "other.sqlite"
            shutil.copy2(src_path, copy)
            other = Store(copy)  # upgrades the copy's schema the same way ours was
            try:
                with self._lock, other._lock:
                    mine, theirs = self.db, other.db
                    have_cases = dict(mine.execute("SELECT id, payload FROM cases"))
                    created_here = dict(mine.execute("SELECT id, created_at FROM cases"))
                    signature_here = dict(mine.execute("SELECT id, signature FROM cases"))
                    decided_here = {r[0] for r in mine.execute(
                        "SELECT case_id FROM decisions WHERE action != 'delete'")}
                    for cid, sig, origin, title, payload, created in theirs.execute(
                            "SELECT id, signature, origin, title, payload, created_at FROM cases"):
                        if cid not in have_cases:
                            mine.execute("INSERT INTO cases (id, signature, origin, title, payload, "
                                         "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                         (cid, sig, origin, title, payload, created))
                            added["cases"] += 1
                        elif '"verdict":null' in have_cases[cid].replace(" ", "") \
                                and '"verdict":null' not in payload.replace(" ", ""):
                            mine.execute("UPDATE cases SET payload = ? WHERE id = ?", (payload, cid))
                        elif (created > created_here[cid]
                                and '"verdict":null' not in payload.replace(" ", "")
                                and (sig != signature_here[cid]  # the same test, failing a new way
                                     or (cid not in decided_here and not _edited(have_cases[cid])))):
                            mine.execute("UPDATE cases SET signature = ?, title = ?, payload = ?, created_at = ? "
                                         "WHERE id = ?", (sig, title, payload, created, cid))
                    seen = {(c, a, t) for c, a, t in mine.execute(
                        "SELECT case_id, action, at FROM decisions")}
                    for row in theirs.execute(
                            "SELECT case_id, signature, action, reason, by, at, board, ticket_key, "
                            "ticket_url, linked_existing, pending FROM decisions ORDER BY id"):
                        if (row[0], row[2], row[5]) in seen:
                            continue
                        mine.execute("INSERT INTO decisions (case_id, signature, action, reason, by, "
                                     "at, board, ticket_key, ticket_url, linked_existing, pending) "
                                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
                        added["decisions"] += 1
                    # A rejection the other side re-checked and undid is undone here too.
                    for sig, rejected_at, at in theirs.execute(
                            "SELECT signature, rejected_at, at FROM reopened"):
                        mine.execute("INSERT OR IGNORE INTO reopened (signature, rejected_at, at) "
                                     "VALUES (?, ?, ?)", (sig, rejected_at, at))
                        mine.execute("DELETE FROM decisions WHERE signature = ? AND at = ? AND action = 'reject'",
                                     (sig, rejected_at))
                    seen_runs = {r[0] for r in mine.execute("SELECT at FROM runs")}
                    for row in theirs.execute(
                            "SELECT at, failures, clusters, recalled, drafted, detail, duration_s, "
                            "model_s FROM runs ORDER BY id"):
                        if row[0] not in seen_runs:
                            mine.execute("INSERT INTO runs (at, failures, clusters, recalled, drafted, "
                                         "detail, duration_s, model_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                         row)
                            added["runs"] += 1
                    mine_events = dict(((c, k), a) for c, k, a in mine.execute(
                        "SELECT case_id, kind, at FROM case_events"))
                    for cid, kind, at in theirs.execute("SELECT case_id, kind, at FROM case_events"):
                        if (cid, kind) not in mine_events:
                            mine.execute("INSERT INTO case_events (case_id, kind, at) VALUES (?, ?, ?)",
                                         (cid, kind, at))
                            added["events"] += 1
                        elif at < mine_events[(cid, kind)]:
                            mine.execute("UPDATE case_events SET at = ? WHERE case_id = ? AND kind = ?",
                                         (at, cid, kind))
                    seen_s = {(a, s_, b) for a, s_, b in mine.execute(
                        "SELECT at, seconds, by FROM manual_samples")}
                    for row in theirs.execute("SELECT at, seconds, filed, signature, note, by, source "
                                              "FROM manual_samples ORDER BY id"):
                        if (row[0], row[1], row[5]) not in seen_s:
                            mine.execute("INSERT INTO manual_samples (at, seconds, filed, signature, "
                                         "note, by, source) VALUES (?, ?, ?, ?, ?, ?, ?)", row)
                            added["samples"] += 1
                    mine_out = dict(mine.execute("SELECT ticket_key, checked_at FROM ticket_outcomes"))
                    for row in theirs.execute(
                            "SELECT ticket_key, signature, status, resolution, outcome, created, "
                            "resolved, checked_at FROM ticket_outcomes"):
                        if row[0] not in mine_out or row[7] > mine_out[row[0]]:
                            mine.execute("INSERT OR REPLACE INTO ticket_outcomes (ticket_key, signature, "
                                         "status, resolution, outcome, created, resolved, checked_at) "
                                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
                            added["outcomes"] += 1
                    seen_p = {(a, f) for a, f in mine.execute("SELECT at, flow FROM probe_plans")}
                    for row in theirs.execute("SELECT at, flow, steps, linked, mutations, kinds "
                                              "FROM probe_plans ORDER BY id"):
                        if (row[0], row[1]) not in seen_p:
                            mine.execute("INSERT INTO probe_plans (at, flow, steps, linked, mutations, "
                                         "kinds) VALUES (?, ?, ?, ?, ?, ?)", row)
                            added["probes"] += 1
                    mine.commit()
            finally:
                other.db.close()
        self._dedupe_decisions()
        return added

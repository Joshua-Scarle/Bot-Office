"""Validation and transactional storage for interactive questions."""
import json
import re
from contextlib import closing
from uuid import uuid4

from .store import now


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("question must be an object")
    steps = payload.get("steps", [payload])
    if not isinstance(steps, list) or not 1 <= len(steps) <= 4:
        raise ValueError("a question needs 1–4 steps")
    result = []
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("step must be an object")
        prompt, header = step.get("prompt"), step.get("header", "")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
            raise ValueError("prompt must be 1–2000 characters")
        if not isinstance(header, str) or len(header) > 12:
            raise ValueError("header must be at most 12 characters")
        options = step.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 8:
            raise ValueError("each step needs 2–8 options")
        choices = []
        for option in options:
            if not isinstance(option, dict):
                raise ValueError("option must be an object")
            label, desc = option.get("label"), option.get("description", "")
            value = option.get("value", label)
            if not isinstance(label, str) or not label.strip() or len(label) > 80:
                raise ValueError("label must be 1–80 characters")
            if not isinstance(desc, str) or len(desc) > 400:
                raise ValueError("description must be at most 400 characters")
            if not isinstance(value, str) or not value.strip() or len(value) > 400:
                raise ValueError("option value must be 1–400 characters")
            choices.append({"label": label, "description": desc, "value": value})
        if len({c['value'] for c in choices}) != len(choices):
            raise ValueError("option values must be unique")
        multi, custom = step.get("multiSelect", False), step.get("allowCustom", True)
        if not isinstance(multi, bool) or not isinstance(custom, bool):
            raise ValueError("multiSelect and allowCustom must be booleans")
        result.append({"prompt": prompt, "header": header, "options": choices,
                       "multiSelect": multi, "allowCustom": custom})
    return {"steps": result}


def validate_answer(payload, body):
    if not isinstance(body, dict) or not isinstance(body.get("skip", False), bool):
        raise ValueError("invalid answer")
    if body.get("skip"):
        return {"skip": True, "steps": []}
    steps = payload["steps"]
    answers = body.get("steps", [body])
    if not isinstance(answers, list) or len(answers) != len(steps):
        raise ValueError("answer every step")
    result = []
    for step, answer in zip(steps, answers, strict=True):
        if not isinstance(answer, dict):
            raise ValueError("invalid step answer")
        selected, custom = answer.get("selected", []), answer.get("custom", "")
        if not isinstance(selected, list) or not all(isinstance(x, str) for x in selected):
            raise ValueError("selected must be a list of strings")
        if not isinstance(custom, str) or len(custom) > 2000:
            raise ValueError("invalid custom answer")
        custom = custom.strip()
        values = []
        for pick in selected:
            matches = [o for o in step["options"] if pick in (o["value"], o["label"])]
            if len(matches) != 1 or matches[0]["value"] in values:
                raise ValueError("unknown or duplicate option")
            values.append(matches[0]["value"])
        if custom and not step["allowCustom"]:
            raise ValueError("custom answers are disabled")
        if not values and not custom:
            raise ValueError("select an option or enter an answer")
        if not step["multiSelect"] and len(values) + bool(custom) > 1:
            raise ValueError("only one answer is allowed")
        result.append({"selected": values, "custom": custom})
    return {"skip": False, "steps": result}


def unpack(row):
    if row is None:
        return None
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    item["answer"] = json.loads(item["answer"]) if item["answer"] else None
    item["blocking"] = bool(item["blocking"])
    return item


def _restore(store, db, row, actor="user"):
    if row["assignment_id"]:
        assignment = db.execute(
            "SELECT * FROM assignments WHERE id=? AND status='waiting_on_user' AND status_note=?",
            (row["assignment_id"], f"Question {row['id']} awaiting answer"),
        ).fetchone()
        if assignment:
            from .assignments import Assignments

            Assignments(store)._set_status(db, assignment, "active", "Question resolved", actor=actor,
                                           job_id=row["job_id"], announce=False)


def create(store, bot_id, job_id, payload, blocking=False):
    payload = validate(payload)
    key, stamp = str(uuid4()), now().isoformat()
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            previous = db.execute(
                "SELECT * FROM questions WHERE bot_id=? AND status='open'", (str(bot_id),)
            ).fetchall()
            for old in previous:
                db.execute("UPDATE questions SET status='skipped', answer=?, answered_at=? "
                           "WHERE id=?",
                           (json.dumps({"skip": True, "steps": []}), stamp, old["id"]))
                _restore(store, db, old, actor=f"bot:{bot_id}")
            job = db.execute("SELECT assignment_id FROM jobs WHERE id=? AND bot_id=?",
                             (str(job_id), str(bot_id))).fetchone() if job_id else None
            assignment = job["assignment_id"] if job else None
            msg = db.execute("INSERT INTO messages (bot_id,role,source,text,job_id,created_at) "
                             "VALUES (?,'assistant','chat',?,NULL,?)",
                             (str(bot_id), "Asked a question.", stamp))
            db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?, 'open',NULL,?,?,NULL)",
                       (key, str(bot_id), str(job_id) if job_id else None, assignment,
                        msg.lastrowid,
                        json.dumps(payload), int(blocking), stamp))
            if assignment:
                row = db.execute("SELECT * FROM assignments WHERE id=? AND status='active'",
                                 (assignment,)).fetchone()
                if row:
                    from .assignments import Assignments

                    Assignments(store)._set_status(db, row, "waiting_on_user",
                                                   f"Question {key} awaiting answer", actor=f"bot:{bot_id}",
                                                   job_id=str(job_id), announce=False)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return get(store, key)


def get(store, key):
    with closing(store._connect()) as db:
        return unpack(db.execute("SELECT * FROM questions WHERE id=?", (str(key),)).fetchone())


def answer(store, bot_id, key, body):
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM questions WHERE id=? AND bot_id=?",
                             (str(key), str(bot_id))).fetchone()
            if not row:
                raise LookupError("question not found")
            if row["status"] != "open":
                raise RuntimeError("question is already closed")
            value = validate_answer(json.loads(row["payload"]), body)
            stamp = now().isoformat()
            db.execute("UPDATE questions SET status=?,answer=?,answered_at=? WHERE id=?",
                       ("skipped" if value["skip"] else "answered", json.dumps(value),
                        stamp, str(key)))
            _restore(store, db, row)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return get(store, key)


def extract(text):
    found = []

    def replace(match):
        try:
            payload = validate(json.loads(match.group(1)))
        except (ValueError, TypeError):
            return match.group(0)
        found.append(payload)
        return ""
    cleaned = re.sub(r"```ask\s*\n(.*?)\n```", replace, text, flags=re.DOTALL)
    return cleaned.strip() or ("Asked a question." if found else ""), found

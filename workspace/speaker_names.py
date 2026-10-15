# workspace/speaker_names.py
# Speaker naming and attribution corrections (#21).
#
# Corrections are an append-only log per speaker run (speaker_corrections):
#   name   - give a machine label a name ("SPEAKER_01" -> "Aroha")
#   merge  - treat one label as another (the pass split one person in two)
#   assign - reassign one passage to a label (a "new" label for a person the
#            pass missed is allowed: "NEW_1", "NEW_2"...)
# The current view is the log replayed over the machine attribution, so undo
# removes the last entry and everything survives restart. Machine attribution
# (segment_speakers) is never changed.
#
# Names apply to this meeting only: there is no cross-meeting list, no
# autocomplete and no voiceprint. A new speaker pass ("identify again") starts
# with an empty log; the old run's corrections stay with that run. All of it is
# removed with the transcript at cleanup (#17).

import time

MAX_NAME = 80
MIN_CLIP_S, MAX_CLIP_S = 1.5, 15.0     # representative passages: long enough to judge
MAX_FALLBACK_S = 30.0                  # longest passage played whole when nothing better
EXAMPLES = 3


class NamingError(ValueError):
    """A refused correction (message is safe to show)."""


def current_run(store, vid):
    return store._one("SELECT * FROM speaker_runs WHERE version_id = ? AND status = 'done' "
                      "ORDER BY started_at DESC LIMIT 1", (vid,))


def _log(store, rid):
    return store._all("SELECT * FROM speaker_corrections WHERE run_id = ? ORDER BY id", (rid,))


def resolve(store, vid):
    """The corrected view: {"labels": {segment_id: final_label},
    "names": {final_label: display name}, "overlap": {segment_id: bool},
    "order": [final labels in order of first appearance]}."""
    run = current_run(store, vid)
    if run is None:
        return {"labels": {}, "names": {}, "overlap": {}, "order": [], "run": None}
    machine = {r["segment_id"]: (r["label"], bool(r["overlap"])) for r in store._all(
        "SELECT * FROM segment_speakers WHERE run_id = ?", (run["id"],))}
    labels = {sid: lbl for sid, (lbl, _) in machine.items()}
    merged, named, merge_order = {}, {}, []
    log = _log(store, run["id"])
    for c in log:
        if c["kind"] == "assign":
            labels[int(c["target"])] = c["value"]
        elif c["kind"] == "merge":
            merged[c["target"]] = c["value"]
            merge_order.append(c["target"])
        elif c["kind"] == "name":
            named[c["target"]] = c["value"]

    def final(lbl, seen=()):
        while lbl in merged and lbl not in seen:
            seen = (*seen, lbl)
            lbl = merged[lbl]
        return lbl
    segs = store._all("SELECT id FROM segments WHERE version_id = ? ORDER BY start_s, id",
                      (vid,))
    out, order, first_seen = {}, [], []
    for s in segs:
        machine_lbl = machine.get(s["id"], (None,))[0]
        if machine_lbl and machine_lbl not in first_seen:
            first_seen.append(machine_lbl)
        lbl = final(labels.get(s["id"])) if labels.get(s["id"]) else None
        out[s["id"]] = lbl
        if lbl and lbl not in order:
            order.append(lbl)
    # Default numbers stay stable while the user corrects things: machine
    # speakers are numbered by first appearance, new speakers after them.
    added = [c["value"] for c in log if c["kind"] == "assign" and c["value"] not in first_seen]
    numbering = {lbl: i + 1 for i, lbl in enumerate(dict.fromkeys(first_seen + added))}
    names = {}
    for lbl in order:
        # A person keeps their own name; if they have none, a name given to
        # someone merged into them follows (the most recently merged wins).
        given = named.get(lbl) or next((named[o] for o in reversed(merge_order)
                                        if o in named and final(o) == lbl), None)
        names[lbl] = given or f"Speaker {numbering.get(lbl, len(numbering) + 1)}"
    return {"labels": out, "names": names, "order": order, "run": run["id"],
            "overlap": {sid: ov for sid, (_, ov) in machine.items()}}


def _append(store, rid, kind, target, value):
    with store.tx() as db:
        db.execute("INSERT INTO speaker_corrections (run_id, kind, target, value, at) "
                   "VALUES (?, ?, ?, ?, ?)", (rid, kind, str(target), value, time.time()))


def _run_or_refuse(store, vid):
    run = current_run(store, vid)
    if run is None:
        raise NamingError("Speakers have not been identified for this meeting.")
    return run["id"]


def rename(store, vid, label, name):
    view = resolve(store, vid)
    rid = _run_or_refuse(store, vid)
    if label not in view["order"]:
        raise NamingError("That speaker is no longer in the transcript.")
    name = " ".join((name or "").split())[:MAX_NAME]
    if not name:
        raise NamingError("Enter a name.")
    _append(store, rid, "name", label, name)


def merge(store, vid, label, into):
    view = resolve(store, vid)
    rid = _run_or_refuse(store, vid)
    if label == into or label not in view["order"] or into not in view["order"]:
        raise NamingError("Choose two different speakers.")
    _append(store, rid, "merge", label, into)


def assign(store, vid, segment_id, label):
    """Reassign one passage. label may be an existing speaker or "new"."""
    view = resolve(store, vid)
    rid = _run_or_refuse(store, vid)
    if segment_id not in view["labels"]:
        raise NamingError("That passage is not in the current transcript.")
    if label == "new":
        used = {lbl for lbl in view["order"]} | {
            c["value"] for c in _log(store, rid) if c["kind"] == "assign"}
        n = 1
        while f"NEW_{n}" in used:
            n += 1
        label = f"NEW_{n}"
    elif label not in view["order"]:
        raise NamingError("Choose a speaker from this meeting.")
    _append(store, rid, "assign", segment_id, label)
    return label


def undo(store, vid):
    """Remove the latest correction. Returns its kind, or None if there is none."""
    run = current_run(store, vid)
    if run is None:
        return None
    with store.tx() as db:
        last = db.execute("SELECT id, kind FROM speaker_corrections WHERE run_id = ? "
                          "ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
        if last is None:
            return None
        db.execute("DELETE FROM speaker_corrections WHERE id = ?", (last["id"],))
        return last["kind"]


def _rows(store, vid):
    return store._all("SELECT s.id, s.start_s, s.end_s, COALESCE(e.text, s.text) AS text, "
                      "e.text IS NOT NULL AS edited, s.words_json FROM segments s "
                      "LEFT JOIN edits e ON e.segment_id = s.id WHERE s.version_id = ? "
                      "ORDER BY s.start_s", (vid,))


def _excerpt(r):
    """(start, end, text) where the clip plays exactly what the text shows.
    Passages up to MAX_FALLBACK_S play whole; a longer one plays its first
    MAX_CLIP_S with only the words spoken in that time (needs word times,
    and is not possible for an edited passage)."""
    if r["end_s"] - r["start_s"] <= MAX_FALLBACK_S:
        return r["start_s"], r["end_s"], r["text"]
    if r["edited"] or not r["words_json"]:
        return None
    import json
    cut = r["start_s"] + MAX_CLIP_S
    words = [w for w in json.loads(r["words_json"]) if w[1] <= cut]
    if not words:
        return None
    return r["start_s"], words[-1][1], "".join(w[2] for w in words).strip() + " …"


def examples(store, vid, label, offset=0, count=EXAMPLES, view=None, rows=None):
    """Representative passages for one speaker: clean (no overlap), long
    enough to recognise a voice, spread across the meeting. Each clip plays
    exactly the text shown (same start and end). offset pages through More
    examples."""
    view = view or resolve(store, vid)
    rows = rows if rows is not None else _rows(store, vid)
    theirs = [r for r in rows if view["labels"].get(r["id"]) == label]
    mine = [(r["start_s"], r["end_s"], r["text"], r["id"]) for r in theirs
            if not view["overlap"].get(r["id"])
            and MIN_CLIP_S <= r["end_s"] - r["start_s"] <= MAX_CLIP_S]
    if not mine:       # only short, long or overlapping speech: use what can be shown exactly
        mine = [(*ex, r["id"]) for r in theirs if (ex := _excerpt(r))]
    # The first page is spread evenly over the meeting; More examples
    # continues with the remaining passages in time order.
    if len(mine) > count:
        if count > 1:
            first = [mine[round(i * (len(mine) - 1) / (count - 1))] for i in range(count)]
        else:
            first = [mine[len(mine) // 2]]
        mine = first + [m for m in mine if m not in first]
    page = mine[offset:offset + count]
    return {"total": len(mine), "items": [
        {"id": sid, "start": a, "end": b, "text": text} for a, b, text, sid in page]}


def panel(store, vid, offsets=None):
    """Everything the Name speakers panel shows, from one resolve."""
    view = resolve(store, vid)
    rows = _rows(store, vid)
    out = summary(store, vid, view)
    for sp in out["speakers"]:
        off = (offsets or {}).get(sp["label"], 0)
        sp["examples"] = examples(store, vid, sp["label"], off, view=view, rows=rows)
        sp["offset"] = off
    return out


def given_names(store, vid, view=None):
    """Every name each speaker has had in the current identification: the
    current one, earlier ones (renamed) and those of speakers merged into
    them, by final label. Used to replace names in a handoff."""
    view = view or resolve(store, vid)
    out = {lbl: [name] for lbl, name in view["names"].items()}
    if view["run"] is None:
        return out
    log = _log(store, view["run"])
    merged = {c["target"]: c["value"] for c in log if c["kind"] == "merge"}

    def final(lbl, seen=()):
        while lbl in merged and lbl not in seen:
            seen = (*seen, lbl)
            lbl = merged[lbl]
        return lbl
    for c in log:
        if c["kind"] == "name":
            out.setdefault(final(c["target"]), []).append(c["value"])
    return out


def summary(store, vid, view=None):
    """Speakers with names, passage counts and seconds of speech, for the Name
    speakers panel and the meeting's Speakers tab."""
    view = view or resolve(store, vid)
    counts, seconds = {}, {}
    for row in store._all("SELECT id, start_s, end_s FROM segments WHERE version_id = ?",
                          (vid,)):
        lbl = view["labels"].get(row["id"])
        if lbl:
            counts[lbl] = counts.get(lbl, 0) + 1
            seconds[lbl] = seconds.get(lbl, 0.0) + max(0.0, row["end_s"] - row["start_s"])
    run = view["run"]
    can_undo = bool(run) and store._one("SELECT 1 FROM speaker_corrections WHERE run_id = ? "
                                        "LIMIT 1", (run,)) is not None
    return {"speakers": [{"label": lbl, "name": view["names"][lbl],
                          "passages": counts.get(lbl, 0),
                          "seconds": round(seconds.get(lbl, 0.0), 1)} for lbl in view["order"]],
            "can_undo": can_undo}


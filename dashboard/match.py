"""Which task-list step each piece of live work belongs to, and what state each step is really in.

Pure functions over the snapshot's own data, so they are cheap and easy to test. A step is matched by the names the
lead writes in it: a lane's branch or name ("map-gen-pilot", "search2"), a train's number ("train 7", "train7"), an
agent's label. Each run goes to at most one step: the best match, and only a step it overlaps in time (a run that
ended before a step was written, or started after the step was ticked, is some other step's work). On a tie a live
run goes to the step in progress, then to a step ticked while it ran, and a finished run to the step ticked while it
ran. The lead's own value stays in item["status"]; the evidence goes to item["derived"], which is display only.
"""
import re

WORD = re.compile(r"[a-z0-9]+")
IDENT = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)+")
TRAIN_REF = re.compile(r"\btrain[\s#_-]*(\d+)")
# words that name the machinery rather than the work: never enough on their own to tie work to a step
GENERIC = {
    "a", "an", "and", "the", "of", "to", "in", "on", "for", "with", "from", "by", "at", "or", "is", "it", "its",
    "branch", "run", "runs", "lane", "lanes", "train", "task", "step", "work", "job", "jobs", "wf", "workflow", "script",
    "fix", "fixes", "build", "builder", "finish", "merge", "check", "test", "tests", "review", "critic", "critics",
    "plan", "planner", "skeptic", "verify", "resume", "retry", "rerun", "new", "pass", "part", "copy", "main",
}
NAME_PREFIXES = ("branch-run-", "train-run-", "train-merge-", "lane-", "run-")
STRONG, MEDIUM, MIN_SCORE = 80, 50, 40
GRACE_MS = 60000
LIVE = ("running", "idle")


def words(text):
    """Lower-case words, with a word of letters then digits (or digits then letters) split too, so "train7" also
    gives "train" and "7". A hash such as 55098b37 is not split: its "b" is not a word."""
    out = set()
    for w in WORD.findall(str(text or "").lower()):
        out.add(w)
        parts = re.findall(r"[a-z]+|\d+", w)
        if len(parts) == 2:
            out.update(parts)
    return out


def idents(text):
    """Hyphenated or underscored names in the text, as written and joined: map-gen-pilot and mapgenpilot."""
    out = set()
    for m in IDENT.findall(str(text or "").lower()):
        out.add(m)
        out.add(re.sub(r"[-_]", "", m))
    return out


class Step:
    """One task-list item, with the word sets it is matched against."""

    def __init__(self, index, item):
        self.index, self.item = index, item
        text = item.get("content") or ""
        self.words, self.idents = words(text), idents(text)
        self.trains = {int(n) for n in TRAIN_REF.findall(text.lower())}
        # the name a step is about, when it starts with one: "help-menu: keep the menu top-left" is help-menu's step
        self.head = text.split(":", 1)[0].strip().lower() if ":" in text else ""
        self.status = item.get("status") or "pending"
        self.since = int(item.get("since") or 0)
        self.done_at = int(item.get("doneAt") or 0)
        self.by = item.get("by") or ""


def strip_name(name):
    """branch-run-mapgen -> mapgen: the generic script prefix says nothing about which lane it is."""
    n = str(name or "").lower()
    for p in NAME_PREFIXES:
        if n.startswith(p) and len(n) > len(p) + 1:
            return n[len(p):]
    return n


def title_head(title):
    """"jump-hold: a held jump climbs" -> "jump-hold" (the name before the colon, when it looks like one)."""
    head = str(title or "").split(":", 1)[0].strip().lower()
    return head if head and len(head) <= 40 and " " not in head else ""


def train_number(run):
    m = re.match(r"\s*(\d+)", str((run.get("train") or {}).get("k") or ""))
    if m:
        return int(m.group(1))
    for v in (run.get("branch"), run.get("lane"), run.get("name"), run.get("title")):
        m = TRAIN_REF.search(str(v or "").lower())
        if m:
            return int(m.group(1))
    return None


def key_score(key, weight, step, own=True):
    """How well one name of a piece of work matches a step: the full weight for the name as written (or joined up),
    0.8 of it when every distinctive part of the name is a word of the step, 0 otherwise. Only a run's own names
    (not a name its description mentions, nor a side agent's or command's prose) may also match on their one
    distinctive word, or by being what the step is about."""
    key = str(key or "").lower().strip()
    if own == "words":
        return words_score(key, weight, step)
    if own and key and key == step.head:
        return weight  # the step is about this name ("hud: fix the status bar"), however short or plain the name
    parts = WORD.findall(key)
    # unique parts ("end-to-end" is one word, not two), and a one-letter last part counts, even "a": smoke-a is not
    # smoke-b
    last = len(parts) - 1
    distinct = list(dict.fromkeys(p for k, p in enumerate(parts)
                                  if (k == last and k > 0 and len(p) == 1 and p.isalpha()) or (p not in GENERIC and len(p) >= 2)))
    if not distinct:
        return 0
    joined = "".join(parts)
    if len(parts) > 1 and (key in step.idents or joined in step.idents or joined in step.words):
        return weight
    if len(parts) == 1:
        return weight * 0.8 if len(joined) >= 4 and joined in step.words else 0
    if len(distinct) >= 2 and all(p in step.words for p in distinct):
        return weight * 0.8
    if own and len(distinct) == 1 and len(distinct[0]) >= 4 and distinct[0] in step.words:
        return weight * 0.6  # "branch-run-docs": the one distinctive part is the whole name
    return 0


def words_score(text, weight, step):
    """A run's title in other words: the weight when at least three of its distinctive words, and most of them, are
    words of the step (a step that paraphrases what the lane does)."""
    distinct = list(dict.fromkeys(w for w in WORD.findall(text) if w not in GENERIC and len(w) >= 4))
    hits = sum(1 for w in distinct if w in step.words)
    return weight if len(distinct) >= 3 and hits >= 3 and hits >= 0.6 * len(distinct) else 0


def run_names(run):
    """(name, weight, own) triples that identify a workflow run, most specific first: its own names (its id, which
    leads sometimes write into a step, its branch, lane, title head; the workflow's name, often a short script name
    such as branch-run-docs, only when the args give no better one), then names that only mention it, then its title's
    words (stronger when the run has no name of its own that says anything, like a lane called "train")."""
    out = [(run.get("id"), 110, True), (run.get("branch"), 100, True), (run.get("lane"), 95, True), (title_head(run.get("title")), 90, True)]
    if not run.get("branch") and (run.get("lane") or "") == (run.get("name") or ""):
        out.append((strip_name(run.get("name")), 85, True))
    plain = not any(set(WORD.findall(str(k).lower())) - GENERIC for k, _, _ in out[1:] if k)
    for a in run.get("agents") or []:
        label = [w for w in WORD.findall(str(a.get("label") or "").lower()) if w not in GENERIC and len(w) >= 4]
        if label:
            out.append(("-".join(label), 40, False))
    k = (run.get("train") or {}).get("k")
    for m in idents(str(run.get("title") or "") + " " + str(k or "")):
        if "-" in m or "_" in m:
            out.append((m, 50, False))  # a name the run's description mentions: weaker than its own names
    if run.get("title"):
        out.append((str(run["title"]).split(":", 1)[-1], 55 if plain else 45, "words"))
    return [x for x in out if x[0]]


def prose_names(text):
    """Names for a side agent or a background command: the hyphenated names in its description, then all of its
    distinctive words together (only when there are two to five of them). Prose never matches on one word."""
    out = [(m, 90, False) for m in idents(text) if "-" in m or "_" in m]
    distinct = list(dict.fromkeys(w for w in WORD.findall(str(text or "").lower()) if w not in GENERIC and len(w) >= 4))
    if 2 <= len(distinct) <= 5:
        out.append(("-".join(distinct), 65, False))
    return out


def score(names, step, train_no=None):
    """The best single name's score, plus a little for every other name that also matches (so a step that names
    more of a run's identity wins a tie), plus more when the step is about one of its own names (its head)."""
    hits = [key_score(k, w, step, own) for k, w, own in names]
    rid = next((str(k).lower() for k, _, own in names if own is True and str(k).lower().startswith("wf_")), "")
    if rid and any(i.startswith("wf_") and len(i) >= 9 and rid.startswith(i) for i in step.idents):
        hits.append(110)  # the step names the run by (a prefix of) its id
    if train_no is not None and train_no in step.trains:
        hits.append(100)
    hits = sorted((h for h in hits if h), reverse=True)
    if not hits:
        return 0
    head = 15 if step.head and any(own is True and str(k or "").lower().strip() == step.head for k, _, own in names) else 0
    return hits[0] + min(20, 5 * (len(hits) - 1)) + head


def own_match(names, step):
    """Whether the step names the work by one of its own names (not a mention, nor prose), even in short: a summary
    line "smoke a, c, d: done" names smoke-a."""
    return any(own is True and key_score(k, w, step, own) >= 0.8 * w for k, w, own in names)


def overlaps(start, end, live, step, own=False):
    """Work counts for a step only if it overlaps the step's life: it did not end before the step was written, and it
    did not start after the step was ticked done. Two exceptions, both only for a step that names the work by its own
    name: a done step written after the work ended (a summary line such as "x, y and z done") keeps it, and a tick
    from the end of a turn (wrap_up) does not close a step to a live run, since the next turn often starts or
    resumes that step's lane."""
    if not live and end and step.since and end < step.since - GRACE_MS and not (own and step.status == "completed"):
        return False
    if (step.status == "completed" and step.done_at and start and start > step.done_at
            and not (live and own and step.by == "wrap_up")):
        return False  # started after the tick (the next step's work, as when `advance` ticks and the next lane starts)
    return True


def rank(step, live, start):
    """Only breaks a tie in score. A live run: the step in progress, or a step ticked while it clearly ran (ticked by
    the end of a turn after the run began, or ticked well after it began; a done line rewritten seconds after a launch
    is not that run's step), then any other ticked step, then a step not started, which may only mention it. A
    finished run: the step ticked while it ran, then the step in progress, then a step ticked before it started, then
    a step not started."""
    done = step.status == "completed"
    if live:
        during = done and (not start or not step.done_at or step.done_at >= start + GRACE_MS
                           or (step.by == "wrap_up" and step.done_at >= start))
        return 3 if step.status == "in_progress" or during else 2 if done else 1
    if done:
        return 3 if not start or not step.done_at or start <= step.done_at else 1
    return 2 if step.status == "in_progress" else 0


def best_step(steps, names, start, end, live, train_no=None, min_score=MIN_SCORE):
    ranked, late = [], []
    for s in steps:
        sc = score(names, s, train_no)
        if sc < min_score or not overlaps(start, end, live, s, own_match(names, s) or (train_no is not None and train_no in s.trains)):
            continue
        bucket = 2 if sc >= STRONG else 1 if sc >= MEDIUM else 0
        entry = (bucket, sc, rank(s, live, start), -s.index, s)
        # a step ticked by the end of a turn before this run began is its home only when no other step names it
        (late if s.status == "completed" and s.done_at and start and start > s.done_at else ranked).append(entry)
    pick = ranked or late
    return max(pick, key=lambda x: x[:4])[4] if pick else None


def derive(items, runs, side=(), tasks=(), plan_updated=0):
    """Fill item["derived"] for every task-list item from the runs, side agents and background tasks of the same
    session, and return the live work no item names: {"runs": [...ids], "side": [...], "tasks": [...]}.

    Only steps the lead has started get a state from the evidence: a step in progress or ticked done whose work is
    live is "running"; a step in progress whose lanes have all ended is "finished" (or "failed", "stopped") only when
    they ended after the lead last changed the list, since a lead that changed the list after that and kept the step
    open knows something the lanes do not. A step not started yet keeps its own state: the live work it names is
    listed with it (derived.live) but it is not shown as running, because it may only mention that lane."""
    steps = [Step(i, it) for i, it in enumerate(items)]
    got = {s.index: {"runs": [], "side": [], "tasks": [], "live": 0} for s in steps}
    lanes = {s.index: {} for s in steps}  # each step's lanes, newest run of each: a retry that worked outranks a failed try
    other = {"runs": [], "side": [], "tasks": []}

    def place(kind, ident, names, start, end, state, train_no=None, min_score=MIN_SCORE, lane=None):
        live = state in LIVE
        s = best_step(steps, names, start, end, live, train_no, min_score) if steps else None
        if s is None:
            if live:
                other[kind].append(ident)
            return
        got[s.index][kind].append(ident)
        if lane is not None and (lane not in lanes[s.index] or start >= lanes[s.index][lane][0]):
            lanes[s.index][lane] = (start, state, end or 0)
        if live:
            got[s.index]["live"] += 1

    for r in runs:
        # a resumed run is matched by when its first attempt began, so the step it began under still owns it
        place("runs", r.get("id"), run_names(r), r.get("firstStart") or r.get("start") or 0, r.get("end") or 0, r.get("state"),
              train_number(r) if r.get("isTrain") else None, lane=r.get("branch") or r.get("lane") or r.get("id"))
    # prose is a weaker signal than a lane's own name: side agents and commands need a medium match
    for a in side:
        place("side", a.get("id"), prose_names(a.get("description")), a.get("start") or 0, a.get("end") or 0, a.get("state"), min_score=MEDIUM)
    for t in tasks:
        place("tasks", t.get("id"), prose_names(t.get("title")), t.get("start") or 0, t.get("end") or 0, t.get("state"), min_score=MEDIUM)

    for s in steps:
        d = dict(got[s.index], status="", note="")
        newest = list(lanes[s.index].values())
        sts = [x[1] for x in newest]
        if d["live"] and s.status in ("in_progress", "completed"):
            d["status"] = "running"
            if s.status == "completed":
                d["note"] = "marked done, still running"
        elif newest and not d["live"] and s.status == "in_progress" and max(x[2] for x in newest) > (plan_updated or 0):
            if "failed" in sts:
                d["status"], d["note"] = "failed", "lane failed"
            elif "killed" in sts:
                d["status"], d["note"] = "stopped", "lane stopped"
            else:
                d["status"], d["note"] = "finished", "lane finished, step not ticked"
        s.item["derived"] = d
    return other

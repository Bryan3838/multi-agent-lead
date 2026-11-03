"""Behaviour tests for the dashboard collector: task-list replay, which step each piece of work belongs to, session
choice, background commands and side agents. Every record here is synthetic (no real project's data).

Run from the dashboard folder:  python -m unittest discover -s tests -v
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import live  # noqa: E402
import match  # noqa: E402
import snapshot_tree  # noqa: E402
import status  # noqa: E402

MIN = 60000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def use(uid, name, inp):
    return {"type": "tool_use", "id": uid, "name": name, "input": inp}


def result(uid, text, error=False):
    return {"type": "tool_result", "tool_use_id": uid, "content": text, **({"is_error": True} if error else {})}


def notification(task_id, use_id, state, summary="", result_text=""):
    return ("<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>%s</status>\n<summary>%s</summary>\n%s</task-notification>"
            % (task_id, use_id, state, summary, "<result>%s</result>\n" % result_text if result_text else ""))


class Session:
    """One synthetic session: a transcript plus its workflow runs and side agents on disk."""

    def __init__(self, project_dir, sid, start_ms):
        self.dir, self.sid, self.at, self.n = project_dir / sid, sid, start_ms, 0
        self.path = project_dir / (sid + ".jsonl")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.write({"type": "user", "timestamp": iso(start_ms), "message": {"role": "user", "content": "Build the widget lanes"}})

    def write(self, obj):
        with open(self.path, "a", encoding="utf8") as f:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")  # compact, as Claude Code writes its transcripts

    def assistant(self, at, *blocks):
        self.write({"type": "assistant", "timestamp": iso(at), "message": {"role": "assistant", "model": "claude-sonnet-5-5", "content": list(blocks)}})

    def user(self, at, *blocks):
        self.write({"type": "user", "timestamp": iso(at), "message": {"role": "user", "content": list(blocks)}})

    def uid(self):
        self.n += 1
        return "toolu_%s_%d" % (self.sid[:4], self.n)

    def plan(self, at, items):
        self.assistant(at, use(self.uid(), "mcp__ccd_turn__update_plan", {"items": [{"content": c, "status": s} for c, s in items]}))

    def call(self, at, name, inp):
        u = self.uid()
        self.assistant(at, use(u, name, inp))
        return u

    def launch(self, at, run_id, args=None, script_path="", name="branch-run"):
        """A Workflow launch (args passed, or baked into the script at script_path) and its 'Workflow launched' result."""
        u = self.call(at, "Workflow", {"scriptPath": script_path} if script_path else {"name": name, "args": args or {}})
        self.user(at + 2000, result(u, "Workflow launched in background. Task ID: w%s\nSummary: Finish one branch\nScript file: %s\nRun ID: %s\n"
                                    % (run_id[-4:], script_path or "C:/scratch/%s-%s.js" % (name, run_id), run_id)))
        return u

    def live_agent(self, run_id, agent_id, label, phase="Build", when=None):
        d = self.dir / "subagents" / "workflows" / run_id
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "journal.jsonl", "a", encoding="utf8") as f:
            f.write(json.dumps({"type": "started", "agentId": agent_id, "label": label, "phase": phase}) + "\n")
        (d / ("agent-%s.meta.json" % agent_id)).write_text(json.dumps({"model": "claude-haiku-5-5"}), encoding="utf8")
        t = d / ("agent-%s.jsonl" % agent_id)
        t.write_text(json.dumps({"type": "assistant", "timestamp": iso(when or time.time() * 1000), "message": {"model": "claude-haiku-5-5", "content": [
            use("toolu_x", "Bash", {"command": "cd /c/work/repo && npm test", "description": "Run the unit tests"})]}}) + "\n", encoding="utf8")
        return t

    def finished_run(self, run_id, start_ms, end_ms, args=None, name="branch-run", script=None, result_obj=None, state="done"):
        wf = self.dir / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        rec = {"runId": run_id, "workflowName": name, "status": "completed", "startTime": start_ms, "args": args or {},
               "result": result_obj or {}, "workflowProgress": [{"type": "workflow_agent", "agentId": "a-" + run_id, "label": "build", "phaseTitle": "Build",
                                                                   "state": state, "startedAt": start_ms, "lastProgressAt": end_ms, "durationMs": end_ms - start_ms}]}
        if script is not None:
            rec["script"] = script
        p = wf / (run_id + ".json")
        p.write_text(json.dumps(rec), encoding="utf8")
        os.utime(p, (end_ms / 1000, end_ms / 1000))

    def side_agent(self, agent_id, use_id, description, finished=False):
        sub = self.dir / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / ("agent-%s.meta.json" % agent_id)).write_text(json.dumps({"agentType": "general-purpose", "description": description, "toolUseId": use_id,
                                                                         "requestShape": "background", **({"finished": True} if finished else {})}), encoding="utf8")
        (sub / ("agent-%s.jsonl" % agent_id)).write_text(json.dumps({"type": "assistant", "timestamp": iso(time.time() * 1000), "message": {
            "model": "claude-sonnet-5-5", "content": [use("toolu_y", "Read", {"file_path": "C:/work/repo/src/app.py"})]}}) + "\n", encoding="utf8")

    def age(self, minutes):
        """Make every file of the session look untouched for this many minutes."""
        t = time.time() - minutes * 60
        for p in [self.path] + [x for x in self.dir.rglob("*")]:
            os.utime(p, (t, t))


class Base(unittest.TestCase):
    def setUp(self):
        for cache in (status._logs, status._slow, status._summaries, status._journals, status._scripts, status._touched,
                      live._head_cache, live._token_cache, live._tail_cache, live._continued):
            cache.clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="dash-test-"))
        self.project = self.tmp / "repo"
        self.project.mkdir()
        self.claude = self.tmp / "claude"
        self.pdir = self.claude / "projects" / status.project_slug(self.project.resolve())
        self.pdir.mkdir(parents=True)
        self.now = int(time.time() * 1000)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def session(self, sid, start_ms):
        return Session(self.pdir, sid, start_ms)

    def collect(self, *extra):
        ap = argparse.ArgumentParser()
        status.add_arguments(ap)
        opts = ap.parse_args(["--project", str(self.project), "--claude-dir", str(self.claude),
                              "--plan-usage-history", str(self.tmp / "none.json")] + list(extra))
        return status.collect(opts)

    def item(self, snap, prefix):
        return next(i for i in snap["plan"]["items"] if i["content"].startswith(prefix))

    def lanes(self, snap, item):
        by = {r["id"]: r["lane"] for r in snap["runs"]}
        return [by[x] for x in item["derived"]["runs"]]


class TaskListReplay(Base):
    def test_wrap_up_with_a_live_lane_still_shows_running(self):
        s = self.session("s1aaaaaa-1", self.now - 30 * MIN)
        s.plan(self.now - 29 * MIN, [("alpha-beta: build the widget", "in_progress"), ("ship it", "pending")])
        s.launch(self.now - 28 * MIN, "wf_aaa111", {"br": "alpha-beta", "title": "alpha-beta: build the widget"})
        s.live_agent("wf_aaa111", "a1", "builder")
        s.call(self.now - 5 * MIN, "mcp__ccd_turn__wrap_up", {"recap": "the lane runs on"})
        snap = self.collect()
        it = self.item(snap, "alpha-beta")
        self.assertEqual(it["status"], "completed")  # the lead's own value is kept as replayed
        self.assertEqual(it["by"], "wrap_up")
        self.assertEqual(it["derived"]["status"], "running")
        self.assertEqual(it["derived"]["note"], "marked done, still running")
        self.assertEqual(self.lanes(snap, it), ["alpha-beta"])

    def test_a_run_started_after_the_tick_belongs_to_the_next_step(self):
        s = self.session("s1aaaaaa-2", self.now - 60 * MIN)
        s.finished_run("wf_old001", self.now - 58 * MIN, self.now - 40 * MIN, {"br": "alpha-beta"})
        s.plan(self.now - 59 * MIN, [("alpha-beta first pass", "in_progress"), ("alpha-beta second pass", "pending")])
        s.call(self.now - 39 * MIN, "mcp__ccd_turn__update_plan", {"advance": True})
        s.launch(self.now - 20 * MIN, "wf_new002", {"br": "alpha-beta"})
        s.live_agent("wf_new002", "a2", "builder")
        snap = self.collect()
        first, second = self.item(snap, "alpha-beta first"), self.item(snap, "alpha-beta second")
        ids = {r["id"]: r for r in snap["runs"]}
        self.assertEqual(second["derived"]["runs"], ["wf_new002"])
        self.assertEqual(second["derived"]["status"], "running")
        self.assertNotIn("wf_new002", first["derived"]["runs"])
        self.assertEqual(first["derived"]["status"], "")  # done and nothing of it still running
        self.assertEqual(ids["wf_old001"]["state"], "done")

    def test_one_run_one_step_and_the_step_in_progress_wins(self):
        s = self.session("s1aaaaaa-3", self.now - 20 * MIN)
        s.plan(self.now - 19 * MIN, [("help-menu: menu stays top-left", "in_progress"), ("publish (add help-menu if it lands in time)", "pending")])
        s.launch(self.now - 18 * MIN, "wf_menu01", {"br": "help-menu"})
        s.live_agent("wf_menu01", "c1", "menu-build")
        snap = self.collect()
        self.assertEqual(self.item(snap, "help-menu")["derived"]["runs"], ["wf_menu01"])
        self.assertEqual(self.item(snap, "publish")["derived"]["runs"], [])

    def test_an_old_finished_run_does_not_finish_a_newer_step(self):
        s = self.session("s1aaaaaa-4", self.now - 120 * MIN)
        s.finished_run("wf_gam001", self.now - 110 * MIN, self.now - 90 * MIN, {"br": "gamma"})
        s.plan(self.now - 30 * MIN, [("gamma round two", "pending")])
        snap = self.collect()
        it = self.item(snap, "gamma")
        self.assertEqual(it["derived"]["runs"], [])
        self.assertEqual(it["derived"]["status"], "")

    def test_a_finished_lane_shows_on_a_step_left_open(self):
        s = self.session("s1aaaaaa-5", self.now - 60 * MIN)
        s.plan(self.now - 59 * MIN, [("delta: the thing", "in_progress")])
        s.finished_run("wf_del001", self.now - 50 * MIN, self.now - 10 * MIN, {"br": "delta"})
        snap = self.collect()
        it = self.item(snap, "delta")
        self.assertEqual((it["derived"]["status"], it["derived"]["note"]), ("finished", "lane finished, step not ticked"))

    def test_a_list_changed_after_the_lane_ended_is_trusted(self):
        s = self.session("s1aaaaaa-12", self.now - 60 * MIN)
        s.plan(self.now - 59 * MIN, [("kappa: build, merge and publish", "in_progress")])
        s.finished_run("wf_kap001", self.now - 50 * MIN, self.now - 30 * MIN, {"br": "kappa"})
        s.plan(self.now - 20 * MIN, [("kappa: build, merge and publish", "in_progress"), ("lambda", "pending")])
        it = self.item(self.collect(), "kappa")
        self.assertEqual(it["derived"]["runs"], ["wf_kap001"])
        self.assertEqual(it["derived"]["status"], "")  # the lead saw the lane end and kept the step open

    def test_a_step_not_started_is_never_shown_running(self):
        s = self.session("s1aaaaaa-13", self.now - 30 * MIN)
        s.plan(self.now - 29 * MIN, [("publish once mu-lane lands", "pending")])
        s.launch(self.now - 28 * MIN, "wf_mu0001", {"br": "mu-lane"})
        s.live_agent("wf_mu0001", "m1", "builder")
        snap = self.collect()
        it = self.item(snap, "publish")
        self.assertEqual((it["derived"]["runs"], it["derived"]["live"], it["derived"]["status"]), (["wf_mu0001"], 1, ""))

    def test_finished_steps_dropped_from_the_list_stay_as_history(self):
        s = self.session("s1aaaaaa-14", self.now - 60 * MIN)
        s.plan(self.now - 50 * MIN, [("nu: first", "completed"), ("xi: second", "in_progress")])
        s.plan(self.now - 40 * MIN, [("xi: second", "in_progress"), ("omicron: third", "pending")])
        snap = self.collect()
        self.assertEqual([i["content"] for i in snap["plan"]["done"]], ["nu: first"])
        self.assertEqual([i["content"] for i in snap["plan"]["items"]], ["xi: second", "omicron: third"])

    def test_a_rewritten_summary_line_is_not_history(self):
        s = self.session("s1aaaaaa-15", self.now - 60 * MIN)
        s.plan(self.now - 50 * MIN, [("Release 3 live (v12); pi done", "completed"), ("rho", "in_progress")])
        s.plan(self.now - 40 * MIN, [("Release 3 live (v12); pi, rho done", "completed"), ("sigma", "pending")])
        self.assertEqual(self.collect()["plan"]["done"], [])

    def test_a_reworded_step_keeps_when_it_started(self):
        s = self.session("s1aaaaaa-6", self.now - 60 * MIN)
        s.plan(self.now - 50 * MIN, [("epsilon: build the widget", "in_progress")])
        s.plan(self.now - 10 * MIN, [("epsilon: build the widget (phone too)", "in_progress")])
        snap = self.collect()
        self.assertEqual(self.item(snap, "epsilon")["since"], status.iso_ms(iso(self.now - 50 * MIN)))

    def test_task_tools_are_replayed(self):
        s = self.session("s1aaaaaa-7", self.now - 30 * MIN)
        u1 = s.call(self.now - 29 * MIN, "TaskCreate", {"subject": "zeta: first", "description": "d"})
        s.user(self.now - 29 * MIN, result(u1, "Task #1 created successfully: zeta: first"))
        u2 = s.call(self.now - 28 * MIN, "TaskCreate", {"subject": "zeta: second", "description": "d"})
        s.user(self.now - 28 * MIN, result(u2, "Task #2 created successfully: zeta: second"))
        s.call(self.now - 27 * MIN, "TaskUpdate", {"taskId": "1", "status": "in_progress"})
        s.call(self.now - 26 * MIN, "TaskUpdate", {"taskId": "2", "status": "deleted"})
        snap = self.collect()
        self.assertEqual([(i["content"], i["status"]) for i in snap["plan"]["items"]], [("zeta: first", "in_progress")])

    def test_task_ids_come_from_the_structured_output(self):
        s = self.session("s1aaaaaa-11", self.now - 30 * MIN)
        u = s.call(self.now - 29 * MIN, "TaskCreate", {"subject": "theta: build", "description": "d"})
        s.write({"type": "user", "timestamp": iso(self.now - 29 * MIN), "toolUseResult": {"task": {"id": "t-7", "subject": "theta: build"}},
                 "message": {"role": "user", "content": [result(u, "Task created")]}})
        s.call(self.now - 28 * MIN, "TaskUpdate", {"taskId": "t-7", "status": "completed"})
        self.assertEqual([(i["content"], i["status"]) for i in self.collect()["plan"]["items"]], [("theta: build", "completed")])

    def test_an_agent_of_the_earlier_attempt_is_not_shown_on_a_resumed_run(self):
        s = self.session("s1aaaaaa-16", self.now - 60 * MIN)
        s.plan(self.now - 59 * MIN, [("upsilon: the thing", "in_progress")])
        s.launch(self.now - 55 * MIN, "wf_ups001", {"br": "upsilon"})
        old = s.live_agent("wf_ups001", "r1", "builder")
        os.utime(s.dir / "subagents" / "workflows" / "wf_ups001" / "agent-r1.meta.json", ((self.now - 54 * MIN) / 1000,) * 2)
        os.utime(old, ((self.now - 45 * MIN) / 1000,) * 2)
        s.finished_run("wf_ups001", self.now - 55 * MIN, self.now - 40 * MIN, {"br": "upsilon"}, state="error")
        s.launch(self.now - 10 * MIN, "wf_ups001", {"br": "upsilon"})  # resumed
        s.live_agent("wf_ups001", "r2", "builder")
        run = next(r for r in self.collect()["runs"] if r["id"] == "wf_ups001")
        self.assertEqual([(a["id"], a["state"]) for a in run["agents"]], [("r2", "running")])

    def test_a_lane_resumed_after_the_end_of_turn_tick_stays_on_its_step(self):
        s = self.session("s1aaaaaa-17", self.now - 60 * MIN)
        s.plan(self.now - 59 * MIN, [("phi-lane: build", "in_progress")])
        s.finished_run("wf_phi001", self.now - 58 * MIN, self.now - 40 * MIN, {"br": "phi-lane"}, state="error")
        s.call(self.now - 35 * MIN, "mcp__ccd_turn__wrap_up", {"recap": "x"})
        s.launch(self.now - 10 * MIN, "wf_phi001", {"br": "phi-lane"})  # resumed after the tick
        s.live_agent("wf_phi001", "p2", "builder")
        it = self.item(self.collect(), "phi-lane")
        self.assertEqual((it["derived"]["runs"], it["derived"]["status"]), (["wf_phi001"], "running"))

    def test_a_retry_that_worked_outranks_the_failed_try(self):
        s = self.session("s1aaaaaa-18", self.now - 90 * MIN)
        s.plan(self.now - 89 * MIN, [("chi-lane: build", "in_progress")])
        s.finished_run("wf_chi001", self.now - 80 * MIN, self.now - 60 * MIN, {"br": "chi-lane"}, state="error")
        s.finished_run("wf_chi002", self.now - 50 * MIN, self.now - 10 * MIN, {"br": "chi-lane"})
        it = self.item(self.collect(), "chi-lane")
        self.assertEqual((it["derived"]["status"], it["derived"]["note"]), ("finished", "lane finished, step not ticked"))

    def test_a_reopened_step_does_not_take_its_old_run_as_finished(self):
        s = self.session("s1aaaaaa-19", self.now - 120 * MIN)
        s.plan(self.now - 119 * MIN, [("psi-lane: build", "in_progress")])
        s.finished_run("wf_psi001", self.now - 110 * MIN, self.now - 90 * MIN, {"br": "psi-lane"})
        s.call(self.now - 89 * MIN, "mcp__ccd_turn__update_plan", {"done": [1]})
        s.call(self.now - 10 * MIN, "mcp__ccd_turn__update_plan", {"start": [1]})
        it = self.item(self.collect(), "psi-lane")
        self.assertEqual((it["status"], it["derived"]["status"]), ("in_progress", ""))

    def test_a_notification_queued_before_its_launch_result_still_ends_the_command(self):
        s = self.session("s1aaaaaa-20", self.now - 30 * MIN)
        u = s.call(self.now - 20 * MIN, "Bash", {"command": "make", "description": "Build it", "run_in_background": True})
        s.write({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(self.now - 20 * MIN + 1500),
                 "content": notification("bz1", u, "completed", "done")})
        s.user(self.now - 20 * MIN + 2000, result(u, "Command running in background with ID: bz1. Output is being written to: C:/tmp/bz1.output"))
        self.assertEqual([(t["id"], t["state"]) for t in self.collect()["tasks"]], [("bz1", "done")])

    def test_a_summary_line_that_grew_a_lot_is_still_not_history(self):
        s = self.session("s1aaaaaa-21", self.now - 60 * MIN)
        s.plan(self.now - 50 * MIN, [("Release 4 live (v20); pi done", "completed"), ("rho", "in_progress")])
        s.plan(self.now - 40 * MIN, [("Release 4 live (v20); pi, rho, sigma and tau merged, phone pass checked, notes sent", "completed"),
                                     ("upsilon", "pending")])
        self.assertEqual(self.collect()["plan"]["done"], [])

    def test_a_step_reopened_by_number_starts_a_new_life(self):
        s = self.session("s1aaaaaa-22", self.now - 120 * MIN)
        s.plan(self.now - 119 * MIN, [("mu-panel: build", "in_progress")])
        s.finished_run("wf_mu0101", self.now - 110 * MIN, self.now - 90 * MIN, {"br": "mu-panel"})
        s.call(self.now - 89 * MIN, "mcp__ccd_turn__update_plan", {"done": [1]})
        s.call(self.now - 10 * MIN, "mcp__ccd_turn__update_plan", {"start": [1]})
        it = self.item(self.collect(), "mu-panel")
        self.assertEqual((it["status"], it["since"]), ("in_progress", status.iso_ms(iso(self.now - 10 * MIN))))
        self.assertEqual(it["derived"]["runs"], [])  # the run before the tick was the first life's work

    def test_a_new_step_like_one_still_listed_starts_its_own_life(self):
        s = self.session("s1aaaaaa-23", self.now - 60 * MIN)
        s.plan(self.now - 50 * MIN, [("nu-panel: build the widget", "in_progress")])
        s.plan(self.now - 10 * MIN, [("nu-panel: build the widget", "in_progress"), ("nu-panel: build the widget, phone", "pending")])
        since = {i["content"]: i["since"] for i in self.collect()["plan"]["items"]}
        self.assertEqual(since, {"nu-panel: build the widget": status.iso_ms(iso(self.now - 50 * MIN)),
                                 "nu-panel: build the widget, phone": status.iso_ms(iso(self.now - 10 * MIN))})

    def test_a_rejected_plan_call_is_undone(self):
        s = self.session("s1aaaaaa-9", self.now - 30 * MIN)
        s.plan(self.now - 29 * MIN, [("kept step", "in_progress")])
        u = s.call(self.now - 20 * MIN, "mcp__ccd_turn__update_plan", {"items": [{"content": "rejected step", "status": "pending"}]})
        s.user(self.now - 20 * MIN, result(u, "Error: the list could not be updated", error=True))
        self.assertEqual([i["content"] for i in self.collect()["plan"]["items"]], ["kept step"])

    def test_a_resumed_run_is_running_again(self):
        s = self.session("s1aaaaaa-10", self.now - 60 * MIN)
        s.plan(self.now - 59 * MIN, [("omega: the thing", "in_progress")])
        s.finished_run("wf_omega01", self.now - 50 * MIN, self.now - 40 * MIN, {"br": "omega"}, state="error")
        s.launch(self.now - 10 * MIN, "wf_omega01", {"br": "omega"})  # resumeFromRunId keeps the run id
        s.live_agent("wf_omega01", "r2", "builder")
        snap = self.collect()
        run = next(r for r in snap["runs"] if r["id"] == "wf_omega01")
        self.assertEqual((run["state"], run.get("resumed")), ("running", True))
        self.assertEqual([a["state"] for a in run["agents"]], ["running"])
        self.assertEqual(self.item(snap, "omega")["derived"]["status"], "running")

    def test_the_old_fields_stay_for_an_older_page(self):
        s = self.session("s1aaaaaa-8", self.now - 10 * MIN)
        s.plan(self.now - 9 * MIN, [("eta step", "pending")])
        snap = self.collect()
        self.assertEqual(snap["plan"]["items"][0]["content"], "eta step")
        self.assertEqual(snap["plan"]["items"][0]["status"], "pending")
        self.assertIn("updated", snap["plan"])


class LaneNames(Base):
    def test_args_baked_into_a_script_copy_name_the_lane(self):
        s = self.session("s2aaaaaa-1", self.now - 20 * MIN)
        script = self.tmp / "branch-run-mapgen.js"
        script.write_text("export const meta = { name: 'branch-run-mapgen' }\nconst A = " + json.dumps({"br": "map-gen-pilot", "title": "map-gen-pilot: a mesh"}) + "\n", encoding="utf8")
        s.plan(self.now - 19 * MIN, [("bridges, map-gen-pilot (next release)", "in_progress")])
        s.launch(self.now - 18 * MIN, "wf_mpg001", script_path=str(script))
        s.live_agent("wf_mpg001", "p1", "mapgen-build")
        snap = self.collect()
        run = next(r for r in snap["runs"] if r["id"] == "wf_mpg001")
        self.assertEqual((run["lane"], run["branch"]), ("map-gen-pilot", "map-gen-pilot"))
        self.assertEqual(self.item(snap, "bridges")["derived"]["runs"], ["wf_mpg001"])

    def test_a_finished_run_reads_its_baked_args_from_the_summary(self):
        s = self.session("s2aaaaaa-2", self.now - 20 * MIN)
        s.finished_run("wf_vh0001", self.now - 15 * MIN, self.now - 5 * MIN, name="branch-run-vh",
                       script="const A = " + json.dumps({"br": "jump-hold", "title": "jump-hold: a held jump climbs"}))
        snap = self.collect()
        self.assertEqual(snap["runs"][0]["lane"], "jump-hold")

    def test_the_result_names_a_run_with_no_args(self):
        s = self.session("s2aaaaaa-3", self.now - 20 * MIN)
        s.finished_run("wf_res001", self.now - 15 * MIN, self.now - 5 * MIN, name="inline-job", result_obj={"br": "ui-audit", "title": "ui-audit: screens"})
        snap = self.collect()
        self.assertEqual(snap["runs"][0]["lane"], "ui-audit")

    def test_a_baked_line_is_json_only(self):
        self.assertEqual(status.baked_args("const A = {\"br\": \"x\"}\n"), {"br": "x"})
        self.assertEqual(status.baked_args("const A = {br: require('child_process')}\n"), {})
        self.assertEqual(status.script_args(str(self.tmp / "missing.js")), {})
        notes = self.tmp / "notes.txt"
        notes.write_text("const A = {\"br\": \"x\"}\n", encoding="utf8")
        self.assertEqual(status.script_args(str(notes)), {})  # only workflow scripts are read

    def test_a_train_step_written_as_one_word(self):
        steps = [{"content": "train7 pass 2: merge, gate, smoke", "status": "in_progress", "since": 1}]
        run = {"id": "wf_t", "isTrain": True, "name": "train-run", "lane": "train-run", "branch": "", "title": "Merge ready branches",
               "train": {"k": "7"}, "state": "running", "start": 10, "end": 0, "agents": []}
        match.derive(steps, [run])
        self.assertEqual(steps[0]["derived"]["runs"], ["wf_t"])

    def lone(self, steps, run):
        match.derive(steps, [run])
        return [s["derived"]["runs"] for s in steps]

    def run_of(self, br, state="running", start=10, end=0):
        return {"id": "wf_" + br, "isTrain": False, "name": "branch-run", "lane": br, "branch": br, "title": br + ": work",
                "state": state, "start": start, "end": end, "agents": []}

    def test_a_live_run_goes_to_its_own_step_even_when_listed_second(self):
        steps = [{"content": "publish (add help-menu if it lands in time)", "status": "pending", "since": 1},
                 {"content": "help-menu: menu stays top-left", "status": "in_progress", "since": 1}]
        self.assertEqual(self.lone(steps, self.run_of("help-menu")), [[], ["wf_help-menu"]])

    def test_after_the_end_of_turn_tick_a_live_lane_keeps_its_own_step(self):
        steps = [{"content": "v6-fixes: settings top line", "status": "completed", "since": 1, "doneAt": 50, "by": "wrap_up"},
                 {"content": "V6: merge v6-fixes, checks, publish", "status": "in_progress", "since": 1}]
        self.assertEqual(self.lone(steps, self.run_of("v6-fixes")), [["wf_v6-fixes"], []])
        self.assertEqual(steps[0]["derived"]["status"], "running")

    def test_a_run_started_after_its_only_step_was_ticked_is_not_that_steps_work(self):
        steps = [{"content": "gamma-lane: first pass", "status": "completed", "since": 1, "doneAt": 1000}]
        other = match.derive(steps, [self.run_of("gamma-lane", start=1000 + match.GRACE_MS + 5000)])
        self.assertEqual((steps[0]["derived"]["runs"], steps[0]["derived"]["status"], other["runs"]), ([], "", ["wf_gamma-lane"]))

    def test_a_finished_run_stays_on_the_step_it_ran_under(self):
        steps = [{"content": "alpha-beta first pass", "status": "completed", "since": 1, "doneAt": 1000},
                 {"content": "alpha-beta second pass", "status": "in_progress", "since": 1}]
        self.assertEqual(self.lone(steps, self.run_of("alpha-beta", "done", start=1000 + 20000, end=5000000)), [[], ["wf_alpha-beta"]])

    def test_a_repeated_word_is_one_word(self):
        steps = [{"content": "Phone pass: check the end screen", "status": "in_progress", "since": 1}]
        other = match.derive(steps, [], tasks=[{"id": "b1", "title": "Run the end-to-end tests", "state": "running", "start": 10, "end": 0}])
        self.assertEqual((steps[0]["derived"]["tasks"], other["tasks"]), ([], ["b1"]))

    def test_a_step_that_names_the_run_id_owns_it(self):
        steps = [{"content": "publish once the lane lands", "status": "pending", "since": 1},
                 {"content": "watch wf_ab12cd34 (the rerun) and merge", "status": "in_progress", "since": 1}]
        run = dict(self.run_of("lane"), id="wf_ab12cd34-5e6")
        self.assertEqual(self.lone(steps, run), [[], ["wf_ab12cd34-5e6"]])

    def test_a_one_letter_suffix_tells_lanes_apart(self):
        steps = [{"content": "smoke-b: the phone pass", "status": "in_progress", "since": 1}]
        other = match.derive(steps, [self.run_of("smoke-c")])
        self.assertEqual((steps[0]["derived"]["runs"], other["runs"]), ([], ["wf_smoke-c"]))

    def test_one_shared_word_never_puts_a_command_on_a_ticked_step(self):
        steps = [{"content": "build: the settings screens", "status": "completed", "since": 1, "doneAt": 1000, "by": "wrap_up"}]
        other = match.derive(steps, [], tasks=[{"id": "b2", "title": "Upload build-5 to the test server", "state": "running", "start": 900000, "end": 0}])
        self.assertEqual((steps[0]["derived"]["status"], other["tasks"]), ("", ["b2"]))

    def test_a_done_summary_line_keeps_the_lanes_it_names(self):
        steps = [{"content": "Release 3: themes-poc, help-menu done", "status": "completed", "since": 5000, "doneAt": 5000},
                 {"content": "Publish release 3 (help-menu if it lands)", "status": "pending", "since": 5000}]
        run = self.run_of("help-menu", "done", start=1000, end=4000)
        self.assertEqual(self.lone(steps, run), [["wf_help-menu"], []])

    def test_a_live_run_tied_between_steps_goes_to_the_one_in_progress(self):
        steps = [{"content": "publish once alpha-beta lands", "status": "pending", "since": 1},
                 {"content": "finish alpha-beta and tidy it", "status": "in_progress", "since": 1}]
        self.assertEqual(self.lone(steps, self.run_of("alpha-beta")), [[], ["wf_alpha-beta"]])

    def test_a_done_line_rewritten_just_after_a_launch_is_not_that_runs_step(self):
        # the train number is the only evidence; the done summary was rewritten 3 s after the next pass launched
        steps = [{"content": "Release 7 live; train9 passes 1-2 done", "status": "completed", "since": 1, "doneAt": 100003},
                 {"content": "train9 pass 3: merge, gate, smoke", "status": "in_progress", "since": 1}]
        run = {"id": "wf_t9p3", "isTrain": True, "name": "train-run", "lane": "train-run", "branch": "", "title": "Merge ready branches",
               "train": {"k": "9"}, "state": "running", "start": 100000, "end": 0, "agents": []}
        self.assertEqual(self.lone(steps, run), [[], ["wf_t9p3"]])

    def test_a_lane_with_a_plain_name_matches_by_its_title(self):
        steps = [{"content": "fix round (menu flicker, cache warmup, sound balance)", "status": "in_progress", "since": 1}]
        run = dict(self.run_of("train"), title="fix round on train: menu flicker, cache warmup, sound balance")
        self.assertEqual(self.lone(steps, run), [["wf_train"]])

    def test_a_hash_in_a_done_line_does_not_name_a_suffix(self):
        steps = [{"content": "panel (55098b37), smoke a/c: done", "status": "completed", "since": 1, "doneAt": 50, "by": "wrap_up"}]
        other = match.derive(steps, [self.run_of("smoke-b", start=10)])
        self.assertEqual((steps[0]["derived"]["runs"], other["runs"]), ([], ["wf_smoke-b"]))

    def test_an_a_suffix_tells_lanes_apart_too(self):
        steps = [{"content": "smoke-b: the phone pass", "status": "in_progress", "since": 1}]
        other = match.derive(steps, [self.run_of("smoke-a")])
        self.assertEqual((steps[0]["derived"]["runs"], other["runs"]), ([], ["wf_smoke-a"]))

    def test_a_short_done_line_keeps_the_lane_it_lists(self):
        later = 1000 + 10 * match.GRACE_MS
        steps = [{"content": "smoke a, c, d: done", "status": "completed", "since": later, "doneAt": later},
                 {"content": "publish after build-9 (see run notes)", "status": "pending", "since": later}]
        run = dict(self.run_of("smoke-a", "done", start=1000, end=4000), title="smoke-a: phone pass for build-9")
        self.assertEqual(self.lone(steps, run), [["wf_smoke-a"], []])

    def test_after_the_end_of_turn_tick_a_live_lane_keeps_its_own_step_listed_second(self):
        steps = [{"content": "Release 4: merge sigma-fixes, checks, publish", "status": "in_progress", "since": 1},
                 {"content": "sigma-fixes: settings top line", "status": "completed", "since": 1, "doneAt": 50, "by": "wrap_up"}]
        self.assertEqual(self.lone(steps, self.run_of("sigma-fixes")), [[], ["wf_sigma-fixes"]])
        self.assertEqual(steps[1]["derived"]["status"], "running")

    def test_an_agent_label_alone_does_not_make_a_step_the_runs_own(self):
        steps = [{"content": "docs: rewrite the guide", "status": "in_progress", "since": 1}]
        run = dict(self.run_of("omega-panel"), agents=[{"label": "docs builder"}])
        other = match.derive(steps, [run])
        self.assertEqual((steps[0]["derived"]["runs"], other["runs"]), ([], ["wf_omega-panel"]))

    def test_a_lane_started_after_the_end_of_turn_tick_keeps_its_own_step(self):
        steps = [{"content": "theta-lane: second pass", "status": "completed", "since": 1, "doneAt": 1000, "by": "wrap_up"}]
        other = match.derive(steps, [self.run_of("theta-lane", start=1000 + match.GRACE_MS + 5000)])
        self.assertEqual((steps[0]["derived"]["runs"], steps[0]["derived"]["status"], other["runs"]), (["wf_theta-lane"], "running", []))

    def test_a_later_round_goes_to_its_new_step_not_the_ticked_one(self):
        steps = [{"content": "theta-lane: round 1", "status": "completed", "since": 1, "doneAt": 1000, "by": "wrap_up"},
                 {"content": "round 2 of theta-lane", "status": "in_progress", "since": 2000}]
        self.assertEqual(self.lone(steps, self.run_of("theta-lane", start=5000)), [[], ["wf_theta-lane"]])

    def test_a_summary_line_written_long_after_the_lane_ended_keeps_it(self):
        later = 1000 + 10 * match.GRACE_MS
        steps = [{"content": "Release 5: iota-poc, kappa-menu done", "status": "completed", "since": later, "doneAt": later},
                 {"content": "Publish release 5 (kappa-menu if it lands)", "status": "pending", "since": later}]
        run = self.run_of("kappa-menu", "done", start=1000, end=4000)
        self.assertEqual(self.lone(steps, run), [["wf_kappa-menu"], []])

    def test_a_command_sharing_one_distinctive_word_stays_unplaced(self):
        steps = [{"content": "docs: rewrite the guide", "status": "in_progress", "since": 1}]
        other = match.derive(steps, [], tasks=[{"id": "b3", "title": "Tail the build-docs log", "state": "running", "start": 10, "end": 0}])
        self.assertEqual((steps[0]["derived"]["tasks"], other["tasks"]), ([], ["b3"]))

    def test_a_run_launched_right_after_a_tick_is_not_the_ticked_steps_work(self):
        steps = [{"content": "alpha-lane: first pass", "status": "completed", "since": 1, "doneAt": 1000}]
        other = match.derive(steps, [self.run_of("alpha-lane", start=1000 + 20000)])
        self.assertEqual((steps[0]["derived"]["runs"], other["runs"]), ([], ["wf_alpha-lane"]))

    def test_a_run_launched_right_after_advance_belongs_to_the_next_step(self):
        steps = [{"content": "alpha-lane: first pass", "status": "completed", "since": 1, "doneAt": 1000},
                 {"content": "second pass of alpha-lane", "status": "in_progress", "since": 1000}]
        self.assertEqual(self.lone(steps, self.run_of("alpha-lane", start=1000 + 20000)), [[], ["wf_alpha-lane"]])

    def test_a_short_name_matches_its_own_step(self):
        steps = [{"content": "hud: fix the status bar", "status": "in_progress", "since": 1}]
        self.assertEqual(self.lone(steps, self.run_of("hud")), [["wf_hud"]])

    def test_lanes_named_train_something_are_lanes(self):
        self.assertFalse(status.is_train("train9-gate-fixes", {"br": "train9"}, {}))
        self.assertFalse(status.is_train("train8-prep", {}, None))
        self.assertTrue(status.is_train("train-run", {}, None))
        self.assertTrue(status.is_train("train-merge-only", {"branches": ["a", "b"]}, None))
        self.assertTrue(status.is_train("nightly-train-merge", {}, {"merge": {}, "gate": {}}))
        self.assertFalse(status.is_train("branch-run-x", {"branches": ["a"]}, None))

    def test_the_script_name_does_not_compete_with_a_real_name(self):
        steps = [{"content": "doors-breakable: event classes", "status": "in_progress", "since": 1},
                 {"content": "publish (doors fix if ready)", "status": "pending", "since": 1}]
        run = {"id": "wf_w", "isTrain": False, "name": "branch-run-doors", "lane": "doors-breakable", "branch": "doors-breakable",
               "title": "doors-breakable: every event opens a door", "state": "done", "start": 10, "end": 20, "agents": []}
        match.derive(steps, [run])
        self.assertEqual((steps[0]["derived"]["runs"], steps[1]["derived"]["runs"]), (["wf_w"], []))


class Sessions(Base):
    def test_two_active_leads_do_not_take_turns(self):
        a = self.session("s3aaaaaa-older", self.now - 120 * MIN)
        a.plan(self.now - 119 * MIN, [("older chat step", "in_progress")])
        a.launch(self.now - 100 * MIN, "wf_olda01", {"br": "old-lane"})
        a.live_agent("wf_olda01", "o1", "builder")
        b = self.session("s3bbbbbb-newer", self.now - 60 * MIN)
        b.plan(self.now - 59 * MIN, [("newer chat step", "in_progress")])
        b.launch(self.now - 50 * MIN, "wf_newb01", {"br": "new-lane"})
        b.live_agent("wf_newb01", "n1", "builder")
        mains = []
        for touch in (a, b, a, b):
            os.utime(touch.path, None)  # whichever chat wrote last
            snap = self.collect()
            mains.append(snap["plan"]["session"])
            self.assertEqual([i["content"] for i in snap["plan"]["items"]], ["newer chat step"])
            self.assertEqual([p["session"] for p in snap["otherPlans"]], ["s3aaaaaa-older"])
            self.assertEqual([i["content"] for i in snap["otherPlans"][0]["items"]], ["older chat step"])
            self.assertEqual({r["lane"] for r in snap["runs"]}, {"old-lane", "new-lane"})  # both chats' live work is shown
        self.assertEqual(set(mains), {"s3bbbbbb-newer"})

    def test_a_new_chat_without_runs_leads_when_the_old_one_is_quiet(self):
        a = self.session("s4aaaaaa-old", self.now - 300 * MIN)
        a.finished_run("wf_q00001", self.now - 290 * MIN, self.now - 280 * MIN, {"br": "q-lane"})
        a.plan(self.now - 299 * MIN, [("old step", "completed")])
        a.age(240)
        b = self.session("s4bbbbbb-new", self.now - 5 * MIN)
        b.plan(self.now - 4 * MIN, [("read the handoff", "in_progress")])
        snap = self.collect()
        self.assertEqual(snap["plan"]["session"], "s4bbbbbb-new")
        self.assertEqual([i["content"] for i in snap["plan"]["items"]], ["read the handoff"])

    def test_a_long_lane_keeps_its_session_active_after_six_later_runs(self):
        s = self.session("s3cccccc-1", self.now - 300 * MIN)
        t = s.live_agent("wf_long001", "l1", "builder")  # the first run of the chat, still writing
        runs = s.dir / "subagents" / "workflows"
        for k in range(6):  # six later runs, each with a folder of its own
            (runs / ("wf_later%02d" % k)).mkdir()
            (runs / ("wf_later%02d" % k) / "journal.jsonl").write_text("", encoding="utf8")
        s.age(240)  # the lead itself has written nothing for four hours
        for k in range(6):  # the later folders are newer than the first run's folder
            os.utime(runs / ("wf_later%02d" % k), ((self.now - (200 - k) * MIN) / 1000,) * 2)
        os.utime(t, None)  # the first run's agent has just written (appending does not touch its folder)
        self.assertGreaterEqual(status.session_activity(self.pdir, s.dir), self.now - MIN)

    def test_session_all_still_merges(self):
        a = self.session("s5aaaaaa-1", self.now - 30 * MIN)
        a.finished_run("wf_m00001", self.now - 25 * MIN, self.now - 20 * MIN, {"br": "m1"})
        b = self.session("s5bbbbbb-2", self.now - 20 * MIN)
        b.finished_run("wf_m00002", self.now - 15 * MIN, self.now - 10 * MIN, {"br": "m2"})
        snap = self.collect("--session", "all")
        self.assertEqual({r["lane"] for r in snap["runs"]}, {"m1", "m2"})


class SideWork(Base):
    def test_background_commands_from_start_to_end(self):
        s = self.session("s6aaaaaa-1", self.now - 30 * MIN)
        u1 = s.call(self.now - 20 * MIN, "Bash", {"command": "sleep 900", "description": "Wait before the next check", "run_in_background": True})
        s.user(self.now - 20 * MIN, result(u1, "Command running in background with ID: b1. Output is being written to: C:/tmp/b1.output"))
        u2 = s.call(self.now - 19 * MIN, "PowerShell", {"command": "npm run gate", "description": "Run the full gate", "run_in_background": True})
        s.user(self.now - 19 * MIN, result(u2, "Command running in background with ID: b2. Output is being written to: C:/tmp/b2.output"))
        u3 = s.call(self.now - 18 * MIN, "Bash", {"command": "npm run serve", "run_in_background": True})
        s.user(self.now - 18 * MIN, result(u3, "Command running in background with ID: b3. Output is being written to: C:/tmp/b3.output"))
        u4 = s.call(self.now - 17 * MIN, "Bash", {"command": "make docs", "description": "Build the docs", "run_in_background": True})
        s.user(self.now - 17 * MIN, result(u4, "Command running in background with ID: b4. Output is being written to: C:/tmp/b4.output"))
        # endings arrive in the three shapes a transcript uses: queued, attached to the next request, a user message
        s.write({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(self.now - 10 * MIN),
                 "content": notification("b2", u2, "completed", 'Background command "Run the full gate" completed (exit code 0)')})
        s.write({"type": "attachment", "timestamp": iso(self.now - 9 * MIN), "attachment": {"type": "queued_command",
                 "prompt": notification("b4", u4, "failed", 'Background command "Build the docs" failed (exit code 2)')}})
        s.call(self.now - 8 * MIN, "TaskStop", {"task_id": "b3"})
        u5 = s.call(self.now - 7 * MIN, "Bash", {"command": "npm run watch", "description": "Watch the files", "run_in_background": True})
        s.user(self.now - 7 * MIN, result(u5, "Command running in background with ID: b5. Output is being written to: C:/tmp/b5.output"))
        u6 = s.call(self.now - 7 * MIN, "Bash", {"command": "npm run serve2", "description": "Serve the page", "run_in_background": True})
        s.user(self.now - 7 * MIN, result(u6, "Command running in background with ID: b6. Output is being written to: C:/tmp/b6.output"))
        s.write({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(self.now - 6 * MIN),  # one notice, several jobs
                 "content": "<task-notification>\n<task-id>__orphan_summary_1</task-id>\n<task-id>b5</task-id>\n<task-id>b6</task-id>\n<status>stopped</status>\n<summary>2 jobs stopped</summary>\n</task-notification>"})
        snap = self.collect()
        self.assertEqual({t["id"]: t["state"] for t in snap["tasks"] if t["id"] in ("b5", "b6")}, {"b5": "stopped", "b6": "stopped"})
        got = {t["id"]: (t["state"], t["title"]) for t in snap["tasks"]}
        self.assertEqual(got["b1"], ("running", "Wait before the next check"))
        self.assertEqual(got["b2"], ("done", "Run the full gate"))
        self.assertEqual(got["b4"][0], "failed")
        self.assertEqual(got["b3"], ("stopped", "command: npm run serve"))
        self.assertIn("b1", snap["plan"]["other"]["tasks"])  # live, and no step names it

    def test_a_side_agent_has_its_brief_doing_and_result(self):
        s = self.session("s7aaaaaa-1", self.now - 30 * MIN)
        u = s.call(self.now - 20 * MIN, "Agent", {"description": "Check the icons", "prompt": "Check the icons on the settings screens. Report any missing one.",
                                                  "run_in_background": True})
        s.user(self.now - 20 * MIN, result(u, "Async agent launched successfully. agentId: s1"))
        s.side_agent("s1", u, "Check the icons")
        live_ = self.collect()["side"][0]
        self.assertEqual((live_["state"], live_["brief"], live_["nowText"]), ("running", "Check the icons on the settings screens.", "reading app.py"))
        s.user(self.now - 2 * MIN, {"type": "text", "text": notification("a9", u, "completed", "Agent finished", "Two icons are missing: save and undo.")})
        s.side_agent("s1", u, "Check the icons", finished=True)
        done = self.collect()["side"][0]
        self.assertEqual((done["state"], done["result"]), ("done", "Two icons are missing: save and undo."))


    def test_a_workflow_agent_continued_later_is_named(self):
        s = self.session("s7aaaaaa-2", self.now - 30 * MIN)
        s.live_agent("wf_cont001", "c7", "tidy-build")
        (s.dir / "subagents" / "agent-c7.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": iso(self.now), "message": {
            "model": "claude-sonnet-5-5", "content": [use("toolu_z", "Edit", {"file_path": "C:/work/repo/src/panel.py"})]}}) + "\n", encoding="utf8")
        row = next(x for x in self.collect()["side"] if x["id"] == "c7")
        self.assertEqual((row["description"], row["nowText"]), ("continued: tidy-build", "editing panel.py"))


class PlainWords(unittest.TestCase):
    def test_tool_calls_in_plain_words(self):
        self.assertEqual(live.describe("Bash", {"command": "x", "description": "Publish the build"}), "Publish the build")
        self.assertEqual(live.describe("Bash", {"command": 'cd "C:/work/my repo" && git status --short'}), "running a command: git status --short")
        self.assertEqual(live.describe("Bash", {"command": "python C:/work/repo/tools/build.py --check"}), "running a command: python build.py --check")
        self.assertEqual(live.describe("Read", {"file_path": "C:/work/repo/src/config.py"}), "reading config.py")
        self.assertEqual(live.describe("mcp__ccd_session_mgmt__get_usage", {}), "checking usage")
        self.assertEqual(live.describe("mcp__Some_Server__do_thing", {}), "using Some Server (do thing)")
        self.assertEqual(live.describe("Agent", {"description": "Look over the art"}), "asking a helper: Look over the art")


class Scrubbing(unittest.TestCase):
    """What the phone export removes from every string: paths, keys and ids, but not names or links."""

    def test_paths_keys_and_ids_go(self):
        from snapshot import clean
        for text in ("N=/c/Users/someone/AppData/Local/Temp/x", "cd /mnt/c/Users/someone/repo", "C:/Users/someone/work/a.py",
                     "C--Users-someone-Documents-GitHub-Some-Repo", "c--users-someone-documents-github-some-repo-here",
                     "projects/-home-someone-repo", "sk-abcdefghijklmnopqrstu", "xoxb-1234567890-abcdefghijklmnopqrstuvwx",
                     "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "3f1c2b0e9d8a7c6b5a4f3e2d1c0b9a8f7e6d5c4b", "0f8fad5b-d9cb-469f-a165-70867728950e"):
            self.assertNotIn("someone", clean(text, 300))
            self.assertIn(re.split(r"[= /]", clean(text, 300))[-1], ("(path)", "(removed)"))

    def test_names_and_links_stay(self):
        from snapshot import clean
        for text in ("https://example.com/home/getting-started", "branch-run-something-long-and-plain-enough",
                     "PROJ-1234-improve-the-login-error-page-v2", "see https://github.com/owner/repo/pull/12"):
            self.assertEqual(clean(text, 300), text)


class PhoneExport(Base):
    def test_new_fields_survive_and_the_page_keeps_its_flag(self):
        s = self.session("s8aaaaaa-1", self.now - 30 * MIN)
        s.plan(self.now - 29 * MIN, [("alpha-beta: build", "in_progress")])
        s.launch(self.now - 28 * MIN, "wf_ph0001", {"br": "alpha-beta"})
        s.live_agent("wf_ph0001", "a1", "builder")
        u = s.call(self.now - 20 * MIN, "Bash", {"command": "sleep 60", "description": "Wait a minute", "run_in_background": True})
        s.user(self.now - 20 * MIN, result(u, "Command running in background with ID: b9. Output is being written to: C:/tmp/b9.output"))
        data, size, _ = snapshot_tree.make_data(self.collect(), "Test status", 200)
        self.assertLess(size, 200 * 1024)
        item = data["plan"]["items"][0]
        self.assertEqual(item["derived"]["runs"], ["wf_ph0001"])
        self.assertEqual(data["plan"]["session"], data["runs"][0]["session"])  # shortened the same way, so the page can pair them
        self.assertEqual(data["tasks"][0]["title"], "Wait a minute")
        page = snapshot_tree.render_page(data)
        self.assertIn("PHONE = true", page)

    def seed_of(self, page):
        tag = '<script id="seed" type="application/json">'
        start = page.index(tag) + len(tag)
        return page[start:page.index("</script>", start)]

    def test_markup_in_the_data_cannot_end_or_hijack_the_seed_block(self):
        data = {"title": "T", "generated": 1, "plan": {"items": [{"content": "a <!--<script> b </script> c", "status": "pending"}]}}
        seed = self.seed_of(snapshot_tree.render_page(data))
        self.assertNotIn("<", seed)
        self.assertEqual(json.loads(seed)["plan"]["items"][0]["content"], "a <!--<script> b </script> c")

    def test_the_card_page_seed_cannot_be_ended_either(self):
        import snapshot
        page = snapshot.render_page(snapshot.build({"plan": {"items": [{"content": "a <!--<script> b", "status": "pending"}]}}, "T"))
        self.assertNotIn("<", self.seed_of(page))

    def test_a_long_list_keeps_every_open_step_and_the_newest_done_ones(self):
        items = [{"content": "done step %d" % k, "status": "completed", "since": 1, "doneAt": 100 + k} for k in range(16)]
        items += [{"content": "open step 0", "status": "in_progress", "since": 1}, {"content": "open step 1", "status": "pending", "since": 1}]
        out = snapshot_tree.trim_plan({"items": items, "session": "s"}, {})
        self.assertEqual([i["content"] for i in out["items"]], ["done step %d" % k for k in range(4, 16)] + ["open step 0", "open step 1"])


if __name__ == "__main__":
    unittest.main()

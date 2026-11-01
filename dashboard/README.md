# Multi-agent dashboard

A live, local control-room page for long multi-agent Claude Code runs: a lead on top, one card per lane, a merge-train panel, worktrees, limits and a recent-events log. It reads files already on disk, so keeping it open costs **no model tokens**. Works for any repo that uses the Claude Code Workflow tool; nothing in it is project-specific.

## Start

```bash
python ~/.claude/skills/multi-agent-lead/dashboard/run.py --project <repo path>
```

It prints a URL (default `http://127.0.0.1:8765/`, next free port if busy). Open it in any browser. `--project` defaults to the current directory. Python 3 standard library only.

| Flag | Meaning |
|---|---|
| `--project` | The repo the Claude Code session ran in. Run records are found under `~/.claude/projects/<slug of that path>/`. |
| `--session` | A session id, or `all`. Default: the newest session that has workflow runs. |
| `--integration-branch` | Branch lanes are compared with (ahead/behind). Default `main`, else `master`, else the main worktree's branch. |
| `--lane-field` | Workflow arg that names a lane. Default tries `br`, `branch`, `lane`, `title`, then the workflow name. |
| `--usage-file` | Optional JSON for the usage bars (below). |
| `--studio-limit N` | Show the count of running processes named by `--studio-process` (no default: name the process to count; without it the count shows as unknown) against N. |
| `--handoff-file` | Notes file whose age is shown. |
| `--idle-minutes` | No transcript activity for this long turns a running agent amber "idle" (default 10). |
| `--port` (0 = stable per project), `--print-port`, `--interval`, `--out-dir`, `--open` | Port, seconds between refreshes (30), where `status.json` goes (a per-project temp folder), open the browser. |

`python status.py --project <repo> --out x.json` writes one snapshot and exits.

## What it reads (all read-only)

- `~/.claude/projects/<slug>/<session>/workflows/<runId>.json`: run summaries (status, args, result, phases, per-agent progress). Written when a run ends.
- `.../subagents/workflows/<runId>/journal.jsonl`, `agent-<id>.meta.json`, `agent-<id>.jsonl`: used for runs still in progress. "Last activity" is the transcript's modified time.
- The session transcript `<session>.jsonl`, read incrementally, only to find the title and args of runs that have no summary yet.
- `git worktree list`, `for-each-ref`, `rev-list --count`, `status --porcelain` (with `--no-optional-locks`; it never fetches or writes).
- The lead's task list (the app's `update_plan` or `TodoWrite` calls), replayed from the same transcript. The panel is hidden when there is none.
- Optional usage file, optional process count.

Everything read from the records is treated as untrusted text: clipped, stripped of control characters, put on the page with `textContent` only, never executed. The server binds to 127.0.0.1, serves only `/` and `/status.json`, rejects other Host headers and sends a strict Content-Security-Policy.

## Panels

- **Needs you**: what a person has to act on, most urgent first: failed agents (a usage limit shows its reset text), agents repeating the same tool call, runs with no activity or past their usual time, a final check that says not ready, uncommitted or unpushed work in a finished lane, a 5-hour forecast near the limit. Lanes already merged (no commits ahead of the integration branch, or merged by a train) are skipped. Minor items (old stopped runs, open issues, stale notes) are behind a button. Click an item to open its lane.
- **Task list**, **Burn and limits**, **Ready to merge**, **Merge train**, **Side agents**, **Model mix**, **Worktrees**, **Recent events**.
- **Burn and limits**: tokens of agents that finished in the last hour and 5 hours (Claude Code's own run totals), what live agents have written so far (output tokens, read from their transcripts), tokens by model and the most expensive lanes. With `--usage-file` it also fits a rate to the percentages the running collector has seen: it needs about 10 minutes of samples, and the history is in memory only, so restarting `run.py` resets it.
- **Ready to merge**: finished lanes whose branch has commits not in the integration branch, with whether a train already holds them, whether they are pushed, and the final check result.
- **Side agents**: subagents the lead started directly with the Agent tool, outside a workflow (`<session>/subagents/agent-*.meta.json`).
- **Model mix**: opus, sonnet, haiku and other agents working now and in the last 24 hours. Each agent's model is a small neutral badge; the bars use one shade per family, Opus in the accent colour, and hovering a segment gives its number. "default" means the record names no model.
- Each running agent shows its latest tool call ("now") and a red "repeating" badge when 4 or more of its last 10 calls were identical.
- The top bar shows how old the notes file is (`--handoff-file`, default `HANDOFF.md`, `NOTES.md` or `docs/HANDOFF.md`).

## Sessions

By default the newest session that has workflow runs; if none has, the newest session with a transcript, so a project that only uses plain subagents and a task list still shows those panels. `--session <id>` picks one, `--session all` merges them.

## Agent usage

Click an agent (a box in the tree, or a row in a lane's detail) for its own view. Its usage is a small table with two columns, **Now** and **Typical** (for this kind of agent), and four rows: output tokens, tool calls, running time and context size. Numbers are right-aligned. Under each "Now" number that has a typical value there is a thin bar and "N% of typical", amber past 100%.
- **Output tokens** is what the agent has written so far (live agents only, read from its transcript), so it has no typical value: finished agents do not record it. **Tool calls** counts its tool uses.
- **Context size** is how big the agent's conversation has grown. The run summary's own "tokens" figure is exactly this (the context size at the end of the run, not tokens spent), so a live agent's context compares fairly with the typical figure. The Burn panel's "context tokens" are sums of these, a rough proxy for work, not a bill.
- **Typical** is the median of finished agents in the same workflow and stage (shown once there are 3 or more).
- In a lane's detail each agent is one compact row with the same four numbers as small columns (stacked label and value pairs on phones), plus its typical value and percentage. A total over the lane's agents is in the summary.
- The phone export uses the same layout, tested at 375 and 430 px with tap targets of 44 px.

## Pushed and ahead

"Not pushed" is counted against the branch's own copy on the remote (`origin/<branch>`, else another remote's copy), not against whatever upstream the branch was set to track. A branch with no copy on any remote shows "never pushed". "N ahead of <integration branch>" is shown separately in the lane detail and is not a warning: it is expected until a release.

## Phone export

`snapshot_tree.py` turns the tree dashboard into one self-contained HTML file for a phone, plus the data that keeps it live:

```bash
python snapshot_tree.py --project <repo> --title "Project status" --out tree.html --data-out tree-data.json [--max-kb 200]
```

- `--out` is `index.html`'s own drawing code (so it never drifts from the desktop page) with the local fetch removed. The page reads, in order, the Artifact db doc `status/current` (when `claude.use('db')` resolves, redrawing on every change), else the snapshot embedded in it.
- `--data-out` is the body of that doc: a copy of the latest `status*.json` (from the running `run.py`) with local paths, URL credentials and key-like strings removed from every string, session ids shortened, and older runs, finished runs' agents and results, worktrees and long text cut down in steps (five levels) until it is under `--max-kb` (a db doc is capped at 256 KiB). Pass `--status` to name a status file instead of using the newest one for the project.
- The page shows "updated N min ago", amber after 15 minutes, and has 44 px tap targets at phone width. It does not publish anything; refresh the doc by running the command again and writing `--data-out` to `status/current`.
- It needs a recent `status*.json`, so keep `run.py` running (or point `--status` at a current file).

## States

State marks: running (pulsing accent dot), done (green tick), failed (red dot and outline, error text shown, e.g. a usage limit), idle (amber dot and outline: running but quiet for `--idle-minutes`), stopped (grey square: the run was killed), pending or skipped (hollow ring, dashed box). Colour is kept to these small marks; box outlines are coloured only for failed, idle and repeating agents. A lane takes the state of its newest run; older runs of the same lane are summarised on the card and listed in the drawer. A run counts as failed when any agent errored, even if the run itself finished. Runs with no summary file and no live agent activity show as idle (for example when the session that launched them died).

A run whose workflow name contains `train` goes to the **Merge train** panel instead of a lane: its branches (queued, then merged), gate and smoke state. Gate and smoke are read from `result.gate` / `result.smoke` (`ok`, `allFail` or `failed` fields); the train's `args.branches` and `args.k` supply the list and number. Click a lane or train box for the drawer: latest result text (`plainWords` or `summary`), tip, commits, open issues, agents, and the resume command shape `Workflow({scriptPath: "...", resumeFromRunId: "wf_..."})`.

## Usage numbers

Two sources are merged, and every bar says which one it came from and how old it is ("from the app, 4 min ago" or "written by the lead 1h ago").

1. **The Claude desktop app's own usage log** (automatic): `plan-usage-history.json`, found by searching the app's data folders (Windows `%LOCALAPPDATA%/Claude-Data/*/` and `%APPDATA%/Claude*/`; macOS `~/Library/Application Support/Claude*/`; Linux `~/.config/Claude*/`), or given with `--plan-usage-history <path>`. It holds `{"version": 2, "samples": [{"t": <ms epoch>, "org": "<account id>", "u": {"fh": <5-hour %>, "sd": <7-day %>}}, ...]}`, a sample about every 15 minutes, possibly from several accounts. The dashboard uses the account of the newest sample. It has no reset times.
2. **A file the lead writes**: `~/.claude/dashboard-usage.json` (or `--usage-file`), the shared file every project reads. Paste the plan windows from the usage tool as they are:

```json
{"windows": [{"label": "5-hour limit", "percentUsed": 12, "resetsAt": "2026-10-31T22:30:00Z"},
             {"label": "Weekly · all models", "percentUsed": 81, "resetsAt": "2026-11-06T22:00:00Z"}]}
```

**Resets and old readings.** The app logs only about every 15 minutes, so the newest sample can be older than the last reset of a window. A reading taken before a window's last reset belongs to the previous window, so it shows as 0% with "reset since the last reading (N min ago)" until a newer reading arrives. The same rule applies to the lead's file when it was written before the reset, and to the weekly window at its own reset. A window that has just reset is left out of the limit forecast. The page applies the rule again every 30 seconds, so a phone page that is hours behind still shows a fresh window once a reset time has passed. If the newest app sample is more than 20 minutes old while the lead's session is clearly active (its transcript is being written), the page adds "the app has not logged usage for N min".

**Which number wins:** the app's percentages when its newest sample is newer than the lead's file, otherwise the file's. Reset times always come from the file; a reset that has already passed is rolled forward by 5 hours (5-hour window) or 7 days (weekly). The note about stale numbers appears only when both sources are over an hour old. On another platform, or with an app version that has no such log, nothing changes: the lead-written file is used alone.

Each window shows used and left, when it resets and how long that is. Only percentages exist, not token counts. Bars turn amber at 75% and red at 85%. The limit forecast uses the app's sample history when there is one (a rising stretch only, so a reset is not mistaken for a rate), else the samples this collector has seen. The older `{"five_hour": {...}, "weekly": {...}}` file form still works. The phone export carries the same windows with their source and age.

## Hidden tabs

A browser throttles the timers of a tab that is hidden (behind another panel, or the window in the background), so it can come back showing old numbers. The page therefore refreshes the moment it can be seen again (on `visibilitychange`, window focus and a restore from the back/forward cache) and recomputes "updated N ago" from the data's own generated time every 5 seconds and on each of those events, so it never shows an old value as current. After a tab has been hidden for over a minute the header adds an amber "paused while hidden" until fresh data arrives. The phone page does the same by re-subscribing to the db doc, which delivers the current doc at once.

## Several projects at once

Each `run.py` writes one small file, `<claude dir>/dashboard-projects/<port>.json` (display name, parent folder name, port, and a few counts), and rewrites it on every refresh: that is its heartbeat. It deletes the file on exit. A server that is killed simply stops beating, and its entry disappears from the list after 2 minutes (and the file is deleted after 10). Each server writes only its own file, so there is nothing to lock, and no full paths are stored.

- **Dropdown:** the header shows a project dropdown when two or more dashboards have a live heartbeat, with each project's running lane count and how many items need you. Choosing one opens that project's own dashboard (a normal link to its port; each server still binds only to 127.0.0.1, still rejects other Host headers and does not read across origins). One project alone shows no dropdown. Two folders with the same name are shown as `parent/name`.
- **Ports:** the default `--port 0` gives each project a stable port from its folder path (8700 to 8999). `python run.py --project <repo> --print-port` prints it, so a `launch.json` entry can use the same number for `--port`, `--strict-port` and `"port"`. If the port is taken, a normal run moves up to the next free one and registers the real port, so the dropdown always has the right number; with `--strict-port` it exits with an error instead, which is what `preview_start` needs (it opens the port in `launch.json`, so it must never silently land on another one).
- **Windows:** servers now bind exclusively. Before this, Windows let a second server bind a port that was already in use, so `--strict-port` was not strict and two dashboards could share one port.
- A dashboard started by `preview_start` finds its real port the same way as any other: `run.py` binds, reads the port it actually got, writes it into its registry file and prints it. With `--strict-port` the real port is always the one in `launch.json`.
- Dashboards started before this change (old code) have no `/projects.json` and are not listed; restart them to join.

## The advisor box

The advisor's replies are encrypted in the session file, so the lead logs each consult itself with one call:

```bash
python advisor_log.py --project <repo> --asked "what it consulted about" --advice "what the advisor said, 1-3 sentences" --action "what it did about it" --model opus
```

That appends one line to `<claude dir>/dashboard-advisor/<project slug>.jsonl` (same slug as the status files): `{"at", "asked", "advice", "action", "model"}`, plain text, control characters removed, each field clipped to 400 characters. The dashboard reads the newest 20 lines (a missing file or a half-written last line is fine).

- **The box** shows the model chip of the latest note, "N logged, last X ago" and the latest advice, or "Thinking, waiting 40s / about: <current task>" while a consult is in flight. In flight means the lead's transcript has an advisor call with no result yet (only counted for 30 minutes).
- **The drawer** shows that live state first, then the count and last use, then every logged consult newest first: when (with the model chip), Asked, Advice, Then (what the lead did). With no notes it says "No consults logged yet. The lead writes a note after each one."
- The phone export carries the newest 10 notes (text clipped shorter) and the live state.

## The tree

One view: the lead on the left (its model, working or waiting, its current task and last action), the advisor beside it (how many times it was consulted), then the task list steps (in the task list's order, with lanes the steps do not mention under "Other work"), then lanes and trains, their stages, their agents, and any helper subagents an agent started. Side agents the lead started directly hang off the lead. Each lane box shows its title, state, ETA, last activity, git state (tip, ahead/behind, uncommitted, not pushed), run history and a progress bar. Agent boxes show what they are doing in plain words, a red outline when one repeats the same call, and the error text when one failed. Links to running agents flow.

Drag to move, scroll or pinch to zoom, "Fit" to fit as much as possible (down to 40%; below 50% the small text hides and only names and states stay). Click a box for the plain-words summary and details, click the +/- on a box to fold or unfold it (finished lanes start folded). Click a task in the Task list: the tree unfolds the matching lanes, glides to them and dims the rest; click the background or "Clear highlight" to go back. A lane's box flashes once when its state changes.

## Task list steps

A step shows an ETA and its live agents when it names a running lane or train. The match is by words: every part of a lane's name (`weapons-3a` needs "weapons" and "3a"), the train number next to the word "train", or the agents' own labels. It is a guess from free text, so the badge also shows how many lanes matched. Click an agent to open its lane. The detail view of a lane says "Part of task" with the step it matched.

## Detail view

Clicking a lane, train or event opens a summary in plain words first: the job, the status (stage x of y, how long, ETA), what each live agent is saying or doing right now (its last message, or its latest tool call put in plain English), the branch progress (commits ahead, uncommitted files, not pushed), the result for finished runs, and anything that needs you. The full result, agents and resume command follow. It is built from data already read, so it costs no tokens.

## ETA

For each live run the page shows `ETA ~Nm`, and the top bar shows the longest one. It is the median time that finished agents of the same workflow and phase took (from the run summaries), minus how long the current agent has been going, plus the median of phases not started yet. It appears only with at least 3 finished agents behind it. Past its typical time a run says "past its usual time" instead of guessing. Treat it as a rough guide: it knows nothing about how big the current job is, and a phase the run will skip is still counted.

## Limits

- Journals carry no timestamps, so an agent's start is its meta file's time and its finish is its transcript's last write.
- Model badges show only when the run carries one (summary progress, agent meta, or `args.builders[].model`).
- Token counts exist only for finished runs.
- Worktree and branch data comes from local refs; run `git fetch` yourself if you want remote state.

Hovering a task in the Task list highlights its boxes in the tree without moving the view; clicking pins it and moves there. The "past its usual time" flag only appears at 1.5 times the typical time.


Burn and limits (rewritten): one bar each for the 5-hour window and the weekly limit with the reset time, then the running agents and the most expensive lanes. The forecast line appears only when the app's usage log belongs to the same account as the merged percentages (within 3 points) and only says 'the limit is reached before the reset' when that is true; Bash background jobs are not agents and do not show as running.

The Burn and limits panel no longer repeats the usage bars (they are in the header): it shows only a burn forecast line when one is reliable, what is running now, finished tokens, the model mix and the most expensive lanes.

The filter tabs also filter the lead's task-list steps in the tree: Live keeps the in-progress steps and steps with a live lane, Attention keeps open Decision / Needs a person steps and steps with a failed, stopped or idle lane, and the other tabs keep every step.

Layout and density: the side column's panels can be reordered (drag grip, up and down buttons, Alt+Arrow), resized by their bottom edge (Shift+Arrow), folded, and the column's width set by a divider; "Reset layout" restores the default; the layout is saved in localStorage and survives live redraws. Finished task steps draw as one-line boxes (consecutive ones merge into a "N done" chip), the tree fits to its content, and on desktop widths the page fills the window height and only the tree box and the side column scroll. A live run with no ETA says "no ETA yet"; an ETA needs 2 finished agents of the same phase.

## Look

The page follows the Claude app's look: warm neutrals with one terracotta accent (dark and light themes), a 4/8/12/16/24 px spacing scale, flat side sections with a count beside each name, and line icons. Detail that used to be spelled out on the page (sources, full reset times, exact per-model numbers, what a filter keeps) is in tooltips. The header holds the lane counts and page tools; the row above the tree holds the filters, the lane and step counts and the usage meters.

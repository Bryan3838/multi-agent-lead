# multi-agent-lead

A Claude Code skill for leading a long, multi-agent coding project in any repo: it checks your model setup, plans with Opus, builds with cheaper models, reviews with independent critics and a skeptic, merges in trains, and keeps a live dashboard, a phone page and a handoff note.

## Install

1. Copy this folder to `~/.claude/skills/multi-agent-lead` (on Windows: `C:\Users\<you>\.claude\skills\multi-agent-lead`).
2. Make sure Python 3 is on your PATH (the dashboard uses only the standard library) and Node.js is installed (only to syntax-check the scripts if you edit them).
3. In a project, start Claude Code and run `/multi-agent-lead`.

## What you need

- Claude Code with the Workflow tool, and a plan with room for several agents running at once.
- For the best setup: main model Sonnet (`/model sonnet`) and advisor Opus (`/advisor opus`). The skill checks this first and tells you what to change.
- The phone page needs the Claude desktop app's Artifact and database features. Everything else works without them.

## What it does

- **Step 0:** checks the main model, the advisor and the sub-agent models, and asks you to confirm.
- **Existing project:** reads the handoff or notes file, starts the dashboard, resumes unfinished work.
- **New idea:** an Opus agent writes `docs/DESIGN.md`, you approve it once, then lanes build it.
- **Lanes:** `scripts/branch-run.js` (plan, build, critics, fix, skeptic) and `scripts/train-run.js` (merge, full test gate, smoke test), with the model chosen per role.
- **Dashboard:** `dashboard/run.py`, a local page at 127.0.0.1 showing the lead, advisor, lanes, agents, usage, the merge queue, and the task list as a queue (what runs now and what each agent is doing, what waits on you, what is next, what is done). It costs no tokens. Its tests: `python -m unittest discover -s dashboard/tests`.
- **Phone page:** `dashboard/snapshot_tree.py` makes a private page you can open on your phone.
- **Hook:** `hooks/task-list-reminder.py` reminds the lead to update its task list after each launch.

## Notes

- Nothing is sent anywhere by the dashboard; it reads local files and git. The phone page is a private page on your own claude.ai account.
- Paths and restart commands in the skill are written for Windows; adapt them on macOS or Linux.
- First version: it has been used in anger on one large project and has not been run on a brand-new repo yet.

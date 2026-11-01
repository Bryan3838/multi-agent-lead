#!/usr/bin/env python3
"""PostToolUse hook for Workflow and Agent: reminds the lead to keep its task list current.

Prints the hook JSON that injects one line of context after the tool ran. It reads nothing from the tool call,
writes nothing to disk and never blocks.
"""
import json
import sys

try:
    sys.stdin.read()
except Exception:
    pass

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": ("You just started new work: update the task list now (12 steps or fewer, lanes named exactly as in the "
                          "workflow args, running work marked in progress) so the dashboard and the phone page stay current."),
}}))

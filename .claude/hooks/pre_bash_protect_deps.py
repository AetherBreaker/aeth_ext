# Standard library imports
import json
import re
import sys

BANNED = re.compile(r"(?:^|[;&|\n]|\bthen\b)\s*uv\s+(?:add|remove|lock)\b")


def main():
    data = json.load(sys.stdin)
    command = data.get("tool_input", {}).get("command", "")
    if not command or not BANNED.search(command):
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Dependency changes (uv add/remove/lock) must be run by the user, not Claude. "
                f"Ask them to run: {command.strip()}"
            ),
        }
    }))


if __name__ == "__main__":
    main()

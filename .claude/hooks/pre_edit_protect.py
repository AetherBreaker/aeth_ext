# Standard library imports
import json
import sys
from pathlib import Path

PROTECTED_NAMES = {".env", "uv.lock"}


def main():
    data = json.load(sys.stdin)
    file_path = data.get("tool_input", {}).get("file_path", "")
    if not file_path:
        return

    name = Path(file_path).name
    if name not in PROTECTED_NAMES:
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"{name} is protected. .env holds live credentials and uv.lock is "
                "managed by uv — ask the user to make this change directly."
            ),
        }
    }))


if __name__ == "__main__":
    main()

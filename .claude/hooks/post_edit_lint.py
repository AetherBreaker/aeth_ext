import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    data = json.load(sys.stdin)
    file_path = data.get("tool_input", {}).get("file_path", "")
    if not file_path or not file_path.endswith(".py") or not Path(file_path).exists():
        return

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    result = subprocess.run(
        ["uv", "run", "ruff", "check", "--fix", file_path],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()
    if not output or output == "All checks passed!":
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"ruff check --fix {Path(file_path).name}:\n{output}",
        }
    }))


if __name__ == "__main__":
    main()

# Standard library imports
import json
import os
import subprocess


def main() -> None:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    result = subprocess.run(
        ["uv", "run", "pyright"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return

    output = (result.stdout + result.stderr).strip()
    if not output:
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": f"pyright (project-wide) reported issues:\n{output[:4000]}",
        }
    }))


if __name__ == "__main__":
    main()

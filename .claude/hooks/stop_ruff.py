# Standard library imports
import json
import os
import subprocess


def main():
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    result = subprocess.run(
        # --unfixable F401 keeps ruff sorting/formatting imports (and applying
        # other safe autofixes) but leaves unused-import removal as a reported
        # violation instead of silently deleting the import -- an import added
        # in one edit and used in a later one shouldn't get stripped in between.
        ["uv", "run", "ruff", "check", "--fix", "--unfixable", "F401", "."],
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
            "additionalContext": f"ruff check (project-wide) reported issues:\n{output[:4000]}",
        }
    }))


if __name__ == "__main__":
    main()

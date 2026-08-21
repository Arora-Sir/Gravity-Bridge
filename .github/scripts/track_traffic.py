"""Merge GitHub's rolling 14-day traffic stats into a permanent history file.

GitHub only exposes /traffic/clones and /traffic/views for the last 14 days,
so this runs daily (see .github/workflows/traffic.yml), adds any day not
already recorded, and regenerates shields.io endpoint badges from the total.
"""
import json
import os
import subprocess
import sys

REPO = os.environ["GITHUB_REPOSITORY"]
HISTORY_PATH = os.path.join(".github", "traffic-history.json")
BADGES_DIR = os.path.join(".github", "badges")


def gh_api(path):
    result = subprocess.run(
        ["gh", "api", f"repos/{REPO}/traffic/{path}"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"clones": {}, "views": {}}


def merge(history_section, entries, key):
    for entry in entries:
        date = entry["timestamp"][:10]
        if date not in history_section:
            history_section[date] = entry[key]


def write_badge(name, label, total):
    os.makedirs(BADGES_DIR, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "label": label,
        "message": f"{total:,}",
        "color": "blue",
    }
    with open(os.path.join(BADGES_DIR, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def main():
    history = load_history()

    clones = gh_api("clones")
    views = gh_api("views")
    merge(history["clones"], clones["clones"], "count")
    merge(history["views"], views["views"], "count")

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, sort_keys=True)
        f.write("\n")

    total_clones = sum(history["clones"].values())
    total_views = sum(history["views"].values())
    write_badge("clones", "clones", total_clones)
    write_badge("views", "views", total_views)

    print(f"total clones: {total_clones}, total views: {total_views}")


if __name__ == "__main__":
    sys.exit(main())

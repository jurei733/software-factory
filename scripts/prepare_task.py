"""Create a bounded handoff artifact from a committed task; does not invoke an AI API."""
import os
from pathlib import Path
import sys

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here if (here / "factory.py").exists() else here.parent))
import factory


def main():
    root = Path.cwd().resolve()
    assistant = os.environ.get("ASSISTANT", "codex")
    factory.require(assistant in ("claude", "codex"), "Unbekannter Assistent.")
    integrated = (root/".factory/config.json").is_file()
    config_file = ".factory/config.json" if integrated else "profiles/factory.json"
    task_root = ".factory/tasks" if integrated else "tasks"
    tasks = factory.inside(root, task_root)
    task_path = factory.inside(root, os.environ.get("TASK_FILE", task_root+"/pilot.json"))
    factory.require(tasks in task_path.parents and task_path.suffix == ".json",
                    "Aufgabe muss eine JSON-Datei innerhalb .factory/tasks sein.")
    config = factory.read_json(root / config_file)
    if config["project"] == "$GITHUB_REPOSITORY":
        from repository_factory import bind_repository
        config = bind_repository(config, os.environ)
    task = factory.read_json(task_path)
    factory.prepare(config, task, root / "work-packet", assistant=assistant)
    factory.write_new_json(root / "work-packet/source.json", {
        "repository": config["project"], "commit": factory.git_commit(root),
        "config_sha256": factory.digest(config), "upstream_write_access": False,
        "scope": "Task metadata only; no source checkout or login is included."})
    print("Briefing für " + assistant + " erzeugt; kein KI-Aufruf ausgeführt.")


if __name__ == "__main__":
    try:
        main()
    except (factory.FactoryError, OSError, ValueError) as error:
        print("FEHLER: " + str(error), file=sys.stderr)
        sys.exit(2)

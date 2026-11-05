"""Preflight/start the optional native Tares crew with the colleague's own Claude subscription."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tares_adapter import Tares, environment
from runtime_safety import runtime_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--tares", default="tares")
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--doctor", action="store_true")
    args = parser.parse_args()
    root = runtime_path(args.runtime)
    state_path, marker_path = root/"pilot.json", root/"claude-crew/initialized.json"
    if not state_path.is_file() or not marker_path.is_file():
        raise ValueError("Initialize project, tickets and handover with prepare-claude first")
    state, marker = json.loads(state_path.read_text()), json.loads(marker_path.read_text())
    tickets = {key: value.get("ticket") for key, value in state.get("tasks", {}).items()}
    if not tickets or not all(tickets.values()) or marker != {"project_id": state.get("project_id"), "config_sha256": state.get("config_sha256"), "tickets": tickets}:
        raise ValueError("Claude preparation does not match the initialized project/tickets")
    env = environment(("CLAUDE_CONFIG_DIR",))
    auth = subprocess.run([args.claude, "auth", "status"], env=env, capture_output=True, text=True, timeout=20)
    info = json.loads(auth.stdout)
    ready = info.get("loggedIn") is True and info.get("authMethod") in ("claude.ai", "oauth_token")
    if args.doctor:
        print(json.dumps({"claude_authenticated": ready, "native_crew_started": False,
                          "requires": "Own existing Claude Code subscription and repository-scoped GitHub login"}))
        return 0 if ready else 2
    if not ready:
        raise ValueError("Native crew needs the colleague's own Claude Code subscription login; no account or plan is purchased")
    if not (root / "claude-crew/crew.json").is_file():
        raise ValueError("Prepare the Claude profile with tares_adapter.py prepare-claude first")
    api = Tares(root)
    remote = api.call("GET", "/api/projects/"+str(state["project_id"])+"/tickets")["tickets"]
    if not set(tickets.values()).issubset({ticket["id"] for ticket in remote}):
        raise ValueError("Prepared project tickets are absent from Tares; initialize the correct runtime")
    settings = api.call("GET", "/api/crew/settings")
    if settings.get("autonomy") != "L3-review" or settings.get("releaser") is not False:
        raise ValueError("This pilot requires L3-review and a disabled releaser; run prepare-claude")
    ports_file = root/"services.json"
    ports = json.loads(ports_file.read_text()) if ports_file.exists() else {"api_port": 18787, "mcp_port": 18788}
    env.update(FACTORY_CREW_HOME=str(root / "claude-crew"), CLAUDE_BIN=args.claude,
               TARESD_URL="http://127.0.0.1:"+str(ports["api_port"]), TARES_FACTORY_MCP_URL="http://127.0.0.1:"+str(ports["mcp_port"])+"/mcp",
               TARES_AUTH_TOKEN=(root / "root-token").read_text().strip())
    subprocess.run([args.tares, "factory", "doctor"], env=env, check=True)
    return subprocess.call([args.tares, "factory", "crew", "up"], env=env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)

"""Register the skill and the MCP server with OpenHarness.

Two things have to happen for `oh` to use this project:

1. The skill must live at `<config dir>/skills/<name>/SKILL.md`. This script
   symlinks the in-repo skill there, so the repo stays the single source of truth
   and edits take effect immediately.
2. The MCP server must appear in `<config dir>/settings.json` under
   `mcp_servers`. Note that OpenHarness 0.1.9 accepts `--mcp-config` and
   `--settings` on the command line but never reads them (both options are
   declared in `cli.py` and unused), so settings.json is the only path that
   works. `mcp_config.json` is kept in the repo as the portable artifact for
   other MCP hosts, and is rewritten from the same source here.

The script also checks the MCP client library inside the `oh` installation,
because 0.1.9 reads `tool.inputSchema` while the MCP SDK renamed that field to
`input_schema` in 2.0. With mcp 2.x installed, every MCP server connects, fails
on that attribute, and is silently reported as having no tools — the agent then
runs with no indicator tools at all and no error the user can see.

The config dir is `~/.openharness` unless `OPENHARNESS_CONFIG_DIR` says
otherwise.

    uv run python scripts/install_skill.py                     # ~/.openharness
    uv run python scripts/install_skill.py --config-dir var/oh-config
    uv run python scripts/install_skill.py --skill-only
    uv run python scripts/install_skill.py --uninstall

Verify with `oh --dry-run -p "analyse VNM"`: the skill should be listed and the
`ta-agent` MCP entry should resolve `(ok)`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "technical-analysis"
MCP_CONFIG = REPO_ROOT / "mcp_config.json"
SERVER_NAME = "ta-agent"

#: The pin that keeps oh 0.1.9's MCP client on the field name it actually reads.
MCP_PIN_COMMAND = "uv tool install 'openharness-ai==0.1.9' --with 'mcp<2' --force"


def default_config_dir() -> Path:
    env = os.environ.get("OPENHARNESS_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".openharness"


def server_config() -> dict:
    """The stdio server entry, derived from this checkout's absolute paths."""
    return {
        "type": "stdio",
        "command": str(REPO_ROOT / ".venv" / "bin" / "python"),
        "args": ["-m", "mcp_server.server"],
        "cwd": str(REPO_ROOT),
        "env": {
            "TA_AGENT_DB": str(REPO_ROOT / "var" / "ta.sqlite"),
            "TA_AGENT_AUDIT_LOG": str(REPO_ROOT / "logs" / "tool_calls.jsonl"),
            "TA_AGENT_LOG_LEVEL": "INFO",
        },
    }


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def refresh_mcp_config() -> None:
    """Keep mcp_config.json in step with this checkout's paths."""
    wanted = {"mcpServers": {SERVER_NAME: server_config()}}
    if _read_json(MCP_CONFIG) == wanted:
        print("mcp_config.json already current")
        return
    _write_json(MCP_CONFIG, wanted)
    print(f"wrote {MCP_CONFIG.relative_to(REPO_ROOT)}")


def oh_interpreter() -> Path | None:
    """The Python that runs `oh`, read from the launcher's shebang."""
    launcher = shutil.which("oh")
    if not launcher:
        return None
    try:
        first_line = Path(launcher).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first_line.startswith("#!"):
        return None
    candidate = Path(first_line[2:].strip().split()[0] if first_line[2:].strip() else "")
    return candidate if candidate.is_file() else None


def check_mcp_client_version() -> int:
    """Warn if `oh`'s MCP client is a version whose tool listing it cannot read."""
    python = oh_interpreter()
    if python is None:
        print("could not locate the oh interpreter; skipping the MCP client version check")
        return 0
    probe = subprocess.run(
        [str(python), "-c", "import importlib.metadata as m; print(m.version('mcp'))"],
        capture_output=True,
        text=True,
    )
    version = probe.stdout.strip()
    if probe.returncode != 0 or not version:
        print("could not read oh's mcp version; skipping the check")
        return 0
    try:
        major = int(version.split(".")[0])
    except ValueError:
        print(f"unrecognised mcp version {version!r}; skipping the check")
        return 0
    if major >= 2:
        print(
            f"WARNING: oh is using mcp {version}. OpenHarness 0.1.9 reads `tool.inputSchema`,\n"
            "  renamed in mcp 2.0, so it will connect to this server, fail on that attribute\n"
            "  and report zero tools without saying why. Pin its client:\n"
            f"    {MCP_PIN_COMMAND}\n"
            "  Then confirm with: uv run python scripts/loop_smoke.py"
        )
        return 1
    print(f"oh mcp client: {version} (compatible)")
    return 0


def install_skill(config_dir: Path) -> int:
    if not (SKILL_DIR / "SKILL.md").is_file():
        print(f"missing {SKILL_DIR / 'SKILL.md'}")
        return 1
    target = config_dir / "skills" / SKILL_DIR.name
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_symlink():
        if target.resolve() == SKILL_DIR:
            print(f"skill already installed: {target} -> {SKILL_DIR}")
            return 0
        print(f"replacing symlink {target} (was -> {os.readlink(target)})")
        target.unlink()
    elif target.exists():
        # A real directory here is someone else's skill, or a copy. Not ours to remove.
        print(f"refusing to overwrite existing directory {target}; move it aside first")
        return 1

    target.symlink_to(SKILL_DIR, target_is_directory=True)
    print(f"skill installed: {target} -> {SKILL_DIR}")
    return 0


def install_mcp(config_dir: Path) -> int:
    settings_path = config_dir / "settings.json"
    settings = _read_json(settings_path)
    servers = settings.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    wanted = server_config()
    if servers.get(SERVER_NAME) == wanted:
        print(f"mcp server already registered in {settings_path}")
        return 0
    servers[SERVER_NAME] = wanted
    settings["mcp_servers"] = servers
    _write_json(settings_path, settings)
    print(f"mcp server '{SERVER_NAME}' registered in {settings_path}")
    return 0


def uninstall(config_dir: Path) -> int:
    status = 0
    target = config_dir / "skills" / SKILL_DIR.name
    if target.is_symlink():
        target.unlink()
        print(f"removed symlink {target}")
    elif target.exists():
        print(f"{target} is a real directory, not our symlink; left alone")
        status = 1
    else:
        print(f"nothing installed at {target}")

    settings_path = config_dir / "settings.json"
    settings = _read_json(settings_path)
    servers = settings.get("mcp_servers")
    if isinstance(servers, dict) and SERVER_NAME in servers:
        del servers[SERVER_NAME]
        settings["mcp_servers"] = servers
        _write_json(settings_path, settings)
        print(f"removed mcp server '{SERVER_NAME}' from {settings_path}")
    else:
        print(f"no '{SERVER_NAME}' entry in {settings_path}")
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="OpenHarness config dir (default: $OPENHARNESS_CONFIG_DIR or ~/.openharness)",
    )
    parser.add_argument("--skill-only", action="store_true", help="skip MCP registration")
    parser.add_argument("--uninstall", action="store_true", help="undo both steps")
    args = parser.parse_args()

    config_dir = (args.config_dir or default_config_dir()).expanduser().resolve()
    if args.uninstall:
        return uninstall(config_dir)

    status = install_skill(config_dir)
    if not args.skill_only:
        refresh_mcp_config()
        status = install_mcp(config_dir) or status
        status = check_mcp_client_version() or status
    # --dry-run resolves the config but does not start the server, so it cannot
    # tell you whether the tools actually load; loop_smoke.py does.
    print(f'verify config with: OPENHARNESS_CONFIG_DIR={config_dir} oh --dry-run -p "analyse VNM"')
    print("verify the whole loop with: uv run python scripts/loop_smoke.py")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

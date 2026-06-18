import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("Tokenmaxxing")

# Public tunnels (Cloudflare quick tunnels, localtunnel, ngrok) hand out a random
# subdomain on every run, so an exact-match allowlist is unworkable. FastMCP's
# built-in Host/Origin check only understands exact matches and ":*" port
# wildcards, so we widen it to also accept "*.suffix" (host suffix, port-insensitive)
# and a bare "*" wildcard. The daemon is still gated behind a workspace grant.
TUNNEL_HOST_PATTERNS = [
    "*.trycloudflare.com",
    "*.loca.lt",
    "*.ngrok-free.app",
    "*.ngrok.io",
    "*.ngrok.app",
    "*.ts.net",  # Tailscale Funnel (https://<machine>.<tailnet>.ts.net)
]
TUNNEL_ORIGIN_PATTERNS = [
    "https://*.trycloudflare.com",
    "https://*.loca.lt",
    "https://*.ngrok-free.app",
    "https://*.ngrok.io",
    "https://*.ngrok.app",
    "https://*.ts.net",
]


def _value_matches_pattern(value: str, pattern: str) -> bool:
    """Match a Host/Origin value against an allowlist pattern.

    Supports exact match, ":*" trailing port wildcard, a bare "*", and
    "*.suffix" subdomain wildcards that ignore any trailing :port.
    """
    if pattern == "*" or value == pattern:
        return True
    if pattern.endswith(":*"):
        return value.startswith(pattern[:-2] + ":")
    if "*." in pattern:
        scheme, sep, host_pattern = pattern.rpartition("*.")
        # scheme is "" for hosts ("*.loca.lt") or "https://" for origins.
        value_host = value[len(scheme):] if scheme and value.startswith(scheme) else None
        if scheme and value_host is None:
            return False
        candidate = value if not scheme else value_host
        candidate = candidate.split(":", 1)[0] if "]" not in candidate else candidate
        return candidate == host_pattern or candidate.endswith("." + host_pattern)
    return False


def _install_wildcard_transport_security() -> None:
    """Teach FastMCP's transport-security middleware about suffix wildcards."""
    from mcp.server.transport_security import TransportSecurityMiddleware

    def _validate_host(self, host):
        if not host:
            return False
        return any(_value_matches_pattern(host, p) for p in self.settings.allowed_hosts)

    def _validate_origin(self, origin):
        if not origin:
            return True
        return any(_value_matches_pattern(origin, p) for p in self.settings.allowed_origins)

    TransportSecurityMiddleware._validate_host = _validate_host
    TransportSecurityMiddleware._validate_origin = _validate_origin

STATE_PATH = Path(os.environ.get("TOKENMAXXING_STATE", "~/.tokenmaxxing/state.json")).expanduser()
DEFAULT_IGNORE_DIRS = {
    ".git",
    ".next",
    ".pytest_cache",
    ".tokenmaxxing/runs",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
BLOCKED_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
}
BLOCKED_PARTS = {
    ".aws",
    ".ssh",
}
BLOCKED_SUFFIXES = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
}
PLAN_PATH = ".tokenmaxxing/plan.md"


def _now() -> int:
    return int(time.time())


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _active_grant() -> tuple[dict | None, str | None]:
    state = _load_state()
    grant = state.get("grant")
    if not grant:
        return None, "No active repository/workspace grant. Run: tokenmaxxing grant /path/to/repo --ttl 4h"

    expires_at = int(grant.get("expires_at", 0))
    if expires_at <= _now():
        return None, "Repository/workspace grant expired. Run: tokenmaxxing grant /path/to/repo --ttl 4h"

    workspace = Path(grant.get("workspace", "")).expanduser().resolve()
    if not workspace.is_dir():
        return None, f"Granted workspace is no longer available: {workspace}"

    grant["workspace"] = str(workspace)
    return grant, None


def _scope_kind(workspace: str) -> str:
    return "repository" if (Path(workspace) / ".git").exists() else "workspace"


def _scope_summary(workspace: str) -> dict:
    workspace_path = Path(workspace)
    kind = _scope_kind(workspace)
    return {
        "scope": kind,
        "name": workspace_path.name,
        "path_format": "workspace-relative paths only",
        "instruction": (
            "Stay inside this granted repository/workspace. Do not request arbitrary "
            "local machine directories. Use list_workspace_files, then read only paths "
            "returned by that tool."
        ),
    }


def _is_ignored_dir(rel_dir: str) -> bool:
    return rel_dir in DEFAULT_IGNORE_DIRS or any(
        rel_dir == ignored or rel_dir.startswith(f"{ignored}/") for ignored in DEFAULT_IGNORE_DIRS
    )


def _is_blocked_path(rel_path: str) -> bool:
    path = Path(rel_path)
    if path.name in BLOCKED_NAMES:
        return True
    if path.suffix in BLOCKED_SUFFIXES:
        return True
    if any(part in BLOCKED_PARTS for part in path.parts):
        return True
    if ".git" in path.parts and path.name in {"config", "credentials"}:
        return True
    return False


def _resolve_workspace_path(workspace: str, file_path: str) -> tuple[Path | None, str | None]:
    rel_path = Path(file_path)
    if rel_path.is_absolute():
        return None, (
            "Use workspace-relative paths only. This connector is scoped to the granted "
            "repository/workspace and cannot browse arbitrary local machine directories."
        )

    workspace_path = Path(workspace).resolve()
    target_path = (workspace_path / rel_path).resolve()
    try:
        target_path.relative_to(workspace_path)
    except ValueError:
        return None, "Path traversal detected. Access denied."

    normalized = target_path.relative_to(workspace_path).as_posix()
    if _is_blocked_path(normalized):
        return None, f"Blocked sensitive path: {normalized}"

    return target_path, None


def _agent_command(agent: str, prompt: str) -> list[str] | None:
    agent_map = {
        "claude": ["claude", "-p", prompt],
        "antigravity": ["agy", "-p", prompt],
        "codex": ["codex", "exec", prompt],
    }
    return agent_map.get(agent.lower().strip())


@mcp.tool()
def get_status() -> str:
    """Return lock state for the currently granted repository/workspace.

    The connector is intentionally scoped. Do not ask for arbitrary local machine
    directories or absolute paths; use list_workspace_files for allowed paths.
    """
    grant, error = _active_grant()
    if error:
        return json.dumps({"locked": True, "message": error}, indent=2)

    scope = _scope_summary(grant["workspace"])
    return json.dumps(
        {
            "locked": False,
            **scope,
            "expires_at": grant["expires_at"],
            "seconds_remaining": grant["expires_at"] - _now(),
            "allow_execute": bool(grant.get("allow_execute", False)),
        },
        indent=2,
    )


@mcp.tool()
def list_workspace_files() -> str:
    """List allowed workspace-relative files in the granted repository/workspace.

    Treat this list as the complete browsing scope. Do not request files or
    directories outside it.
    """
    grant, error = _active_grant()
    if error:
        return f"Locked: {error}"

    workspace = Path(grant["workspace"])
    files_list = []
    for root, dirs, files in os.walk(workspace):
        rel_root = Path(root).relative_to(workspace).as_posix()
        if rel_root == ".":
            rel_root = ""

        filtered_dirs = []
        for directory in dirs:
            rel_dir = f"{rel_root}/{directory}".strip("/")
            if not _is_ignored_dir(rel_dir) and not _is_blocked_path(rel_dir):
                filtered_dirs.append(directory)
        dirs[:] = filtered_dirs

        for file_name in files:
            rel_path = f"{rel_root}/{file_name}".strip("/")
            if not _is_blocked_path(rel_path):
                files_list.append(rel_path)

    scope = _scope_summary(grant["workspace"])
    header = (
        f"Scope: {scope['scope']} '{scope['name']}'.\n"
        "Use only workspace-relative paths listed below. Do not request arbitrary local directories.\n"
    )
    body = "\n".join(sorted(files_list)) if files_list else "No files found in workspace."
    return header + body


@mcp.tool()
def read_workspace_file(file_path: str) -> str:
    """Read one workspace-relative file from the granted repository/workspace.

    Pass only paths returned by list_workspace_files. Absolute paths and paths
    outside the granted scope are refused.
    """
    grant, error = _active_grant()
    if error:
        return f"Locked: {error}"

    target_path, error = _resolve_workspace_path(grant["workspace"], file_path)
    if error:
        return f"Error: {error}"
    if not target_path.exists() or not target_path.is_file():
        return f"Error: File not found in the granted scope: {file_path}. Use list_workspace_files first."

    try:
        return target_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "Error: File is not UTF-8 text."
    except Exception as exc:
        return f"Error reading file: {exc}"


@mcp.tool()
def write_handover_plan(plan_markdown: str) -> str:
    """Write the handoff plan to .tokenmaxxing/plan.md in the granted workspace."""
    grant, error = _active_grant()
    if error:
        return f"Locked: {error}"

    target_path, error = _resolve_workspace_path(grant["workspace"], PLAN_PATH)
    if error:
        return f"Error: {error}"

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(plan_markdown, encoding="utf-8")
    return f"Successfully wrote handoff plan to {PLAN_PATH}"


@mcp.tool()
def execute_handover(agent: str = "codex") -> str:
    """Execute .tokenmaxxing/plan.md with a local CLI agent if execution is trusted for this grant."""
    grant, error = _active_grant()
    if error:
        return f"Locked: {error}"
    if not grant.get("allow_execute", False):
        return "Execution locked. Run: tokenmaxxing grant /path/to/repo --ttl 2h --allow-execute"

    workspace = Path(grant["workspace"])
    plan_path = workspace / PLAN_PATH
    if not plan_path.exists():
        return f"Error: No handoff plan found at {PLAN_PATH}."

    prompt = (
        "Please read the plan in .tokenmaxxing/plan.md and implement the requested changes. "
        "Verify your changes using the verification commands, then report back when finished."
    )
    cmd = _agent_command(agent, prompt)
    if not cmd:
        return "Error: Unknown agent. Supported agents: claude, opencode, codex."

    runs_dir = workspace / ".tokenmaxxing" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = runs_dir / f"{_now()}-{agent}.log"

    result = subprocess.run(cmd, cwd=workspace, capture_output=True, text=True, timeout=1800)
    log = [
        "=== CLI AGENT EXECUTION LOG ===",
        f"Command: {' '.join(cmd)}",
        f"Workspace: {workspace}",
        f"Exit Code: {result.returncode}",
        "",
        "--- STDOUT ---",
        result.stdout or "(no stdout)",
        "",
        "--- STDERR ---",
        result.stderr or "(no stderr)",
    ]
    log_path.write_text("\n".join(log), encoding="utf-8")
    return f"Agent exited with code {result.returncode}. Log written to {log_path.relative_to(workspace)}"


def main():
    parser = argparse.ArgumentParser(description="Tokenmaxxing MCP server")
    parser.add_argument("--transport", default="sse", choices=["stdio", "sse", "streamable-http"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument("--allowed-origin", action="append", default=[])
    args = parser.parse_args()

    print(f"Starting Tokenmaxxing daemon. State: {STATE_PATH}")
    _install_wildcard_transport_security()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.transport_security.allowed_hosts = [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        *TUNNEL_HOST_PATTERNS,
        *args.allowed_host,
    ]
    mcp.settings.transport_security.allowed_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "https://chatgpt.com",
        "https://chat.openai.com",
        *TUNNEL_ORIGIN_PATTERNS,
        *args.allowed_origin,
    ]
    if args.transport == "sse":
        print(f"Running MCP SSE on http://{args.host}:{args.port}/sse")
        mcp.run(transport="sse")
    elif args.transport == "streamable-http":
        print(f"Running MCP Streamable HTTP on http://{args.host}:{args.port}/mcp")
        mcp.run(transport="streamable-http")
    else:
        print("Running MCP over stdio")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

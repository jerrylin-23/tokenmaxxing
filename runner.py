import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


STATE_PATH = Path(os.environ.get("TOKENMAXXING_STATE", "~/.tokenmaxxing/state.json")).expanduser()
PLAN_PATH = ".tokenmaxxing/plan.md"


def parse_ttl(value: str) -> int:
    match = re.fullmatch(r"(\d+)([smhd]?)", value.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError("TTL must look like 30m, 4h, 1d, or 3600.")

    amount = int(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return amount * multiplier


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    STATE_PATH.chmod(0o600)


def active_grant() -> tuple[dict | None, str | None]:
    state = load_state()
    grant = state.get("grant")
    if not grant:
        return None, "No active workspace grant."
    if int(grant.get("expires_at", 0)) <= int(time.time()):
        return None, "Workspace grant expired."
    workspace = Path(grant.get("workspace", "")).expanduser().resolve()
    if not workspace.is_dir():
        return None, f"Workspace no longer exists: {workspace}"
    grant["workspace"] = str(workspace)
    return grant, None


def agent_command(agent: str, prompt: str) -> list[str] | None:
    return {
        "claude": ["claude", "-p", prompt],
        "antigravity": ["agy", "-p", prompt],
        "codex": ["codex", "exec", "--skip-git-repo-check", "--sandbox", "danger-full-access", prompt],
    }.get(agent.lower().strip())


def agent_interactive_command(agent: str, prompt: str) -> list[str] | None:
    """Interactive variants: seed the plan prompt but keep a live session open so
    the user can continue working with the agent after the initial pass."""
    return {
        "claude": ["claude", "--dangerously-skip-permissions", prompt],
        "antigravity": ["agy", "--dangerously-skip-permissions", "--prompt-interactive", prompt],
        "codex": ["codex", "--sandbox", "danger-full-access", prompt],
    }.get(agent.lower().strip())


def process_text(value: str | bytes | None) -> str:
    if value is None:
        return "(no output)"
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace") or "(no output)"
    return value or "(no output)"


def grant_workspace(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"[Error] Not a directory: {workspace}")
        sys.exit(1)

    expires_at = int(time.time()) + args.ttl_seconds
    state = {
        "grant": {
            "workspace": str(workspace),
            "expires_at": expires_at,
            "allow_execute": args.allow_execute,
        }
    }
    save_state(state)
    mode = "read/write plan + execute" if args.allow_execute else "read/write plan only"
    print(f"[Grant] {workspace}")
    print(f"[Grant] Mode: {mode}")
    print(f"[Grant] Expires in: {args.ttl_seconds} seconds")


def print_status(_: argparse.Namespace) -> None:
    grant, error = active_grant()
    if error:
        print(f"[Locked] {error}")
        print(f"[State] {STATE_PATH}")
        return

    remaining = int(grant["expires_at"]) - int(time.time())
    print("[Unlocked]")
    print(f"Workspace: {grant['workspace']}")
    print(f"Seconds remaining: {remaining}")
    print(f"Execution trusted: {bool(grant.get('allow_execute', False))}")
    print(f"State: {STATE_PATH}")


def revoke(_: argparse.Namespace) -> None:
    save_state({})
    print("[Revoke] Cleared active workspace grant.")


def daemon(args: argparse.Namespace) -> None:
    import server
    # Strip the 'daemon' command from sys.argv so server's parser works correctly
    new_args = [sys.argv[0]]
    if len(sys.argv) > 2:
        # Keep everything after the 'daemon' subcommand
        idx = sys.argv.index("daemon")
        new_args.extend(sys.argv[idx+1:])
    sys.argv = new_args
    print(f"[Daemon] Starting MCP server directly in-process...")
    sys.stdout.flush()
    server.main()


def launch_agent_plist(label: str, host: str, port: int, allowed_hosts: list[str], allowed_origins: list[str], transport: str = "streamable-http") -> str:
    runner_path = Path(__file__).resolve()
    venv_python = runner_path.parent / ".venv" / "bin" / "python"
    python_path = venv_python if venv_python.exists() else Path(sys.executable).resolve()
    args = [
        str(python_path),
        str(runner_path),
        "daemon",
        "--transport",
        transport,
        "--host",
        host,
        "--port",
        str(port),
    ]
    for allowed_host in allowed_hosts:
        args.extend(["--allowed-host", allowed_host])
    for allowed_origin in allowed_origins:
        args.extend(["--allowed-origin", allowed_origin])
    arg_xml = "\n".join(f"    <string>{html.escape(arg)}</string>" for arg in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{html.escape(label)}</string>
  <key>ProgramArguments</key>
  <array>
{arg_xml}
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{html.escape(str(Path.home() / ".tokenmaxxing" / "daemon.log"))}</string>
  <key>StandardErrorPath</key>
  <string>{html.escape(str(Path.home() / ".tokenmaxxing" / "daemon.err.log"))}</string>
</dict>
</plist>
"""


def install_launchagent(args: argparse.Namespace) -> None:
    plist = launch_agent_plist(args.label, args.host, args.port, args.allowed_host, args.allowed_origin, args.transport)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{args.label}.plist"

    if args.dry_run:
        print(plist)
        return

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    (Path.home() / ".tokenmaxxing").mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist, encoding="utf-8")
    print(f"[LaunchAgent] Wrote {plist_path}")

    if args.load:
        launchctl = shutil.which("launchctl")
        if not launchctl:
            print("[LaunchAgent] launchctl not found; load it manually after login.")
            return
        subprocess.run([launchctl, "unload", str(plist_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = subprocess.run([launchctl, "load", str(plist_path)])
        if result.returncode == 0:
            print("[LaunchAgent] Loaded daemon.")
        else:
            print(f"[LaunchAgent] launchctl load exited with {result.returncode}.")


def uninstall_launchagent(args: argparse.Namespace) -> None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{args.label}.plist"
    if args.unload:
        launchctl = shutil.which("launchctl")
        if launchctl and plist_path.exists():
            subprocess.run([launchctl, "unload", str(plist_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("[LaunchAgent] Unloaded daemon.")

    if plist_path.exists():
        plist_path.unlink()
        print(f"[LaunchAgent] Removed {plist_path}")
    else:
        print(f"[LaunchAgent] Not installed: {plist_path}")


def execute(args: argparse.Namespace) -> None:
    grant, error = active_grant()
    if error:
        print(f"[Error] {error} Run: tokenmaxxing grant /path/to/repo --ttl 4h")
        sys.exit(1)

    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else Path(grant["workspace"])
    prompt = (
        "Please read the plan in .tokenmaxxing/plan.md and implement the requested changes. "
        "Verify your changes using the verification commands, then report back when finished."
    )

    if getattr(args, "interactive", False):
        icmd = agent_interactive_command(args.agent, prompt)
        if not icmd:
            print("[Error] Unknown agent. Supported agents: claude, antigravity, codex.")
            sys.exit(1)
        plan_path = workspace / PLAN_PATH
        if not plan_path.exists():
            print(f"[Error] No handoff plan found at {plan_path}")
            sys.exit(1)
        if args.dry_run:
            print(f"[Dry Run] Interactive workspace: {workspace}")
            print(f"[Dry Run] Command: {' '.join(icmd)}")
            return
        os.chdir(workspace)
        print(f"[Execution] Interactive session in {workspace}")
        print(f"[Execution] Command: {' '.join(icmd)}")
        print("[Execution] Handing control to the agent — continue the work in this window.")
        sys.stdout.flush()
        try:
            # Replace this process with the agent so it inherits the real TTY.
            os.execvp(icmd[0], icmd)
        except FileNotFoundError:
            print(f"[Error] Agent CLI not found on PATH: {icmd[0]}")
            sys.exit(127)

    cmd = agent_command(args.agent, prompt)
    if not cmd:
        print("[Error] Unknown agent. Supported agents: claude, opencode, codex.")
        sys.exit(1)

    if args.dry_run:
        print(f"[Dry Run] Workspace: {workspace}")
        print(f"[Dry Run] Command: {' '.join(cmd)}")
        plan_path = workspace / PLAN_PATH
        print(f"[Dry Run] Plan: {plan_path}")
        print(f"[Dry Run] Plan exists: {plan_path.exists()}")
        return

    plan_path = workspace / PLAN_PATH
    if not plan_path.exists():
        print(f"[Error] No handoff plan found at {plan_path}")
        sys.exit(1)

    runs_dir = workspace / ".tokenmaxxing" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = runs_dir / f"{int(time.time())}-{args.agent}.log"

    print(f"[Execution] Workspace: {workspace}")
    print(f"[Execution] Command: {' '.join(cmd)}")
    sys.stdout.flush()

    import threading

    proc = subprocess.Popen(
        cmd,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    stdout_lines = []

    def read_stream():
        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                stdout_lines.append(line)
        except Exception:
            pass

    t = threading.Thread(target=read_stream, daemon=True)
    t.start()

    timed_out = False
    try:
        proc.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        timed_out = True
        t.join(timeout=1.0)

    stdout_content = "".join(stdout_lines)

    if timed_out:
        log_path.write_text(
            "\n".join(
                [
                    "=== CLI AGENT EXECUTION LOG ===",
                    f"Command: {' '.join(cmd)}",
                    f"Workspace: {workspace}",
                    f"Exit Code: timeout",
                    f"Timeout Seconds: {args.timeout}",
                    "",
                    "--- OUTPUT ---",
                    stdout_content,
                ]
            ),
            encoding="utf-8",
        )
        print(f"\n[Execution] Timed out after {args.timeout} seconds")
        print(f"[Execution] Log: {log_path}")
        sys.exit(124)

    log_path.write_text(
        "\n".join(
            [
                "=== CLI AGENT EXECUTION LOG ===",
                f"Command: {' '.join(cmd)}",
                f"Workspace: {workspace}",
                f"Exit Code: {proc.returncode}",
                "",
                "--- OUTPUT ---",
                stdout_content,
            ]
        ),
        encoding="utf-8",
    )
    print(f"\n[Execution] Exit code: {proc.returncode}")
    print(f"[Execution] Log: {log_path}")
    sys.exit(proc.returncode)


def run_gui(_: argparse.Namespace) -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gui
        gui.main()
    except Exception as exc:
        print(f"[Error] Failed to launch GUI: {exc}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Tokenmaxxing local handoff daemon")
    subparsers = parser.add_subparsers(dest="command")

    gui_parser = subparsers.add_parser("gui", help="Run the Tokenmaxxing standalone desktop GUI")
    gui_parser.set_defaults(func=run_gui)

    daemon_parser = subparsers.add_parser("daemon", help="Run the always-on MCP daemon")
    daemon_parser.add_argument("--transport", default="sse", choices=["sse", "stdio", "streamable-http"])
    daemon_parser.add_argument("--host", default="127.0.0.1")
    daemon_parser.add_argument("--port", type=int, default=8000)
    daemon_parser.add_argument("--allowed-host", action="append", default=[])
    daemon_parser.add_argument("--allowed-origin", action="append", default=[])
    daemon_parser.set_defaults(func=daemon)

    grant_parser = subparsers.add_parser("grant", help="Grant a workspace for a limited time")
    grant_parser.add_argument("workspace")
    grant_parser.add_argument("--ttl", default="4h", help="Grant TTL, e.g. 30m, 4h, 1d")
    grant_parser.add_argument("--allow-execute", action="store_true", help="Allow web-triggered execute_handover")
    grant_parser.set_defaults(func=grant_workspace)

    status_parser = subparsers.add_parser("status", help="Show active grant state")
    status_parser.set_defaults(func=print_status)

    revoke_parser = subparsers.add_parser("revoke", help="Clear the active workspace grant")
    revoke_parser.set_defaults(func=revoke)

    install_parser = subparsers.add_parser("install-launchagent", help="Install a macOS LaunchAgent for login startup")
    install_parser.add_argument("--label", default="io.jerrylin.tokenmaxxing")
    install_parser.add_argument("--transport", default="streamable-http", choices=["sse", "stdio", "streamable-http"], help="Transport the always-on daemon serves (ChatGPT Web needs streamable-http)")
    install_parser.add_argument("--host", default="127.0.0.1")
    install_parser.add_argument("--port", type=int, default=8000)
    install_parser.add_argument("--allowed-host", action="append", default=[])
    install_parser.add_argument("--allowed-origin", action="append", default=[])
    install_parser.add_argument("--load", action="store_true", help="Load the LaunchAgent immediately")
    install_parser.add_argument("--dry-run", action="store_true", help="Print the plist without writing it")
    install_parser.set_defaults(func=install_launchagent)

    uninstall_parser = subparsers.add_parser("uninstall-launchagent", help="Remove the macOS LaunchAgent")
    uninstall_parser.add_argument("--label", default="io.jerrylin.tokenmaxxing")
    uninstall_parser.add_argument("--unload", action="store_true", help="Unload before removing")
    uninstall_parser.set_defaults(func=uninstall_launchagent)

    execute_parser = subparsers.add_parser("execute", help="Run the current handoff plan locally")
    execute_parser.add_argument("--agent", default="codex", choices=["claude", "antigravity", "codex"])
    execute_parser.add_argument("--workspace", help="Override the granted workspace")
    execute_parser.add_argument("--dry-run", action="store_true")
    execute_parser.add_argument("--interactive", action="store_true", help="Launch the agent as a live interactive session (hands over the TTY) so you can continue the work")
    execute_parser.add_argument("--timeout", type=int, default=1800, help="Execution timeout in seconds")
    execute_parser.set_defaults(func=execute)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if hasattr(args, "ttl"):
        try:
            args.ttl_seconds = parse_ttl(args.ttl)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    args.func(args)


if __name__ == "__main__":
    main()

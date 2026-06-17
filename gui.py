import os
import sys
import json
import time
import shlex
import socket
import shutil
import threading
import subprocess
import webbrowser
import urllib.request
import urllib.error
import tempfile
from pathlib import Path

import webview

RUN_DIR = Path(tempfile.gettempdir()) / "tokenmaxxing-demo"

if getattr(sys, "frozen", False):
    # Packaged build: locate the repo root (with demo.sh) relative to the executable.
    exe_dir = Path(sys.executable).resolve().parent
    REPO_DIR = exe_dir
    for p in [exe_dir, *list(exe_dir.parents)[:5]]:
        if (p / "demo.sh").exists():
            REPO_DIR = p
            break
else:
    REPO_DIR = Path(__file__).resolve().parent

DEMO_SH = REPO_DIR / "demo.sh"
STATE_FILE = Path("~/.tokenmaxxing/state.json").expanduser()
AGENTS = ["antigravity", "claude", "codex"]
TTLS = ["1h", "2h", "4h", "8h", "12h", "24h"]


def _venv_python():
    venv = REPO_DIR / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


class Api:
    """Bridge exposed to the web UI as window.pywebview.api.*"""

    def __init__(self):
        self.window = None
        self.workspace = self._load_last_workspace()
        self.agent = "codex"
        self.ttl = "4h"
        self.new_terminal = True
        self.is_transitioning = False

    def bind(self, window):
        self.window = window

    # ---- JS callable -------------------------------------------------
    def init(self):
        st = self._status()
        return {
            "workspace": self.workspace,
            "agent": self.agent,
            "ttl": self.ttl,
            "agents": AGENTS,
            "ttls": TTLS,
            "new_terminal": self.new_terminal,
            **st,
        }

    def get_status(self):
        return self._status()

    def set_workspace(self, path):
        if path:
            self.workspace = path
            self._save_last_workspace(path)
        return {"workspace": self.workspace}

    def set_agent(self, agent):
        if agent in AGENTS:
            self.agent = agent
        return {"agent": self.agent}

    def set_ttl(self, ttl):
        if ttl in TTLS:
            self.ttl = ttl
        return {"ttl": self.ttl}

    def set_new_terminal(self, value):
        self.new_terminal = bool(value)
        return {"new_terminal": self.new_terminal}

    def browse_workspace(self):
        try:
            res = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=self.workspace)
        except Exception:
            res = None
        if res:
            path = res[0] if isinstance(res, (list, tuple)) else res
            self.workspace = path
            self._save_last_workspace(path)
            self._log(f"[GUI] Selected workspace: {path}")
            return {"workspace": path, "plan": self.get_plan()}
        return {"workspace": self.workspace}

    def open_url(self, url):
        if url:
            webbrowser.open(url)
        return True

    # ---- service controls -------------------------------------------
    def _get_runner_cmd(self, subcommand, *args):
        if getattr(sys, "frozen", False):
            return [sys.executable, subcommand] + list(args)
        else:
            return [sys.executable, str(REPO_DIR / "runner.py"), subcommand] + list(args)

    def _daemon_healthy(self):
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8000/mcp",
                method="POST",
                data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}',
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=1.5) as conn:
                return conn.status == 200
        except Exception:
            return False

    def start_service(self):
        if not self.workspace or not Path(self.workspace).is_dir():
            return {"ok": False, "error": f"Workspace directory does not exist:\n{self.workspace}"}
        self._save_last_workspace(self.workspace)

        self._log("\n[GUI] Verifying Tailscale status…")
        ok, err, turl = self._verify_tailscale()
        if not ok:
            self._log(f"[GUI] Tailscale verification failed: {err.splitlines()[0]}")
            return {"ok": False, "setup": {"message": err, "url": turl, "kind": "tailscale"}}
        self._log("[GUI] Tailscale is active and running.")

        self._log("[GUI] Verifying Tailscale Funnel is enabled…")
        f_ok, f_msg, f_url = self._verify_funnel()
        if not f_ok:
            self._log(f"[GUI] Funnel not ready: {f_msg.splitlines()[0]}")
            return {"ok": False, "setup": {"message": f_msg, "url": f_url, "kind": "funnel"}}
        self._log("[GUI] Tailscale Funnel is enabled for this node.")

        self.is_transitioning = True
        threading.Thread(target=self._start_service_thread, daemon=True).start()
        return {"ok": True}

    def _start_service_thread(self):
        try:
            self._log("\n[GUI] Starting handoff service...")
            
            # 1. Unload conflicting LaunchAgent if active
            uid = os.getuid()
            label = "io.jerrylin.tokenmaxxing"
            plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            try:
                res = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
                if label in res.stdout:
                    self._log(f"[GUI] Evicting always-on LaunchAgent ({label}) for the session...")
                    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
                    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
                    time.sleep(1)
            except Exception as e:
                self._log(f"[Warning] Failed to check/unload LaunchAgent: {e}")
            
            # 2. Start MCP Daemon
            if self._daemon_healthy():
                self._log("[GUI] Reusing daemon already healthy on 127.0.0.1:8000.")
            else:
                self._log("[GUI] Starting MCP daemon (streamable-http) on 127.0.0.1:8000...")
                cmd = self._get_runner_cmd("daemon", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8000")
                RUN_DIR.mkdir(parents=True, exist_ok=True)
                daemon_log_path = RUN_DIR / "daemon.log"
                with open(daemon_log_path, "w") as f:
                    proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
                (RUN_DIR / "daemon.pid").write_text(str(proc.pid))
                
                healthy = False
                for _ in range(30):
                    if self._daemon_healthy():
                        healthy = True
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.5)
                
                if healthy:
                    self._log("[GUI] Daemon healthy (local /mcp responding).")
                else:
                    self._log(f"[Error] Daemon startup failed. Check logs at {daemon_log_path}")
                    self.is_transitioning = False
                    self._emit("window.refreshStatus && window.refreshStatus()")
                    return
            
            # 3. Enable Tailscale funnel
            ts = self._resolve_tailscale()
            if not ts:
                self._log("[Error] Tailscale binary not found.")
                self.is_transitioning = False
                self._emit("window.refreshStatus && window.refreshStatus()")
                return
                
            try:
                res = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=3)
                data = json.loads(res.stdout or "{}")
                dns_name = (data.get("Self", {}) or {}).get("DNSName", "").rstrip(".")
                if not dns_name:
                    self._log("[Error] Could not determine MagicDNS name.")
                    self.is_transitioning = False
                    self._emit("window.refreshStatus && window.refreshStatus()")
                    return
            except Exception as e:
                self._log(f"[Error] Failed to read Tailscale status: {e}")
                self.is_transitioning = False
                self._emit("window.refreshStatus && window.refreshStatus()")
                return

            self._log(f"[GUI] Enabling Tailscale Funnel for 127.0.0.1:8000 on {dns_name}...")
            try:
                res = subprocess.run([ts, "funnel", "--bg", "8000"], capture_output=True, text=True, timeout=15)
                if res.returncode != 0:
                    self._log(f"[Error] Tailscale funnel failed: {res.stderr or res.stdout}")
                    self.is_transitioning = False
                    self._emit("window.refreshStatus && window.refreshStatus()")
                    return
            except Exception as e:
                self._log(f"[Error] Failed to start Tailscale funnel: {e}")
                self.is_transitioning = False
                self._emit("window.refreshStatus && window.refreshStatus()")
                return
                
            # 4. Grant workspace
            self._log(f"[GUI] Granting workspace {self.workspace} for {self.ttl}...")
            grant_cmd = self._get_runner_cmd("grant", self.workspace, "--ttl", self.ttl)
            res = subprocess.run(grant_cmd, capture_output=True, text=True)
            if res.returncode != 0:
                self._log(f"[Error] Failed to grant workspace: {res.stderr or res.stdout}")
                self.is_transitioning = False
                self._emit("window.refreshStatus && window.refreshStatus()")
                return
                
            # 5. Verify public endpoint
            url = f"https://{dns_name}/mcp"
            (RUN_DIR / "connector_url").write_text(url)
            self._log(f"[GUI] Tunnel URL: {url}")
            self._log("[GUI] Verifying public MCP handshake...")
            
            handshake_ok = False
            for attempt in range(1, 7):
                probe = self._probe_tunnel(url)
                if probe.get("state") == "green":
                    handshake_ok = True
                    self._log(f"[GUI] Public handshake OK (attempt {attempt}).")
                    break
                time.sleep(3)
                
            if not handshake_ok:
                self._log("[Warning] Public handshake did not succeed. Tunnel is active but handshake check failed.")
            
            self._log("[GUI] Service successfully started!")
            self._log("============================================================")
            self._log(f" ChatGPT custom connector URL: {url}")
            self._log(f" Workspace: {self.workspace} (TTL {self.ttl})")
            self._log("============================================================")
            
        except Exception as e:
            self._log(f"[Error] Start service failed: {e}")
        finally:
            self.is_transitioning = False
            self._emit("window.refreshStatus && window.refreshStatus()")

    def stop_service(self):
        self.is_transitioning = True
        threading.Thread(target=self._stop_service_thread, daemon=True).start()
        return {"ok": True}

    def _stop_service_thread(self):
        try:
            self._log("\n[GUI] Stopping tunnel and daemon...")
            
            # 1. Revoke grant
            self._log("[GUI] Revoking workspace grant...")
            cmd = self._get_runner_cmd("revoke")
            subprocess.run(cmd, capture_output=True)
            
            # 2. Turn off funnel
            self._log("[GUI] Disabling Tailscale funnel...")
            ts = self._resolve_tailscale()
            if ts:
                subprocess.run([ts, "funnel", "--https=443", "off"], capture_output=True, timeout=5)
                
            # 3. Stop daemon
            pid_file = RUN_DIR / "daemon.pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    self._log(f"[GUI] Stopping daemon process (PID {pid})...")
                    os.kill(pid, 15)
                    time.sleep(0.5)
                    os.kill(pid, 9)
                except Exception:
                    pass
                finally:
                    try:
                        pid_file.unlink()
                    except Exception:
                        pass
            
            try:
                (RUN_DIR / "connector_url").unlink()
            except Exception:
                pass
                
            self._log("[GUI] Service stopped.")
            
        except Exception as e:
            self._log(f"[Error] Stop service failed: {e}")
        finally:
            self.is_transitioning = False
            self._emit("window.refreshStatus && window.refreshStatus()")

    def run_agent(self):
        agent = self.agent
        plan_file = Path(self.workspace) / ".tokenmaxxing" / "plan.md"
        if not plan_file.exists():
            return {"ok": False, "no_plan": True, "plan_path": str(plan_file)}
        self._spawn_agent(agent, confirmed=True)
        return {"ok": True}

    def run_agent_confirmed(self):
        self._spawn_agent(self.agent, confirmed=True)
        return {"ok": True}

    def fix_setup(self, problem):
        """Launch the selected CLI agent in a Terminal to help fix Tailscale setup,
        seeded with the detected problem and the target Funnel configuration."""
        agent = self.agent
        prompt = self._setup_prompt(problem or "Tailscale Funnel is not configured.")
        self._log(f"\n[GUI] Launching '{agent}' to help set up Tailscale Funnel…")
        if not self._launch_prompt_terminal(agent, prompt, f"{agent} · Tailscale setup"):
            self._log("[GUI] Could not open a Terminal for setup help.")
            return {"ok": False}
        self._log(f"[GUI] '{agent}' is now helping with Tailscale setup in a Terminal window.")
        return {"ok": True}

    @staticmethod
    def _setup_prompt(problem):
        return (
            "I'm configuring Tailscale Funnel on this Mac so a local MCP server — the "
            "\"tokenmaxxing\" handoff server listening on http://127.0.0.1:8000 — can be exposed "
            "to ChatGPT over a public HTTPS URL.\n\n"
            f"The setup check reported this problem:\n{problem}\n\n"
            "Please help me fix it step by step, running the necessary shell commands and "
            "explaining as you go. Target state:\n"
            "  1. Tailscale installed and logged in  (check: tailscale status; if missing: "
            "brew install --cask tailscale, then tailscale up).\n"
            "  2. MagicDNS + HTTPS certificates enabled for the tailnet "
            "(admin console: https://login.tailscale.com/admin/dns).\n"
            "  3. This node granted the \"funnel\" node attribute in the ACL policy "
            "(admin console: https://login.tailscale.com/admin/acls/file).\n"
            "  4. Then run: tailscale funnel --bg 8000  and confirm the public "
            "https://<magicdns-name>.ts.net URL responds.\n\n"
            "Diagnose with `tailscale status --json` and `tailscale funnel status`. Anything that "
            "needs the Tailscale admin console requires me to click — give me the exact link and "
            "what to change. Don't touch anything unrelated to this Tailscale Funnel setup."
        )

    def _spawn_agent(self, agent, confirmed):
        py = _venv_python()
        runner = str(REPO_DIR / "runner.py")
        if self.new_terminal:
            self._log(f"\n[GUI] Launching '{agent}' in a new Terminal window…")
            if self._launch_agent_terminal(agent, self.workspace, py, runner):
                self._log(f"[GUI] Agent '{agent}' is now running in its own Terminal window.")
                self._log("[GUI] Watch that window for live, interactive output.")
                return
            self._log("[GUI] Could not open a Terminal window; running in this console instead.")
        self._log(f"\n[GUI] Spawning local agent: {agent}…")
        cmd = [py, runner, "execute", "--agent", agent, "--workspace", self.workspace]
        threading.Thread(target=self._run_stream, args=(cmd, self.workspace, None, True),
                         daemon=True).start()

    # ---- plan document ----------------------------------------------
    def get_plan_md(self):
        plan_file = Path(self.workspace) / ".tokenmaxxing" / "plan.md"
        if not plan_file.exists():
            return {"text": "", "missing": str(plan_file)}
        try:
            return {"text": plan_file.read_text(encoding="utf-8")}
        except Exception as exc:
            return {"text": "", "error": str(exc)}

    def get_tunnel_health(self):
        url = (RUN_DIR / "connector_url")
        if not self._daemon_alive() or not url.exists():
            return {"state": "off", "text": "Idle"}
        return self._probe_tunnel(url.read_text().strip())

    # ==================================================================
    # internals
    # ==================================================================
    def _emit(self, js):
        if not self.window:
            return
        try:
            self.window.evaluate_js(js)
        except Exception:
            pass

    @staticmethod
    def _classify(line):
        low = line.lower()
        if any(t in line for t in ("[Error]", "Exception", "Traceback")) or \
           any(t in low for t in ("error", "failed", "failure", "fatal", "denied")):
            return "error"
        if "[Success]" in line or "exit code: 0" in low or "successfully" in low or \
           "handshake ok" in low or "✓" in line:
            return "success"
        if any(t in low for t in ("warn", "warning", "timeout", "timed out", "retry", "evicting")):
            return "warn"
        if line.strip().startswith(("[GUI]", "[Execution]", "[Daemon]", "›")):
            return "info"
        return "plain"

    def _log(self, line):
        self._emit(f"window.appendLog({json.dumps(line)}, {json.dumps(self._classify(line))})")

    def _run_stream(self, cmd, cwd, env, is_agent=False):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, cwd=cwd, env=env)
            for line in proc.stdout:
                self._log(line.rstrip("\n"))
            proc.wait()
            rc = proc.returncode
            if is_agent:
                self._log(f"[GUI] Local execution finished (exit code {rc}).")
            else:
                self.is_transitioning = False
                self._log("[GUI] Done." if rc == 0 else f"[GUI] Process exited with code {rc}.")
        except Exception as exc:
            self.is_transitioning = False
            self._log(f"[Error] {exc}")
        self._emit("window.refreshStatus && window.refreshStatus()")

    def _status(self):
        running = self._daemon_alive()
        url = ""
        url_file = RUN_DIR / "connector_url"
        if running and url_file.exists():
            url = url_file.read_text().strip()
        return {
            "running": running,
            "transitioning": self.is_transitioning,
            "url": url,
            "time_left": self._time_left() if running else "--",
        }

    def _daemon_alive(self):
        pid_file = RUN_DIR / "daemon.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 0)
                return True
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        return False

    def _time_left(self):
        try:
            res = subprocess.run([_venv_python(), str(REPO_DIR / "runner.py"), "status"],
                                 capture_output=True, text=True, timeout=1)
            for line in res.stdout.splitlines():
                if "Seconds remaining:" in line:
                    sec = int(line.split(":")[1].strip())
                    if sec <= 0:
                        return "expired"
                    m, s = divmod(sec, 60)
                    h, m = divmod(m, 60)
                    return f"{h:02d}h {m:02d}m" if h else f"{m:02d}m {s:02d}s"
            if "No active workspace grant" in res.stdout or "[Locked]" in res.stdout:
                return "locked"
        except Exception:
            pass
        return "--"

    # ---- workspace state --------------------------------------------
    def _load_last_workspace(self):
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if state.get("last_workspace"):
                    return state["last_workspace"]
                if state.get("grant", {}).get("workspace"):
                    return state["grant"]["workspace"]
            except Exception:
                pass
        return str(Path.home() / "career-ops")

    def _save_last_workspace(self, path):
        try:
            state = {}
            if STATE_FILE.exists():
                try:
                    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            state["last_workspace"] = str(Path(path).resolve())
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass

    # ---- tailscale / funnel -----------------------------------------
    def _resolve_tailscale(self):
        for p in (shutil.which("tailscale"), "/usr/local/bin/tailscale",
                  "/opt/homebrew/bin/tailscale",
                  "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
            if p and os.path.exists(p):
                return p
        return None

    def _verify_tailscale(self):
        ts = self._resolve_tailscale()
        if not ts:
            return False, ("Tailscale is not installed on this machine.\n\n"
                           "Install it, then log in before starting the service:\n"
                           "  • brew install --cask tailscale\n"
                           "  • or download from https://tailscale.com/download\n"
                           "Then open Tailscale and sign in."), "https://tailscale.com/download"
        try:
            res = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=3)
            if res.returncode != 0:
                out = (res.stdout or "") + (res.stderr or "")
                if any(t in out for t in ("NeedsLogin", "logged out", "stopped")):
                    return False, "Tailscale is installed but not logged in. Run 'tailscale up' or sign in via the app.", None
                return False, "Tailscale is not running or logged in. Open the Tailscale app and sign in.", None
            data = json.loads(res.stdout or "{}")
            if data.get("BackendState", "") != "Running":
                return False, f"Tailscale is not ready (state: {data.get('BackendState')}). Run 'tailscale up' or sign in.", None
            return True, None, None
        except subprocess.TimeoutExpired:
            return False, "Tailscale status check timed out. Check if tailscaled is running.", None
        except Exception as exc:
            return False, f"Failed to check Tailscale status: {exc}", None

    def _verify_funnel(self):
        ts = self._resolve_tailscale()
        if not ts:
            return False, "Tailscale is not installed.", "https://tailscale.com/kb/1223/funnel"
        try:
            res = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=3)
            data = json.loads(res.stdout or "{}")
        except Exception as exc:
            return False, f"Could not read Tailscale status to check Funnel: {exc}", \
                "https://tailscale.com/kb/1223/funnel"
        node = data.get("Self", {}) or {}
        caps = set((node.get("CapMap") or {}).keys())
        dns = (node.get("DNSName") or "").rstrip(".")
        if not caps:
            # Empty/not-ready status — don't misreport Funnel as disabled.
            return False, ("Couldn't read this node's Tailscale capabilities (Tailscale may "
                           "still be starting up). Wait a moment and press Start again."), None
        has_funnel = "funnel" in caps or any("cap/funnel" in c for c in caps)
        has_https = "https" in caps
        if not dns or not has_https:
            return False, (
                "Tailscale HTTPS / MagicDNS is not enabled for this tailnet.\n\n"
                "Funnel needs HTTPS certificates and MagicDNS turned on:\n"
                "  1. Open the DNS admin page (button below).\n"
                "  2. Enable MagicDNS and HTTPS Certificates.\n"
                "  3. Then re-run Start.\n\n"
                "Docs: https://tailscale.com/kb/1223/funnel"
            ), "https://login.tailscale.com/admin/dns"
        if not has_funnel:
            return False, (
                "Tailscale Funnel is not enabled for this machine.\n\n"
                "Funnel is what exposes the handoff server to ChatGPT. To turn it on:\n"
                "  1. Open your tailnet ACL policy (button below).\n"
                "  2. Grant this node the \"funnel\" node attribute.\n"
                "  3. Save the policy, then re-run Start.\n\n"
                "Docs: https://tailscale.com/kb/1223/funnel"
            ), "https://login.tailscale.com/admin/acls/file"
        return True, None, None

    def _probe_tunnel(self, url):
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "tokenmaxxing-health", "version": "1.0"}},
            }).encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Accept": "application/json, text/event-stream"})
            with urllib.request.urlopen(req, timeout=8):
                return {"state": "green", "text": "Reachable"}
        except urllib.error.HTTPError:
            return {"state": "green", "text": "Reachable"}
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return {"state": "amber", "text": "Slow / not responding"}
            if isinstance(reason, socket.gaierror):
                return {"state": "red", "text": "DNS unresolved"}
            return {"state": "amber", "text": "Tunnel up, no response"}
        except Exception:
            return {"state": "amber", "text": "Unknown"}

    # ---- interactive terminal ---------------------------------------
    def _launch_prompt_terminal(self, agent, prompt, banner):
        """Open a Terminal running the agent CLI interactively on a custom prompt
        (used for Tailscale setup help — no plan/grant required)."""
        if sys.platform != "darwin":
            return False
        import runner
        cmd = runner.agent_interactive_command(agent, prompt)
        if not cmd:
            return False
        quoted = " ".join(shlex.quote(c) for c in cmd)
        script = "\n".join([
            "#!/bin/bash",
            f"cd {shlex.quote(str(Path.home()))} || exit 1",
            "clear",
            f'echo "🪙 Tokenmaxxing — {banner}"',
            'echo "------------------------------------------------------------"',
            "echo",
            quoted,
            "status=$?",
            "echo",
            'echo "[Tokenmaxxing] Setup session ended (exit $status). This window stays open."',
            'exec "$SHELL" -i',
            "",
        ])
        return self._osascript_terminal(script, f"setup_{agent}")

    def _osascript_terminal(self, script, name):
        try:
            exec_dir = Path(tempfile.gettempdir()) / "tokenmaxxing-exec"
            exec_dir.mkdir(parents=True, exist_ok=True)
            script_path = exec_dir / f"{name}_{int(time.time())}.sh"
            script_path.write_text(script, encoding="utf-8")
            script_path.chmod(0o755)
            subprocess.run(
                ["osascript",
                 "-e", 'tell application "Terminal"',
                 "-e", "activate",
                 "-e", f'do script "/bin/bash \\"{script_path}\\""',
                 "-e", "end tell"],
                check=True, capture_output=True, text=True, timeout=10)
            return True
        except Exception as exc:
            self._log(f"[GUI] Terminal launch failed: {exc}")
            return False

    def _launch_agent_terminal(self, agent, workspace, python_bin, runner):
        if sys.platform != "darwin":
            return False
        if getattr(sys, "frozen", False):
            exec_cmd = (f"{shlex.quote(sys.executable)} execute "
                        f"--agent {shlex.quote(agent)} --workspace {shlex.quote(workspace)} --interactive")
        else:
            exec_cmd = (f"{shlex.quote(python_bin)} {shlex.quote(runner)} execute "
                        f"--agent {shlex.quote(agent)} --workspace {shlex.quote(workspace)} --interactive")
            
        script = "\n".join([
            "#!/bin/bash",
            f"cd {shlex.quote(workspace)} || exit 1",
            "clear",
            f'echo "🪙 Tokenmaxxing — interactive {agent} session on the handoff plan"',
            'echo "Workspace: $(pwd)"',
            'echo "The agent starts on .tokenmaxxing/plan.md — keep working with it here."',
            'echo "------------------------------------------------------------"',
            "echo",
            exec_cmd,
            "status=$?",
            "echo",
            'echo "[Tokenmaxxing] Agent session ended (exit $status). This window stays open."',
            'exec "$SHELL" -i',
            "",
        ])
        return self._osascript_terminal(script, f"run_{agent}")


# ======================================================================
# Front-end (sleek black) — single embedded HTML document
# ======================================================================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:#000000; --panel:#0e0e12; --panel-2:#16161c; --raise:#1f1f27;
  --line:rgba(255,255,255,.11); --line-2:rgba(255,255,255,.20);
  --tx:#f4f4f7; --tx-dim:#c2c2cc; --tx-faint:#90909c;
  --accent:#ffffff; --green:#3dd7a0; --green-bg:#0e2a20;
  --red:#f87171; --amber:#fbbf24; --blue:#6aa8ff; --indigo:#b0bcff;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:var(--bg); color:var(--tx);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif;
  font-size:13px; -webkit-font-smoothing:antialiased; overflow:hidden;
  display:flex; flex-direction:column; height:100vh;
}
.mono{font-family:ui-monospace,"SF Mono",Menlo,monospace}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:#222227;border-radius:6px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:#33333a}

/* top bar */
.topbar{display:flex;align-items:center;justify-content:space-between;
  padding:14px 22px;border-bottom:1px solid var(--line);flex:0 0 auto}
.brand{display:flex;align-items:center;gap:11px}
.logo{width:26px;height:26px;border-radius:8px;background:var(--accent);color:#000;
  display:flex;align-items:center;justify-content:center;font-weight:600;font-size:14px}
.brand h1{font-size:15px;font-weight:500;letter-spacing:-.2px}
.brand .tag{font-size:10px;color:var(--tx-faint);letter-spacing:1.5px;text-transform:uppercase;margin-top:1px}
.tbright{display:flex;align-items:center;gap:18px}
.meta{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--tx-dim)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--tx-faint);flex:0 0 auto;transition:.25s}
.dot.green{background:var(--green);box-shadow:0 0 0 0 rgba(52,211,153,.5);animation:pulse 2s infinite}
.dot.red{background:var(--red)} .dot.amber{background:var(--amber)} .dot.off{background:var(--tx-faint)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.45)}70%{box-shadow:0 0 0 7px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}

/* layout */
.wrap{flex:1;display:flex;flex-direction:column;padding:18px 22px 16px;gap:14px;min-height:0}
.command{display:flex;align-items:center;gap:18px;background:var(--panel);
  border:1px solid var(--line);border-radius:16px;padding:16px 18px;flex:0 0 auto}
.bigbtn{border:none;cursor:pointer;font-size:12.5px;font-weight:500;letter-spacing:-.1px;
  padding:9px 15px;border-radius:10px;display:flex;align-items:center;gap:7px;
  transition:.18s;white-space:nowrap;color:#04130d;background:var(--green)}
.bigbtn:hover{filter:brightness(1.08)} .bigbtn:active{transform:scale(.985)}
.bigbtn.stop{background:transparent;color:var(--red);border:1px solid rgba(248,113,113,.4)}
.bigbtn.stop:hover{background:rgba(248,113,113,.08);filter:none}
.bigbtn.busy{background:var(--raise);color:var(--tx-dim);cursor:default}
.bigbtn .ico{font-size:13px;line-height:1}
.connector{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px}
.clabel{font-size:10px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx-faint)}
.crow{display:flex;align-items:center;gap:8px}
.curl{flex:1;min-width:0;background:var(--panel-2);border:1px solid var(--line);
  border-radius:9px;padding:9px 12px;font-size:12px;color:var(--tx-dim);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.curl.live{color:var(--tx)}
.iconbtn{background:var(--panel-2);border:1px solid var(--line);color:var(--tx-dim);
  cursor:pointer;border-radius:9px;padding:9px 11px;font-size:12px;transition:.15s;white-space:nowrap}
.iconbtn:hover{background:var(--raise);color:var(--tx);border-color:var(--line-2)}
.iconbtn:disabled{opacity:.4;cursor:default}
.health{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--tx-dim);
  padding-top:2px}

/* body */
.body{flex:1;display:grid;grid-template-columns:340px 1fr;gap:14px;min-height:0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;
  display:flex;flex-direction:column;min-height:0;overflow:hidden}
.phead{display:flex;align-items:center;justify-content:space-between;
  padding:13px 16px;border-bottom:1px solid var(--line);flex:0 0 auto}
.phead .pt{display:flex;align-items:center;gap:9px;font-size:13px;font-weight:500}
.bar{width:3px;height:14px;border-radius:2px;background:var(--blue)}
.bar.acc{background:var(--indigo)}
.phead .sub{font-size:11px;color:var(--tx-faint)}
.ghost{background:none;border:none;color:var(--tx-dim);cursor:pointer;font-size:11px;
  padding:4px 9px;border-radius:7px;transition:.15s}
.ghost:hover{background:var(--raise);color:var(--tx)}

/* plan document */
.doc{flex:1;overflow-y:auto;padding:8px 18px 22px;color:var(--tx-dim);font-size:13px;line-height:1.65}
.doc h1{font-size:17px;color:var(--tx);font-weight:500;margin:16px 0 8px;letter-spacing:-.2px}
.doc h1:first-child{margin-top:4px}
.doc h2{font-size:14.5px;color:var(--tx);font-weight:500;margin:20px 0 7px;padding-top:14px;border-top:1px solid var(--line)}
.doc h3{font-size:13px;color:var(--indigo);font-weight:500;margin:14px 0 4px}
.doc h4{font-size:12.5px;color:var(--tx);font-weight:500;margin:12px 0 4px}
.doc p{margin:6px 0}
.doc ul,.doc ol{margin:6px 0 6px 18px}
.doc li{margin:3px 0}
.doc code{background:var(--panel-2);border:1px solid var(--line);border-radius:5px;
  padding:1px 5px;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:var(--green)}
.doc pre{background:#000;border:1px solid var(--line);border-radius:9px;padding:11px 13px;
  overflow-x:auto;margin:9px 0}
.doc pre code{background:none;border:none;padding:0;color:#c4c4cc;font-size:11.5px}
.doc strong{color:var(--tx);font-weight:500}
.doc em{color:var(--tx-dim);font-style:italic}
.doc blockquote{border-left:2px solid var(--line-2);padding-left:11px;margin:8px 0;color:var(--tx-faint)}
.doc hr{border:none;border-top:1px solid var(--line);margin:14px 0}
.doc .empty{color:var(--tx-faint);padding:14px 0}

/* terminal */
.term{flex:1;overflow-y:auto;background:#000;padding:12px 14px;
  font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12px;line-height:1.6}
.ln{white-space:pre-wrap;word-break:break-word}
.ln.error{color:var(--red)} .ln.success{color:var(--green)} .ln.warn{color:var(--amber)}
.ln.info{color:var(--indigo)} .ln.plain{color:#c4c4cc} .ln.muted{color:var(--tx-faint)}

/* footer toolbar */
.footer{flex:0 0 auto;display:flex;align-items:center;gap:12px;background:var(--panel);
  border:1px solid var(--line);border-radius:14px;padding:11px 14px}
.fld{display:flex;align-items:center;gap:8px;min-width:0}
.fld.grow{flex:1;min-width:0}
.flabel{font-size:9.5px;letter-spacing:1.2px;text-transform:uppercase;color:var(--tx-faint);white-space:nowrap}
.input{flex:1;min-width:0;background:var(--panel-2);border:1px solid var(--line);color:var(--tx);
  border-radius:9px;padding:8px 11px;font-size:12px;font-family:ui-monospace,Menlo,monospace;outline:none}
.input:focus{border-color:var(--line-2)}
.sel{position:relative}
.seltrig{background:var(--panel-2);border:1px solid var(--line);color:var(--tx);cursor:pointer;
  border-radius:9px;padding:8px 12px;font-size:12px;display:flex;align-items:center;gap:8px;min-width:96px;justify-content:space-between}
.seltrig:hover{border-color:var(--line-2)}
.selmenu{position:absolute;bottom:calc(100% + 6px);left:0;right:0;background:var(--raise);
  border:1px solid var(--line-2);border-radius:10px;overflow:hidden;display:none;z-index:30}
.selmenu.open{display:block}
.selopt{padding:8px 12px;font-size:12px;cursor:pointer;color:var(--tx-dim)}
.selopt:hover{background:var(--blue);color:#04244c} .selopt.on{color:var(--tx)}
.runbtn{background:var(--accent);color:#000;border:none;cursor:pointer;font-weight:500;
  font-size:12.5px;padding:9px 16px;border-radius:9px;display:flex;align-items:center;gap:7px;transition:.15s}
.runbtn:hover{filter:brightness(.92)} .runbtn:disabled{opacity:.35;cursor:default;filter:none}
.check{display:flex;align-items:center;gap:8px;cursor:pointer;font-size:11.5px;color:var(--tx-dim);white-space:nowrap}
.check .b{width:15px;height:15px;border-radius:5px;border:1.5px solid #3a3a42;display:flex;
  align-items:center;justify-content:center;font-size:10px;transition:.15s}
.check.on .b{background:var(--green);border-color:var(--green);color:#04130d}
.check.on{color:var(--tx)}

/* modal */
.overlay{position:absolute;inset:0;background:rgba(0,0,0,.6);display:none;
  align-items:center;justify-content:center;z-index:50}
.overlay.show{display:flex}
.modal{width:520px;max-width:90%;background:var(--panel);border:1px solid var(--line-2);
  border-radius:16px;overflow:hidden}
.mhead{display:flex;align-items:center;gap:10px;padding:18px 20px 14px;font-size:15px;font-weight:500;border-bottom:1px solid var(--line)}
.mbody{padding:16px 20px;font-family:ui-monospace,Menlo,monospace;font-size:12px;
  color:var(--tx-dim);white-space:pre-wrap;line-height:1.6;max-height:280px;overflow-y:auto}
.mfoot{display:flex;justify-content:flex-end;gap:9px;padding:14px 20px;border-top:1px solid var(--line)}
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">
    <div class="logo">◆</div>
    <div><h1>tokenmaxxing</h1><div class="tag">local handoff portal</div></div>
  </div>
  <div class="tbright">
    <div class="meta"><span class="dot off" id="svcdot"></span><span id="svctext">Stopped</span></div>
    <div class="meta"><span style="color:var(--tx-faint)">grant</span><span id="timeleft" class="mono">--</span></div>
  </div>
</div>

<div class="wrap">
  <div class="command">
    <button class="bigbtn" id="toggle" onclick="onToggle()"><span class="ico">⏻</span><span id="togglelabel">Start handoff service</span></button>
    <div class="connector">
      <div class="clabel">ChatGPT connector URL</div>
      <div class="crow">
        <div class="curl mono" id="curl">—</div>
        <button class="iconbtn" id="copybtn" onclick="copyUrl()" disabled>Copy</button>
      </div>
      <div class="health"><span class="dot off" id="tundot"></span><span id="tuntext">Idle</span></div>
    </div>
  </div>

  <div class="body">
    <div class="panel">
      <div class="phead">
        <div class="pt"><span class="bar"></span>Handoff plan</div>
        <button class="ghost" onclick="loadPlan()">Reload</button>
      </div>
      <div class="doc" id="plan"></div>
    </div>

    <div class="panel">
      <div class="phead">
        <div class="pt"><span class="bar acc"></span>Console <span style="color:var(--tx-faint);font-weight:400;font-size:11px;margin-left:4px">service &amp; setup log</span></div>
        <button class="ghost" onclick="clearLog()">Clear</button>
      </div>
      <div class="term" id="term"></div>
    </div>
  </div>

  <div class="footer">
    <div class="fld grow">
      <span class="flabel">Workspace</span>
      <input class="input" id="ws" spellcheck="false" onchange="onWsChange()">
      <button class="iconbtn" onclick="browse()">Browse</button>
    </div>
    <div class="fld">
      <span class="flabel">Agent</span>
      <div class="sel">
        <div class="seltrig" id="agenttrig" onclick="toggleSel('agent')"><span id="agentval">codex</span><span style="color:var(--tx-faint)">▾</span></div>
        <div class="selmenu" id="agentmenu"></div>
      </div>
    </div>
    <div class="fld">
      <span class="flabel">TTL</span>
      <div class="sel">
        <div class="seltrig" id="ttltrig" style="min-width:64px" onclick="toggleSel('ttl')"><span id="ttlval">4h</span><span style="color:var(--tx-faint)">▾</span></div>
        <div class="selmenu" id="ttlmenu"></div>
      </div>
    </div>
    <div class="check on" id="termchk" onclick="toggleTerm()"><span class="b">✓</span>New Terminal</div>
    <button class="runbtn" id="runbtn" onclick="runAgent()" disabled><span style="font-size:13px">⚡</span>Run</button>
  </div>
</div>

<div class="overlay" id="overlay">
  <div class="modal">
    <div class="mhead">🔒 <span id="mtitle">Funnel setup required</span></div>
    <div class="mbody" id="mbody"></div>
    <div class="mfoot" id="mfoot"></div>
  </div>
</div>

<script>
let S = {running:false, url:"", agents:[], ttls:[], agent:"codex", ttl:"4h", new_terminal:true, transitioning:false};

window.appendLog = function(text, level){
  const t = document.getElementById('term');
  const d = document.createElement('div');
  d.className = 'ln ' + (level||'plain');
  d.textContent = text;
  t.appendChild(d);
  t.scrollTop = t.scrollHeight;
};
function clearLog(){ document.getElementById('term').innerHTML=''; }

function setDot(id, state){ document.getElementById(id).className = 'dot ' + state; }

function applyStatus(st){
  S.running = st.running; S.url = st.url||""; S.transitioning = st.transitioning;
  const tgl = document.getElementById('toggle'), lbl = document.getElementById('togglelabel');
  const dot = 'svcdot', txt = document.getElementById('svctext');
  if(st.transitioning){
    tgl.className='bigbtn busy'; lbl.textContent='Working…'; setDot(dot,'amber'); txt.textContent='Working…';
  } else if(st.running){
    tgl.className='bigbtn stop'; lbl.textContent='Stop handoff service'; setDot(dot,'green'); txt.textContent='Running';
  } else {
    tgl.className='bigbtn'; lbl.textContent='Start handoff service'; setDot(dot,'off'); txt.textContent='Stopped';
  }
  document.getElementById('timeleft').textContent = st.time_left || '--';
  const curl = document.getElementById('curl'), cbtn = document.getElementById('copybtn');
  if(st.url){ curl.textContent = st.url; curl.classList.add('live'); cbtn.disabled=false; }
  else { curl.textContent='—'; curl.classList.remove('live'); cbtn.disabled=true; }
  document.getElementById('runbtn').disabled = !st.running || st.transitioning;
  if(!st.running){ setDot('tundot','off'); document.getElementById('tuntext').textContent='Idle'; }
}
window.refreshStatus = async function(){ applyStatus(await pywebview.api.get_status()); };

async function poll(){ try{ applyStatus(await pywebview.api.get_status()); }catch(e){} }
async function health(){
  if(!S.running) return;
  try{ const h = await pywebview.api.get_tunnel_health();
    setDot('tundot', h.state); document.getElementById('tuntext').textContent = h.text; }catch(e){}
}

async function onToggle(){
  if(S.transitioning) return;
  if(S.running){ await pywebview.api.stop_service(); applyStatus({running:true,transitioning:true}); }
  else{
    applyStatus({running:false,transitioning:true});
    const r = await pywebview.api.start_service();
    if(!r.ok){
      applyStatus(await pywebview.api.get_status());
      if(r.setup){ showSetup(r.setup); }
      else if(r.error){ showModal('Cannot start', r.error, null); }
    }
  }
}

function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function inlineMd(t){
  return esc(t)
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g,'$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" onclick="event.preventDefault();pywebview.api.open_url(\'$2\')">$1</a>');
}
function renderMarkdown(md){
  const lines=md.split('\n'); let html=''; let inCode=false; let buf=[]; let list=null;
  const closeList=()=>{ if(list){ html+='</'+list+'>'; list=null; } };
  for(const raw of lines){
    if(raw.trim().startsWith('```')){
      if(inCode){ html+='<pre><code>'+esc(buf.join('\n'))+'</code></pre>'; buf=[]; inCode=false; }
      else { closeList(); inCode=true; }
      continue;
    }
    if(inCode){ buf.push(raw); continue; }
    const line=raw.replace(/\s+$/,''); let m;
    if((m=line.match(/^(#{1,6})\s+(.*)$/))){ closeList(); const l=Math.min(m[1].length,4); html+='<h'+l+'>'+inlineMd(m[2])+'</h'+l+'>'; continue; }
    if(/^\s*[-*]\s+\[[ xX]\]\s+/.test(line)){ if(list!=='ul'){closeList();html+='<ul>';list='ul';} html+='<li>'+inlineMd(line.replace(/^\s*[-*]\s+\[[ xX]\]\s+/,''))+'</li>'; continue; }
    if((m=line.match(/^\s*[-*]\s+(.*)$/))){ if(list!=='ul'){closeList();html+='<ul>';list='ul';} html+='<li>'+inlineMd(m[1])+'</li>'; continue; }
    if((m=line.match(/^\s*\d+\.\s+(.*)$/))){ if(list!=='ol'){closeList();html+='<ol>';list='ol';} html+='<li>'+inlineMd(m[1])+'</li>'; continue; }
    if((m=line.match(/^\s*>\s?(.*)$/))){ closeList(); html+='<blockquote>'+inlineMd(m[1])+'</blockquote>'; continue; }
    if(/^([-=*])\1{2,}$/.test(line.trim())){ closeList(); html+='<hr>'; continue; }
    if(line.trim()===''){ closeList(); continue; }
    closeList(); html+='<p>'+inlineMd(line)+'</p>';
  }
  closeList(); if(inCode){ html+='<pre><code>'+esc(buf.join('\n'))+'</code></pre>'; }
  return html;
}
async function loadPlan(){
  const p = await pywebview.api.get_plan_md();
  const host = document.getElementById('plan');
  if(p.missing){ host.innerHTML='<div class="empty">No plan.md in this workspace.</div>'; return; }
  if(p.error){ host.innerHTML='<div class="empty" style="color:var(--red)">Could not read plan: '+esc(p.error)+'</div>'; return; }
  host.innerHTML = renderMarkdown(p.text||'');
  host.scrollTop = 0;
}

async function runAgent(){
  if(document.getElementById('runbtn').disabled) return;
  const r = await pywebview.api.run_agent();
  if(r && r.no_plan){
    showModal('No handoff plan', 'No plan.md found at:\n'+r.plan_path+'\n\nRun the executor anyway?', null, true);
  }
}

function onWsChange(){ pywebview.api.set_workspace(document.getElementById('ws').value).then(loadPlan); }
async function browse(){ const r = await pywebview.api.browse_workspace();
  if(r.workspace){ document.getElementById('ws').value=r.workspace; loadPlan(); } }
function copyUrl(){ if(S.url){ navigator.clipboard.writeText(S.url); const b=document.getElementById('copybtn'); b.textContent='Copied'; setTimeout(()=>b.textContent='Copy',1200);} }
function toggleTerm(){ S.new_terminal=!S.new_terminal; document.getElementById('termchk').classList.toggle('on',S.new_terminal);
  document.getElementById('termchk').querySelector('.b').textContent=S.new_terminal?'✓':''; pywebview.api.set_new_terminal(S.new_terminal); }

function buildSel(kind, values, current){
  const menu=document.getElementById(kind+'menu'); menu.innerHTML='';
  values.forEach(v=>{ const o=document.createElement('div'); o.className='selopt'+(v===current?' on':''); o.textContent=v;
    o.onclick=()=>{ document.getElementById(kind+'val').textContent=v; menu.classList.remove('open');
      menu.querySelectorAll('.selopt').forEach(x=>x.classList.remove('on')); o.classList.add('on');
      if(kind==='agent') pywebview.api.set_agent(v); else pywebview.api.set_ttl(v); };
    menu.appendChild(o); });
}
function toggleSel(kind){ const m=document.getElementById(kind+'menu'); const open=m.classList.contains('open');
  document.querySelectorAll('.selmenu').forEach(x=>x.classList.remove('open')); if(!open) m.classList.add('open'); }
document.addEventListener('click',e=>{ if(!e.target.closest('.sel')) document.querySelectorAll('.selmenu').forEach(x=>x.classList.remove('open')); });

function showModal(title,msg,url,confirm){
  document.getElementById('mtitle').textContent=title;
  document.getElementById('mbody').textContent=msg;
  const foot=document.getElementById('mfoot'); foot.innerHTML='';
  if(confirm){
    const yes=mkbtn('Run anyway',true); yes.onclick=()=>{hideModal(); pywebview.api.run_agent_confirmed();};
    const no=mkbtn('Cancel',false); no.onclick=hideModal; foot.append(no,yes);
  } else {
    if(url){ const open=mkbtn('Open setup page',true); open.onclick=()=>pywebview.api.open_url(url);
      const copy=mkbtn('Copy URL',false); copy.onclick=()=>navigator.clipboard.writeText(url); foot.append(copy,open); }
    const close=mkbtn('Close',false); close.onclick=hideModal; foot.append(close);
  }
  document.getElementById('overlay').classList.add('show');
}
function showSetup(s){
  document.getElementById('mtitle').textContent = s.kind==='funnel' ? 'Funnel setup required' : 'Tailscale setup required';
  document.getElementById('mbody').textContent = s.message;
  const foot=document.getElementById('mfoot'); foot.innerHTML='';
  const fix=mkbtn('Set up with '+S.agent+'  ↗', true);
  fix.onclick=()=>{ hideModal(); pywebview.api.fix_setup(s.message); };
  foot.append(fix);
  if(s.url){
    const open=mkbtn('Open admin page', false); open.onclick=()=>pywebview.api.open_url(s.url);
    const copy=mkbtn('Copy link', false); copy.onclick=()=>navigator.clipboard.writeText(s.url);
    foot.append(copy, open);
  }
  const close=mkbtn('Close', false); close.onclick=hideModal; foot.append(close);
  document.getElementById('overlay').classList.add('show');
}
function hideModal(){ document.getElementById('overlay').classList.remove('show'); }
function mkbtn(label,primary){ const b=document.createElement('button');
  b.className=primary?'runbtn':'iconbtn'; b.textContent=label; return b; }

window.addEventListener('pywebviewready', async ()=>{
  const s = await pywebview.api.init();
  S = Object.assign(S, s);
  document.getElementById('ws').value = s.workspace || '';
  document.getElementById('agentval').textContent = s.agent;
  document.getElementById('ttlval').textContent = s.ttl;
  buildSel('agent', s.agents, s.agent);
  buildSel('ttl', s.ttls, s.ttl);
  applyStatus(s);
  await loadPlan();
  poll(); health();
  setInterval(poll, 3000);
  setInterval(health, 15000);
});
</script>
</body>
</html>
"""


def main():
    api = Api()
    window = webview.create_window(
        "Tokenmaxxing Control Center",
        html=INDEX_HTML,
        js_api=api,
        width=1080,
        height=760,
        min_size=(900, 640),
        background_color="#000000",
    )
    api.bind(window)
    webview.start()


if __name__ == "__main__":
    subcommands = {"gui", "daemon", "grant", "status", "revoke", "install-launchagent", "uninstall-launchagent", "execute"}
    if len(sys.argv) > 1 and sys.argv[1] in subcommands:
        import runner
        runner.main()
    else:
        main()

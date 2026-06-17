# Tokenmaxxing Handoff: Next Session Workflow & UI Improvements

*Last updated: 2026-06-17. Status: **All agents (Antigravity, Claude Code, Codex) working and streaming logs in real-time.***

This handoff outlines what was accomplished in the current session, how the `codex` and `antigravity` agent executions were fixed/verified, and the proposed backlog of **UI and workflow improvements** for the next session.

---

## 🚀 1. Session Accomplishments & Bug Fixes

1. **Real-time Log Streaming to GUI Console:**
   * *Problem:* Previously, `runner.py` executed agent tools using `subprocess.run(capture_output=True)`, which buffered all outputs. The GUI's log window remained blank and frozen until the agent finished completely.
   * *Solution:* Refactored `execute` in [runner.py](file:///Users/jerry/Projects/tokenmaxxing/runner.py#L250-L290) to spawn agent processes using `subprocess.Popen` and read stdout/stderr asynchronously on a background thread. Outputs now print to `sys.stdout` in real-time, instantly displaying inside the GUI console.
2. **Fixed Codex Execution & Stdin Hangs:**
   * *Problem:* Codex was failing when run outside of a git repository with the error: `Not inside a trusted directory and --skip-git-repo-check was not specified.` It was also hanging because it tried to read additional inputs from stdin when run inside a subprocess.
   * *Solution:*
     * Added `--skip-git-repo-check` and `--sandbox danger-full-access` directly to the `codex` command array in `runner.py`.
     * Redirected stdin to `subprocess.DEVNULL` to ensure Codex never blocks waiting for input.
3. **Stable Tailscale Tunnel Integration:**
   * The server runs on Streamable HTTP (which ChatGPT Web requires) and is exposed via a stable Tailscale Funnel.
4. **Single-Button UI Toggle:**
   * The start/stop controls are unified into a single button in [gui.py](file:///Users/jerry/Projects/tokenmaxxing/gui.py) that changes states dynamically.
5. **Folder Path Persistence:**
   * Selected folders are saved to `~/.tokenmaxxing/state.json` under `"last_workspace"` and auto-loaded on fresh launches.

---

## ⚡ 2. Current CLI Agent Configurations

Local agent commands are mapped inside `runner.py` as:

| Agent | Command Executed | Non-interactive Sandbox |
| :--- | :--- | :--- |
| `antigravity` | `agy -p "<prompt>"` | Standard Antigravity execution |
| `claude` | `claude -p "<prompt>"` | Claude Code execution |
| `codex` | `codex exec --skip-git-repo-check --sandbox danger-full-access "<prompt>"` | Codex execution with full workspace permissions |

---

## 🛠️ 3. Backlog: Wishlist for Claude (UI & Workflow Improvements)

In the next session, implement the following improvements:

### A. Real-Time Log Enhancements (Tkinter UI)
* **Color Coding:** Currently, the log window displays raw, plain white text. Parse log lines and apply color highlights:
  * Red (`#EF4444`) for lines containing `[Error]`, `Exception`, `Failed`, or stack traces.
  * Green (`#10B981`) for lines containing `[Success]`, `Exit code: 0`, or successful test completions.
  * Yellow (`#F59E0B`) for warnings or timeouts.
* **Auto-Scroll Toggle:** Add a checkbox inside the GUI console card to toggle auto-scroll to the bottom of the logs.

### B. Interactive Handoff Checklist
* **Plan Visualization:** In the GUI, read `.tokenmaxxing/plan.md` if it exists.
* Parse the markdown list of tasks (e.g., lines containing `- [ ]` or `1.`) and render them as checkable checkboxes in a sidebar or secondary panel inside the GUI.
* This allows the developer to visually track what stage the planning agent proposed and mark them off as they verify the local agent's build results.

### C. Live Tunnel Health Check Indicator
* Add a tiny blinking LED-style icon next to the Connector URL:
  * **Blinking Green:** Endpoint is publicly reachable (run a background curl to the connector url every 15s to check `/mcp` status).
  * **Amber:** Tunnel open but endpoint not responding.
  * **Red:** Tunnel disconnected.

### D. Always-On LaunchAgent Streamable HTTP Support
* The `install-launchagent` command in `runner.py` currently registers a plist pointing to standard SSE transport. Update the LaunchAgent helper to pass `--transport streamable-http` so it supports ChatGPT Web action connectors persistently.

---

## 📈 4. Verification Check for the Next Agent
To verify that everything is configured correctly:
1. Start the handoff service:
   ```bash
   tokenmaxxing gui
   ```
2. Press **`🚀 Start Handoff Service`** in the GUI, copy the connector URL, and test it in your browser or with curl:
   ```bash
   curl -X POST <your-connector-url> -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
   ```
3. Generate a plan at `.tokenmaxxing/plan.md`.
4. Run agent execution via the GUI and watch the logs stream in real-time.

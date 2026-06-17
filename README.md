# 🪙 Tokenmaxxing

> A sleek, black macOS desktop + CLI portal for agentic handoffs: **plan on ChatGPT Web → execute with your local CLI agent.**

Tokenmaxxing runs a local MCP server, exposes it over a stable **Tailscale Funnel** URL so ChatGPT Web can read your workspace and write an implementation plan, then hands that plan to a local CLI agent (Antigravity / Claude Code / Codex) for execution.

---

## 💡 Why this exists

AI-assisted coding is most effective when you separate **design (planning)** from **construction (execution)**. The name *Tokenmaxxing* is about maximizing your AI budget:

1. **Ideation is token-heavy.** Brainstorming and architecture take long, open-ended conversations.
2. **Web and API limits are separate.** When ChatGPT Web reads files and writes `.tokenmaxxing/plan.md` through the connector, it runs under your *ChatGPT* usage limits — **zero developer-API credits**.
3. **Execution is cheap and local.** You only spend developer-API tokens on the final pass, when the local agent reads the pre-compiled plan and implements it.
4. **Security stays local.** The web planner gets a time-limited, read-only workspace grant; code modification and shell execution happen locally, under your control.
5. **Stable URL.** Tailscale Funnel gives a connector URL that stays constant across restarts — paste it into ChatGPT once.

```
ChatGPT Web  ──(Tailscale Funnel)──>  Local MCP server  ──writes──>  .tokenmaxxing/plan.md
 (planner: read-only workspace)                                              │
                                                                             ▼
                                              Local CLI agent (Antigravity / Claude / Codex)
                                              reads the plan, edits files, runs tests
```

---

## ✨ Features

- **Sleek black desktop GUI** — a native window (rendered with `pywebview`/WebKit): start/stop the service, watch the grant timer, read the live plan, and launch agents.
- **Stable connector URL** via Tailscale Funnel, with a **pre-flight check** that verifies Tailscale is installed, logged in, and that **Funnel** is actually enabled before starting — and surfaces the exact admin-console link if not.
- **Agent-assisted setup** — if Tailscale/Funnel isn't configured, one click launches your selected CLI agent in a Terminal, seeded with the detected problem and the steps to fix it.
- **Interactive execution** — "Run" opens a real Terminal and starts your chosen agent *interactively* on the plan, so you can keep working with it (not a one-shot capture).
- **Plan reader** — renders `.tokenmaxxing/plan.md` as a formatted document inside the app.
- **TTL-based grants** — workspaces are exposed for a limited time (e.g. 4h) and auto-lock when the grant expires.
- **Tunnel health indicator** and a **service/setup console** for live orchestrator logs.

---

## 📦 Installation

Requires Python 3.10+ and [Tailscale](https://tailscale.com/download) (installed, logged in, with **MagicDNS + HTTPS** and **Funnel** enabled for the node).

```bash
git clone https://github.com/<you>/tokenmaxxing.git
cd tokenmaxxing
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Launch the GUI:

```bash
tokenmaxxing gui          # or: python runner.py gui
```

> **Packaged `.app`:** The PyInstaller spec (`Tokenmaxxing.spec`) predates the pywebview rewrite and needs `pywebview` hidden imports before it will bundle correctly — building the standalone app is a known TODO.

---

## 🛠️ Workflow

### 1. Start the service
Open the app, pick your workspace, click **Start handoff service**. Tokenmaxxing runs the Tailscale + Funnel pre-flight, starts the local MCP server, and shows a stable connector URL with a countdown timer.

### 2. Connect ChatGPT Web
Copy the connector URL (e.g. `https://<your-machine>.<tailnet>.ts.net/mcp`) and add it as a custom action/connector in ChatGPT Web.

### 3. Generate the plan (web)
Ask ChatGPT to inspect the workspace and write the plan:
> *"Read the workspace and write an implementation plan to `.tokenmaxxing/plan.md` to implement [feature]."*

The plan appears in the app's **Handoff plan** panel.

### 4. Execute the plan (local)
Pick an agent (`antigravity` / `claude` / `codex`) and click **Run**. A Terminal opens with the agent running interactively on the plan — read, intervene, and keep working with it directly.

---

## 🔒 Security guardrails

- **Scoped exposure** — only the granted workspace directory is reachable.
- **Auto-lock TTL** — grants lock automatically when the TTL expires.
- **Sensitive-file blocklist** — `.env`, `.ssh`, `.aws`, `*.key`, `*.pem`, `.git/config` are blocked.
- **Ignored folders** — heavy/generated dirs (`node_modules`, `.venv`, `dist`, `build`) are skipped.

---

## 🧩 CLI

```bash
tokenmaxxing gui                         # launch the desktop app
tokenmaxxing grant <path> --ttl 4h       # grant a workspace for a window of time
tokenmaxxing status                      # show the active grant
tokenmaxxing revoke                      # clear the grant
tokenmaxxing daemon --transport streamable-http   # run the MCP daemon
tokenmaxxing execute --agent codex --interactive  # run an agent on the plan
tokenmaxxing install-launchagent         # always-on daemon at login (macOS)
```

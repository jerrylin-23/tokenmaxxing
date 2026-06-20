# 🪙 Tokenmaxxing

> A sleek, black macOS desktop + CLI portal for agentic handoffs: **plan on ChatGPT Web → execute with your local CLI agent.**



https://github.com/user-attachments/assets/9f6e577f-3a8a-4a08-965c-6b441b87a1b9



Tokenmaxxing runs a local MCP server, exposes it over a stable **Tailscale Funnel** URL so ChatGPT Web can read your workspace and write an implementation plan, then hands that plan to a local CLI agent (Antigravity / Claude Code / Codex) for execution.

---

## 💡 The "Rate-Limit Arbitrage" (Why this exists)

Tokenmaxxing leverages a powerful rate-limit arbitrage: **ChatGPT Web usage limits are completely separate from developer API limits (like those used by Codex and local CLI agents).**

Since most developers don't code directly inside a web browser, we split the workflow:
1. **Design & Planning on the Web (Free/Flat-Rate):** Brainstorming, analyzing codebase context, and architecting solutions are token-heavy operations. By exposing your workspace via Tailscale Funnel to ChatGPT Web, the web planner can read your workspace files and write a comprehensive plan to `.tokenmaxxing/plan.md` using your flat-rate web subscription, **consuming zero developer-API credits**.
2. **Local Execution (Cheap & Fast):** Once the heavy design lift is complete, you use your favorite local CLI agents (such as Antigravity, Claude Code, or Codex) to execute the plan. The local agents only need to process the pre-compiled plan and implement the code, keeping developer API costs extremely low.
3. **Local Control & Security:** The web planner only gets time-limited, read-only access to write the plan. File writes, command execution, and code modifications happen locally on your machine, under your supervision.

---

```
ChatGPT Web
  planner with read-only workspace access
  |
  | Tailscale Funnel
  v
Local MCP server
  writes .tokenmaxxing/plan.md
  |
  v
Local CLI agent
  Antigravity / Claude / Codex
  reads the plan, edits files, runs tests
```

---

## ✨ Features

- **Sleek black desktop GUI:** a native window (rendered with `pywebview`/WebKit) to start/stop the service, watch the grant timer, read the live plan, and launch agents.
- **Stable connector URL:** Tailscale Funnel support with a **pre-flight check** that verifies Tailscale is installed, logged in, and that **Funnel** is actually enabled before starting, then surfaces the exact admin-console link if not.
- **Agent-assisted setup:** if Tailscale/Funnel isn't configured, one click launches your selected CLI agent in a Terminal, seeded with the detected problem and the steps to fix it.
- **Interactive execution:** "Run" opens a real Terminal and starts your chosen agent *interactively* on the plan, so you can keep working with it (not a one-shot capture).
- **Plan reader:** renders `.tokenmaxxing/plan.md` as a formatted document inside the app.
- **TTL-based grants:** workspaces are exposed for a limited time (e.g. 4h) and auto-lock when the grant expires.
- **Tunnel health indicator** and a **service/setup console** for live orchestrator logs.

---

## 📦 Installation

Requires Python 3.10+.

```bash
git clone https://github.com/jerrylin-23/tokenmaxxing.git
cd tokenmaxxing
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Launch the GUI:

```bash
tokenmaxxing gui          # or: python runner.py gui
```

> **Packaged `.app`:** To run the standalone packaged application, you can build the executable and bundle it using `pyinstaller Tokenmaxxing.spec` followed by `./make_dmg.sh` to package it into a ready-to-use macOS DMG file (`dist/Tokenmaxxing.dmg`).

---

## ⛑️ Tailscale & Funnel Configuration Guide

Tokenmaxxing uses **Tailscale Funnel** to securely expose the local MCP daemon to the public internet so that ChatGPT Web can communicate with it. Follow these steps to configure your machine:

### 1. Install & Authenticate Tailscale
If you don't have Tailscale installed, install the CLI version and log in:
```bash
# Install Tailscale on macOS:
brew install --cask tailscale

# Start Tailscale and log in:
tailscale up
```

### 2. Enable MagicDNS and HTTPS Certificates
ChatGPT Web requires custom MCP connectors to use secure `https://` URLs. 
1. Open your [Tailscale DNS Admin Console](https://login.tailscale.com/admin/dns).
2. Ensure **MagicDNS** is enabled.
3. Scroll down to **HTTPS Certificates** and click **Enable**.

### 3. Grant Funnel Node Attributes in ACL Policy
By default, Tailscale nodes cannot expose public Funnels. You must grant permission in the Tailscale Access Control Policy:
1. Open your [Tailscale ACL Policy Editor](https://login.tailscale.com/admin/acls/file).
2. Add the `"funnel"` attribute node capability to your policy. For example:
   ```json
   "nodeAttrs": [
       {
           "target": ["*"],
           "attr": ["funnel"],
       }
   ]
   ```
   *(For details, see the [Tailscale Funnel Documentation](https://tailscale.com/kb/1223/funnel)).*

### 4. Verify Setup
You can verify that Funnel is enabled on your machine by running:
```bash
tailscale funnel status
```
If you start the service via the Tokenmaxxing GUI, the application will automatically perform these pre-flight checks and notify you if anything is missing.

---

## 🛠️ Workflow

### 1. Start the service
Open the app, pick your workspace, click **Start handoff service**. Tokenmaxxing runs the Tailscale + Funnel pre-flight, starts the local MCP server, and shows a stable connector URL with a countdown timer.

### 2. Connect ChatGPT Web (Developer Mode)

To connect ChatGPT Web to your local Tokenmaxxing daemon, you must enable **Developer Mode** in your ChatGPT settings and register the connector:

#### Step A: Enable Developer Mode in ChatGPT
1. Go to [chatgpt.com](https://chatgpt.com) and log in.
2. Click on your profile settings (gear icon or menu in the bottom-left or top-right corner) and select **Settings**.
3. Navigate to the **Apps** (or **Connectors**) section.
4. Locate the **Developer mode** toggle and turn it **ON** (Enabled).

#### Step B: Register the Tokenmaxxing Connector
1. In the same **Apps** settings window, click **Add App** or **Create app**.
2. Configure the application with the following details:
   - **Name:** `Tokenmaxxing` (or a name of your choice).
   - **Server URL:** Paste the Funnel URL shown in the Tokenmaxxing GUI (e.g., `https://<your-machine>.<tailnet>.ts.net/mcp`). *Make sure the URL ends with `/mcp`.*
3. Select **No Authentication** (or trust the provider if prompted).
4. Click **Create** / **Save**.

#### Step C: Enable the Connector in your Chat
1. Start a new conversation on ChatGPT.
2. Click the **App/Connector** icon in the text input bar (or use the `@` menu / tool selector depending on the current ChatGPT UI layout) and verify that the **Tokenmaxxing** connector is checked/enabled.


### 3. Generate the plan (web)
Ask ChatGPT to inspect the workspace and write the plan:
> *"First call `get_project_context`, then read the source files relevant to [feature]. Write an implementation plan to `.tokenmaxxing/plan.md`."*

The plan appears in the app's **Handoff plan** panel.

`get_project_context` gives the web chat a compact view of the current working
tree, safe file map, and the root instructions/manifests before it drills into
the relevant source files. This prevents planning from a blank slate. The
connector is deliberately a sealed workspace view: do not ask web ChatGPT to
read arbitrary machine paths, home directories, or sibling repositories.

### 4. Execute the plan (local)
Pick an agent (`antigravity` / `claude` / `codex`) and click **Run**. A Terminal opens with the agent running interactively on the plan, so you can read, intervene, and keep working with it directly.

---

## 🔒 Security guardrails

- **Scoped exposure:** only the granted workspace directory is reachable.
- **Auto-lock TTL:** grants lock automatically when the TTL expires.
- **Sensitive-file blocklist:** `.env`, `.ssh`, `.aws`, `*.key`, `*.pem`, `.git/config` are blocked.
- **Ignored folders:** heavy/generated dirs (`node_modules`, `.venv`, `dist`, `build`) are skipped.

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

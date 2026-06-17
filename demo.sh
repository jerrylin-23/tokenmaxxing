#!/usr/bin/env bash
#
# tokenmaxxing demo orchestrator
#
# Brings up the full ChatGPT-connector demo with one command:
#   1. starts the local MCP daemon (locked until you grant a workspace)
#   2. starts exactly one public tunnel (Cloudflare quick tunnel by default)
#   3. grants the demo workspace for a limited TTL
#   4. verifies the /sse MCP handshake works *through the public URL*
#   5. prints the connector URL to paste into ChatGPT
#
# Usage:
#   ./demo.sh start      # bring everything up (default)
#   ./demo.sh stop       # tear everything down and revoke the grant
#   ./demo.sh status     # show daemon / tunnel / grant state
#   ./demo.sh url        # print just the connector URL
#
# Config (env vars):
#   TM_WORKSPACE   workspace to grant         (default: ~/career-ops)
#   TM_PORT        local daemon port          (default: 8000)
#   TM_TTL         grant TTL                  (default: 4h)
#   TM_ALLOW_EXEC  set to 1 to allow web-triggered execute_handover (default: 0)
#   TM_TUNNEL      cloudflared | localtunnel  (default: cloudflared)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="${TMPDIR:-/tmp}/tokenmaxxing-demo"
DAEMON_PID_FILE="$RUN_DIR/daemon.pid"
TUNNEL_PID_FILE="$RUN_DIR/tunnel.pid"
DAEMON_LOG="$RUN_DIR/daemon.log"
TUNNEL_LOG="$RUN_DIR/tunnel.log"
URL_FILE="$RUN_DIR/connector_url"

TM_WORKSPACE="${TM_WORKSPACE:-$HOME/career-ops}"
TM_PORT="${TM_PORT:-8000}"
TM_TTL="${TM_TTL:-4h}"
TM_ALLOW_EXEC="${TM_ALLOW_EXEC:-0}"
TM_TUNNEL="${TM_TUNNEL:-tailscale}"
# ChatGPT's custom-connector / Apps SDK speaks MCP over Streamable HTTP (served
# at /mcp). Use that by default; set TM_TRANSPORT=sse for legacy SSE clients.
TM_TRANSPORT="${TM_TRANSPORT:-streamable-http}"
LAUNCHAGENT_LABEL="${LAUNCHAGENT_LABEL:-io.jerrylin.tokenmaxxing}"
case "$TM_TRANSPORT" in
  streamable-http) TM_PATH="/mcp" ;;
  sse)             TM_PATH="/sse" ;;
  *) echo "[demo] TM_TRANSPORT must be 'streamable-http' or 'sse'" >&2; exit 1 ;;
esac

# All human-facing messages go to stderr so that command substitution
# (e.g. url="$(start_tunnel_cloudflared)") only ever captures real data on stdout.
log()  { printf '\033[1;36m[demo]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[demo]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[demo]\033[0m %s\n' "$*" >&2; exit 1; }

activate_venv() {
  # shellcheck disable=SC1091
  if [ -f "$REPO_DIR/.venv/bin/activate" ]; then
    source "$REPO_DIR/.venv/bin/activate"
  else
    die "venv not found at $REPO_DIR/.venv — run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
  fi
}

pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

read_pid() { [ -f "$1" ] && cat "$1" 2>/dev/null || true; }

kill_pid_file() {
  local pf="$1" pid
  pid="$(read_pid "$pf")"
  if pid_alive "$pid"; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    pid_alive "$pid" && kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pf"
}

daemon_healthy() {
  # Judge liveness by HTTP status code, not curl's exit code (/sse is an open
  # stream that never returns). Probe the transport's own path so we only treat
  # a daemon serving OUR transport as healthy: a mismatched daemon answers our
  # path with 404, nothing listening gives 000.
  local code
  code="$(curl -sS -o /dev/null --max-time 3 -w '%{http_code}' "http://127.0.0.1:$TM_PORT$TM_PATH" 2>/dev/null || true)"
  [ -n "$code" ] && [ "$code" != "000" ] && [ "$code" != "404" ]
}

wait_for_local_daemon() {
  local i
  for i in $(seq 1 30); do
    daemon_healthy && return 0
    pid_alive "$(read_pid "$DAEMON_PID_FILE")" || return 1
    sleep 0.5
  done
  return 1
}

unload_conflicting_launchagent() {
  # The always-on LaunchAgent (if installed) runs a fixed transport on $TM_PORT
  # and respawns on KeepAlive, so it would either hold the port or serve the
  # wrong transport. Unload it for the duration of the demo.
  local plist="$HOME/Library/LaunchAgents/$LAUNCHAGENT_LABEL.plist"
  if launchctl list 2>/dev/null | grep -q "$LAUNCHAGENT_LABEL"; then
    warn "Evicting always-on LaunchAgent ($LAUNCHAGENT_LABEL) so the demo can serve $TM_TRANSPORT on port $TM_PORT."
    # `bootout` is the modern, reliable evict; `unload` is the legacy fallback.
    launchctl bootout "gui/$(id -u)/$LAUNCHAGENT_LABEL" 2>/dev/null \
      || launchctl unload "$plist" 2>/dev/null || true
    sleep 1
  fi
}

start_daemon() {
  unload_conflicting_launchagent
  # Reuse only a daemon already serving our transport at $TM_PATH; otherwise start one.
  if daemon_healthy; then
    log "Reusing daemon already healthy on 127.0.0.1:$TM_PORT (not starting a new one)."
    rm -f "$DAEMON_PID_FILE"   # we don't own it, so stop must not kill it
    return 0
  fi
  log "Starting MCP daemon ($TM_TRANSPORT) on 127.0.0.1:$TM_PORT ..."
  ( python "$REPO_DIR/server.py" --transport "$TM_TRANSPORT" --host 127.0.0.1 --port "$TM_PORT" \
      >"$DAEMON_LOG" 2>&1 & echo $! >"$DAEMON_PID_FILE" )
  if wait_for_local_daemon; then
    log "Daemon healthy (local /sse responding)."
  else
    warn "Daemon did not become healthy. Last log lines:"
    tail -n 20 "$DAEMON_LOG" >&2 || true
    die "daemon startup failed (is another non-tokenmaxxing process on port $TM_PORT?)"
  fi
}

start_tunnel_cloudflared() {
  command -v cloudflared >/dev/null 2>&1 || die "cloudflared not installed (brew install cloudflared)"
  # Default to HTTP/2 (TCP 443). cloudflared's default QUIC transport needs UDP
  # egress on port 7844, which many networks block/drop — that shows up as
  # "Failed to dial a quic connection" and an unresolvable tunnel hostname.
  local proto="${TM_CF_PROTOCOL:-http2}"
  log "Starting Cloudflare quick tunnel (protocol: $proto) ..."
  ( cloudflared tunnel --no-autoupdate --protocol "$proto" --url "http://127.0.0.1:$TM_PORT" \
      >"$TUNNEL_LOG" 2>&1 & echo $! >"$TUNNEL_PID_FILE" )
  local i url=""
  for i in $(seq 1 40); do
    url="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)"
    [ -n "$url" ] && break
    pid_alive "$(read_pid "$TUNNEL_PID_FILE")" || { tail -n 20 "$TUNNEL_LOG" >&2; die "cloudflared exited early"; }
    sleep 0.5
  done
  [ -n "$url" ] || { tail -n 20 "$TUNNEL_LOG" >&2; die "could not parse tunnel URL from cloudflared log"; }
  echo "$url"
}

start_tunnel_localtunnel() {
  command -v lt >/dev/null 2>&1 || die "localtunnel not installed (npm install -g localtunnel)"
  log "Starting localtunnel ..."
  ( lt --port "$TM_PORT" >"$TUNNEL_LOG" 2>&1 & echo $! >"$TUNNEL_PID_FILE" )
  local i url=""
  for i in $(seq 1 40); do
    url="$(grep -Eo 'https://[a-z0-9-]+\.loca\.lt' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)"
    [ -n "$url" ] && break
    pid_alive "$(read_pid "$TUNNEL_PID_FILE")" || { tail -n 20 "$TUNNEL_LOG" >&2; die "localtunnel exited early"; }
    sleep 0.5
  done
  [ -n "$url" ] || { tail -n 20 "$TUNNEL_LOG" >&2; die "could not parse tunnel URL from localtunnel log"; }
  echo "$url"
}

# Run a command with a hard timeout (macOS has no `timeout`). A stopped tailscaled
# makes the tailscale CLI block forever, so every tailscale call goes through this.
# Returns the command's exit status, or 124 if it had to be killed.
ts_run() {
  local t="$1"; shift
  "$@" &
  local pid=$! n=0 rc=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 0.5; n=$((n + 1))
    if [ "$n" -ge $((t * 2)) ]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
  done
  wait "$pid" || rc=$?   # capture status without tripping set -e
  return "$rc"
}

# Is tailscaled up and answering quickly?
ts_alive() { ts_run 4 "$TS_BIN" status >/dev/null 2>&1; }

# Tailscale ships its CLI on PATH (Homebrew) and/or inside the app bundle
# (cask/App Store) — and on macOS those talk to *different* daemons. Prefer a CLI
# whose daemon actually answers; otherwise fall back to any that exists so we can
# still emit a clear error.
resolve_ts_bin() {
  if [ -n "${TS_BIN:-}" ] && { command -v "$TS_BIN" >/dev/null 2>&1 || [ -x "$TS_BIN" ]; }; then return 0; fi
  local app="/Applications/Tailscale.app/Contents/MacOS/Tailscale" c
  for c in tailscale "$app"; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then
      if ts_run 4 "$c" status >/dev/null 2>&1; then TS_BIN="$c"; return 0; fi
    fi
  done
  for c in tailscale "$app"; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then TS_BIN="$c"; return 0; fi
  done
  return 1
}

start_tunnel_tailscale() {
  resolve_ts_bin || die "tailscale not found. Install the app (brew install --cask tailscale, or tailscale.com/download) and sign in, then re-run."
  ts_alive || die "tailscaled is not running/responding. Easiest fix: install the Tailscale app and sign in (open -a Tailscale). CLI-only installs need: sudo tailscaled install-system-daemon && sudo tailscale up"
  # Read backend state + MagicDNS name in one bounded call.
  local jf="$RUN_DIR/ts_status.json" state host
  ts_run 6 "$TS_BIN" status --json >"$jf" 2>/dev/null || die "tailscale status timed out (tailscaled not ready)."
  state="$(python -c 'import sys,json; print(json.load(open(sys.argv[1])).get("BackendState",""))' "$jf" 2>/dev/null || true)"
  host="$(python -c 'import sys,json; print((json.load(open(sys.argv[1])).get("Self") or {}).get("DNSName","").rstrip("."))' "$jf" 2>/dev/null || true)"
  [ "$state" = "Running" ] || die "Tailscale is not logged in (state: ${state:-unknown}). Run: $TS_BIN up"
  [ -n "$host" ] || die "could not determine Tailscale MagicDNS name (enable MagicDNS + HTTPS in the admin console)."
  log "Enabling Tailscale Funnel for 127.0.0.1:$TM_PORT on $host ..."
  # Funnel config lives in tailscaled (--bg), not a child process. Capture output:
  # if Funnel/HTTPS isn't permitted yet, tailscale prints an enable URL we surface.
  if ! ts_run 20 "$TS_BIN" funnel --bg "$TM_PORT" >"$TUNNEL_LOG" 2>&1; then
    warn "tailscale funnel failed — output:"
    cat "$TUNNEL_LOG" >&2
    die "Enable Funnel + HTTPS for this node in the Tailscale admin console (often a URL is printed above), then re-run."
  fi
  echo "https://$host"
}

verify_public_mcp() {
  # The tunnel host is covered by the wildcard allowlist in server.py, so the
  # handshake should succeed remotely. A brand-new Cloudflare quick tunnel needs
  # a few seconds for its edge route to propagate, so retry before giving up.
  local url="$1" attempt body
  log "Verifying public MCP handshake at $url ($TM_TRANSPORT; allow a few seconds for the tunnel) ..."
  for attempt in 1 2 3 4 5 6; do
    if [ "$TM_TRANSPORT" = "streamable-http" ]; then
      # Drive a real MCP initialize the way ChatGPT will. FastMCP replies with an
      # SSE-framed JSON-RPC result containing serverInfo/protocolVersion.
      body="$(curl -sS --max-time 10 -X POST "$url" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"tokenmaxxing-demo","version":"0"}}}' \
        2>/dev/null || true)"
      if printf '%s' "$body" | grep -q '"serverInfo"\|"protocolVersion"'; then
        log "Public handshake OK (MCP initialize returned serverInfo, attempt $attempt)."
        return 0
      fi
    else
      body="$(curl -sS -N --max-time 10 "$url" 2>/dev/null | head -3 || true)"
      if printf '%s' "$body" | grep -q 'event: endpoint'; then
        log "Public handshake OK (received SSE endpoint event, attempt $attempt)."
        return 0
      fi
    fi
    sleep 3
  done
  warn "Public handshake did not succeed after retries. Last response:"
  printf '%s\n' "${body:-<empty>}" >&2
  return 1
}

grant_workspace() {
  [ -d "$TM_WORKSPACE" ] || die "workspace not found: $TM_WORKSPACE (set TM_WORKSPACE)"
  local exec_flag=()
  local exec_note=""
  if [ "$TM_ALLOW_EXEC" = "1" ]; then
    exec_flag=(--allow-execute)
    exec_note=" (execute enabled)"
  fi
  log "Granting workspace $TM_WORKSPACE for $TM_TTL$exec_note ..."
  # ${arr[@]+"${arr[@]}"} expands to nothing for an empty array — required so
  # `set -u` does not abort on bash 3.2 (the macOS default).
  tokenmaxxing grant "$TM_WORKSPACE" --ttl "$TM_TTL" ${exec_flag[@]+"${exec_flag[@]}"} >/dev/null
}

cmd_start() {
  mkdir -p "$RUN_DIR"
  activate_venv
  cmd_stop_quiet              # enforce single daemon + single tunnel (handoff rule #5)
  start_daemon
  local url
  case "$TM_TUNNEL" in
    cloudflared)  url="$(start_tunnel_cloudflared)" ;;
    localtunnel)  url="$(start_tunnel_localtunnel)" ;;
    tailscale)    url="$(start_tunnel_tailscale)" ;;
    *) die "unknown TM_TUNNEL: $TM_TUNNEL (use cloudflared, localtunnel, or tailscale)" ;;
  esac
  log "Tunnel URL: $url"
  local connector_url="$url$TM_PATH"
  grant_workspace
  if ! verify_public_mcp "$connector_url"; then
    warn "Tunnel is up but the handshake check failed. The daemon and tunnel are still running; inspect:"
    warn "  daemon log: $DAEMON_LOG"
    warn "  tunnel log: $TUNNEL_LOG"
  fi
  printf '%s\n' "$connector_url" >"$URL_FILE"
  echo
  log "============================================================"
  log " ChatGPT custom connector URL ($TM_TRANSPORT):"
  printf '\n    %s\n\n' "$connector_url"
  log " Workspace: $TM_WORKSPACE  (TTL $TM_TTL)"
  log " Stop with: ./demo.sh stop"
  log "============================================================"
}

stop_tunnel() {
  # Only touch Funnel if tailscaled is actually responding, and bound the call
  # so a dead daemon can never hang teardown.
  if [ "$TM_TUNNEL" = "tailscale" ] && resolve_ts_bin && ts_alive; then
    ts_run 5 "$TS_BIN" funnel --https=443 off >/dev/null 2>&1 || true
  fi
  kill_pid_file "$TUNNEL_PID_FILE"
}

cmd_stop_quiet() {
  stop_tunnel
  kill_pid_file "$DAEMON_PID_FILE"
}

cmd_stop() {
  activate_venv
  log "Stopping tunnel and daemon ..."
  cmd_stop_quiet
  tokenmaxxing revoke >/dev/null 2>&1 || true
  rm -f "$URL_FILE"
  log "Revoked grant and stopped processes."
}

cmd_status() {
  activate_venv
  local dpid tpid
  dpid="$(read_pid "$DAEMON_PID_FILE")"; tpid="$(read_pid "$TUNNEL_PID_FILE")"
  if pid_alive "$dpid"; then log "daemon: running (pid $dpid, port $TM_PORT)"; else log "daemon: not running"; fi
  if pid_alive "$tpid"; then log "tunnel: running (pid $tpid)"; else log "tunnel: not running"; fi
  [ -f "$URL_FILE" ] && log "connector URL: $(cat "$URL_FILE")"
  echo "--- grant ---"
  tokenmaxxing status || true
}

cmd_url() {
  if [ -f "$URL_FILE" ]; then cat "$URL_FILE"; else die "no connector URL on file — run ./demo.sh start"; fi
}

case "${1:-start}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  url)    cmd_url ;;
  *) die "usage: $0 {start|stop|status|url}" ;;
esac

---
name: connect-conversations
description: Connect or reconnect this installed Pensieve plugin's automatic conversation capture when the user asks to save their agent conversations. Opens browser approval and completes local setup without asking the user to run terminal commands or handle credentials.
metadata:
  role: setup
  summary: Connect automatic conversation capture through browser approval.
---

# Connect conversations

Use this action when the user asks to connect conversation capture. Pairing
installs an upload-only credential for this client. It does not silently enable
capture, approve historical imports or inspect host login tokens.

This helper runs on the same local computer as the installed plugin and its
hooks. The supported capture contracts are local macOS Codex CLI and Claude
Code. A desktop app, Cowork or cloud workspace needs its own verified hook and
transcript support. An MCP connection by itself cannot capture conversations.
Do not run setup in a remote container and describe the user's local app as connected.

1. Tell the user that you are opening Pensieve's connection approval. Resolve
   `../../scripts/capture_setup.py` from this skill's directory. Run it with
   `python3`, action `start` and `--client codex` or `--client claude`, matching
   the current host. Set `--runtime codex_cli` or `--runtime claude_code_cli`
   only when the host is known to be that runtime; otherwise use `unknown`.
   These are commands for the agent's execution tool, not instructions to the user.
2. Open or show the helper's `verification_url` in the user's normal browser.
   The user approves in Pensieve, using the same account as this client's
   Pensieve connection. That page controls the selected company, capture and
   company knowledge sharing. Captured work transcripts are visible to members
   of that company. Do not approve on the user's behalf or silently change their settings.
3. Run the helper with action `poll`, the same `--client` and `--wait 45`.
   `paired` means local credentials were installed. It does not prove an upload
   happened or that capture is enabled. If approval is still pending, tell the
   user the next ordinary agent hook will complete it automatically. Do not
   repeatedly wait. After expiry or `restart_required`, run `start --restart`
   when the user is ready to finish again.
4. Invite the user to continue their work with the chosen Pensieve company.
   Capture starts from a fresh local baseline after setup. Confirm actual
   capture from the Conversations page's last upload, never from pairing alone.
5. To include earlier work, the user chooses the company, project folder and
   date range in Conversations. Run the helper with action `sync`, the same
   `--client` and `--wait 45` to check authorised import requests. This reads
   only the host's standard transcript store under the server's explicit grant.
   It never opens arbitrary selected folders or guesses a chat's company.
   Remaining work resumes at later hooks. `sync_checked` means a bounded check
   ran, not that all history finished; show progress from Conversations.

For a connection check, use action `status` with the same client. Its output is
safe to display. A paired credential for another client or account does not
authorise this client. Stop and explain a `setup_error` without opening private
configuration files. The user never needs to export chats, download setup files,
paste tokens, or copy commands into a terminal.

Historical import can only access files still stored in this execution
environment. Ordinary Claude Chat and ChatGPT Chat do not run this capture
contract. Desktop, Work and Cowork require their own verified hook, Python and
local transcript access. Never promise all account history or remote/cloud chats.

Never read, print, copy or attach the helper's private config, pending pairing
state, upload key or polling secret. Do not supply credentials in command-line
arguments, tool calls, URLs or chat. The helper sends them directly to Pensieve.

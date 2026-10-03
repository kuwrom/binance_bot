"""`shortbot-web`: the owner's browser console for the desk (docs/WEB.md).

Every desk command is reachable here through its commands.py declaration, the same one its MCP tool and `shortbot`
subcommand come from: a form generated from the body's signature (forms.py), run as actor web:<command> under its
weight class, a write submitted to the gateway as an intent (runner.py), exactly as the MCP server does. Nothing here
is human-only: the kill-switch clear and the other loosenings stay in `shortbot-admin` at a real terminal.

- auth.py: one owner password (scrypt hash beside the journal, set with `shortbot-web passwd`), in-memory sessions,
  a per-IP login throttle;
- server.py: the Starlette app (JSON API + the static page) on 127.0.0.1; Caddy terminates HTTPS in front of it
  (deploy/Caddyfile);
- static/: the page, plain JS and CSS, no CDN.
"""

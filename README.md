# x-skill

🐦 Read and post on X (Twitter) through the official [X API v2](https://docs.x.com/x-api): profiles, an account's recent posts, threads, recent search, post lookup by URL or ID, your home timeline, bookmarks and likes, and posting with a preview first. Python 3 standard library only.

An agent skill for [OpenClaw](https://openclaw.ai) (published on ClawHub as `x-api-v2`), Claude Code, Codex and other agents that read `SKILL.md`.

## Install

Name the folder `x-api-v2`, the skill's `name`:

```bash
openclaw skills install @odrobnik/x-api-v2                                   # OpenClaw
git clone https://github.com/odrobnik/x-skill ~/.claude/skills/x-api-v2       # Claude Code
git clone https://github.com/odrobnik/x-skill ~/.agents/skills/x-api-v2       # Codex
```

Then set up X credentials as described in [SETUP.md](SETUP.md): an app bearer token for reading, plus an OAuth 2.0 client for bookmarks, likes and posting.

## Quick Start

```bash
# Read (needs X_BEARER_TOKEN, or config.json in ~/.openclaw/x/ or $X_SKILL_CONFIG_DIR)
python3 scripts/x.py user XDevelopers
python3 scripts/x.py timeline XDevelopers --max 20 --exclude retweets,replies
python3 scripts/x.py search "from:XDevelopers -is:retweet" --max 10
python3 scripts/x.py thread https://x.com/XDevelopers/status/1234567890

# Sign in once for bookmarks, likes and posting (needs an OAuth 2.0 client, see SETUP.md)
python3 scripts/x.py auth
python3 scripts/x.py bookmarks --max 50
python3 scripts/x.py home --max 20 --exclude replies
python3 scripts/x.py post "Hello from OpenClaw!"          # preview only; add --yes to post
python3 scripts/x.py --json search "OpenClaw" | jq -r '.[].url'   # --json on any command: one JSON document on stdout
```

See [SKILL.md](SKILL.md) for all commands.

## Tests

```bash
python3 -m unittest discover tests
```

The tests run offline against a faked X API and a temporary config folder.

## License

MIT

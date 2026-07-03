# X (Twitter) API Skill

An [OpenClaw](https://openclaw.ai) skill for interacting with X (Twitter) via API v2.

## Features

- **User profiles** — bio, followers, verification status
- **Timelines** — user tweets with retweet/reply filters
- **Search** — recent tweets (last 7 days) with advanced operators
- **Threads** — conversation thread extraction
- **Tweet lookup** — single or batch by ID/URL
- **Bookmarks** — list bookmarked tweets (OAuth)
- **Likes** — user's liked tweets (OAuth)
- **Posting** — post tweets (OAuth)

## Setup

See [SETUP.md](SETUP.md) for credentials setup (bearer token + optional OAuth 2.0).

## Requirements

- Python 3 (stdlib only — no pip dependencies)
- X Developer account with API credentials

## TweetClaw Companion

Use this skill when you want direct X API v2 calls from local OpenClaw:
profiles, timelines, search, threads, bookmarks, likes, and posting. If the
workflow needs an OpenClaw plugin that also covers follower exports, media
upload or download, tweet monitors, webhooks, giveaway data, direct messages, or
reviewed post/reply actions through the Xquik API surface, install TweetClaw as
a companion instead of expanding this stdlib client:

```bash
npm install -g @xquik/tweetclaw
```

Keep this skill responsible for raw X API v2 access. Use TweetClaw for the
broader, approval-gated OpenClaw automation path.

## License

MIT

# Ticker

A personal AI news feed. Each person picks their own sources and topics. Ticker fetches the news several
times a day, AI ranks it by relevance, and a 👍 / 👎 teaches it what they like. It is built the same way as
[Popcorn](https://github.com/conradoplano/popcorn): Django, SQLite, one Docker container on the NAS,
passwordless email-code login, phone-first server-rendered pages (HTMX), installable on the home screen.

## What it does

- **First-login setup wizard**: describe yourself (optional), pick topics, name your sources (the app finds
  each feed), set preferences, and one AI call writes your starting profile and fills the feed.
- **Sources**: any RSS or Atom feed, added by name, website or feed address. A source is fetched once for
  everyone who follows it, with ETag / Last-Modified. Only title, snippet, link, time and image link are
  stored; the original page opens for reading.
- **Ranking**: new items are de-duplicated and muted subjects dropped, then the AI scores each item 0–100
  with the matching topic and a one-line reason. Without AI the feed is ranked by topic keywords.
- **Feed**: Best for me / Latest, filters by topic and source, search, "New" marks since the last visit,
  one card per story with the other sources listed, a morning briefing of the five most important items.
- **Learning**: 👍, 👎, opening and saving are stored; every week the AI rewrites a "What I have learned
  about you" profile, editable in Settings.
- **Discovery**: about one item in ten (adjustable) comes from a topic not followed yet, with
  **+ Follow this topic**.
- **Assistants (MCP)**: OAuth 2.1 + MCP at `/mcp` with `get_feed`, `get_briefing`, `get_settings`,
  `add_topic`, `add_source`, `rate_item`.
- Export of your data and deletion of your account in Settings. English only; every feed is private.

## Layout

| App | What |
| --- | --- |
| `accounts` | users, email-code login, `adduser` command |
| `core` | health check, manifest, AI helper and cost ledger (`AIUsage`) |
| `sources` | sources, follows, articles; fetching (`fetch.py`) and feed discovery (`discover.py`) |
| `topics` | topics, muted subjects, the wizard's topic catalogue |
| `feed` | scores, ratings, ranking, the feed, briefing, learned profile, scheduler, setup wizard, settings |
| `connect` | OAuth and MCP for assistants |

## Running it

Locally without Docker:

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
DEBUG=true DEV_LOGIN=true python manage.py migrate
DEBUG=true DEV_LOGIN=true python manage.py runserver
```

In a second terminal, the scheduler (fetching and refreshing):

```bash
DEBUG=true python manage.py run_scheduler
```

The production image locally on http://localhost:8063:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

Tests: `python manage.py test` (with `DEBUG=true`).

Useful commands: `adduser <email> [--admin]`, `fetch_sources [--force]`, `refresh_feeds [--email ...]`,
`run_scheduler [--once]`.

## Deploy

Every push to `main` runs the tests and publishes `ghcr.io/conradoplano/ticker:latest`
(`.github/workflows/docker.yml`). On the NAS, create a Container Manager project from
`deploy/docker-compose.nas.yml`, keep SQLite in the mounted `./data` folder (back it up by copying it), and
put the DSM reverse proxy with HTTPS in front of port 5063. The scheduler runs inside the same container
(`RUN_SCHEDULER=true`).

AI uses OpenAI as in Popcorn (`OPENAI_API_KEY`, `AI_MODEL`, `AI_EFFORT`). New people get a free AI trial for
their first `AI_TRIAL_HOURS` (default 24), so the setup wizard and the first feed work straight away. The trial
is capped at `AI_TRIAL_LIMIT_USD` per person (default $0.10) and `AI_TRIAL_GLOBAL_DAILY_LIMIT_USD` per day for all
trial users together (default $0.50), so mass sign-ups can't run up costs. After that, a person uses AI once an
admin approves them on the Admin page (Settings → Open admin). `AI_DAILY_LIMIT_USD` (per person, changeable per
person) and `AI_GLOBAL_DAILY_LIMIT_USD` cap the cost for everyone, and every call is recorded with its cost.

## Licence

MIT

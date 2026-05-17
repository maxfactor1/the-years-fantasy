# The Years Fantasy

A keeper management web app for a 12-team Yahoo Fantasy Football league. Tracks keeper eligibility, franchise tags, draft pick trades, and draft board state across seasons.

## Features

- **Keeper eligibility** — calculates keep round and eligibility for every player on every team based on original draft year/round and years kept
- **Franchise tags** — mark players as franchise tagged; tracks re-franchise blocks and historical tags
- **Draft board** — visual round-by-round board showing which picks are occupied by keepers or franchise tags, with traded-pick overlays
- **Pick trades** — record traded draft picks; board updates automatically
- **ADP integration** — pulls FantasyPros ADP to determine franchise tag round costs
- **Admin panel** — password-protected UI for all data management, ETL triggers, and validation tools
- **Data validation** — browse raw draft picks, transactions, rosters, and ADP matching by season

## Tech Stack

- **Backend**: Python 3.11, FastAPI, SQLAlchemy (async), aiosqlite
- **Frontend**: Vanilla JS, no framework
- **Database**: SQLite (persisted via Docker volume)
- **Auth**: Yahoo OAuth 2.0 for API access; JWT for admin panel
- **Deployment**: Docker / Fly.io

## Keeper Rules

**Regular keepers** (max 3 per team):
- Only previously drafted players — original draft year/round is tracked across all seasons
- Original 1st-round picks are never keepable
- Keep cost = `original_draft_round − years_since_original_draft` — must be ≥ Round 2
- Maximum 5 total seasons on roster since original draft
- Kickers and Defenses ineligible

**Franchise tags** (max 4 per team, each beyond 1 costs a keeper slot):
- Any player on the week-14 roster (drafted or undrafted)
- Round cost determined by FantasyPros ADP rank
- Cannot re-franchise a player tagged the previous season
- Cannot franchise original 1st-round picks or players whose keep cost hits Round 1
- Kickers and Defenses ineligible
- A franchise-tagged player is **completely ineligible** the following season (no regular keep, no re-tag)

## Local Development

### Prerequisites

- Docker Desktop
- A [Yahoo Developer app](https://developer.yahoo.com/apps/) with Fantasy Sports read scope
- A [FantasyPros API key](https://www.fantasypros.com/api/)

### Setup

1. Copy the example env file and fill in your values:
   ```bash
   cp .env.example .env
   ```

2. Key variables to set in `.env`:
   ```
   YAHOO_CLIENT_ID=
   YAHOO_CLIENT_SECRET=
   YAHOO_LEAGUE_ID=179398        # your current season league ID
   YAHOO_PREVIOUS_SEASON=2025    # most recently completed season
   YAHOO_UPCOMING_SEASON=2026    # draft year you're preparing for
   FANTASYPROS_API_KEY=
   ADMIN_PASSWORD=
   SECRET_KEY=                   # long random string for JWT signing
   OAUTH_REDIRECT_URI=http://localhost:8000/setup/callback
   ```

3. Start the container:
   ```bash
   docker compose up -d
   ```

4. Authorize Yahoo OAuth — visit `http://localhost:8000/setup/authorize` and complete the flow.

5. Go to the admin panel at `http://localhost:8000/admin`:
   - Add historical league IDs (Yahoo creates a new league ID each season — find them in the Yahoo URL)
   - Click **Run Initial ETL** to fetch teams, draft picks, transactions, and rosters
   - Fetch each historical season using the Historical League IDs card
   - Click **Recalculate Keepers** when done
   - Import ADP data and mark any historical franchise tags

The public site is at `http://localhost:8000`.

## Project Structure

```
app/
  api/
    admin.py        # admin endpoints (ETL, franchise tags, validation, etc.)
    keepers.py      # public keeper/team summary endpoints
    setup.py        # Yahoo OAuth flow
  services/
    keeper_service.py       # keeper eligibility calculation engine
    yahoo_service.py        # Yahoo Fantasy API client
    etl_service.py          # orchestrates full data import
    fantasypros_service.py  # ADP data fetching
  models.py         # SQLAlchemy models
  config.py         # settings from env vars
  database.py       # async DB session setup
  main.py           # FastAPI app + router registration
  static/
    index.html      # public draft board / team view
    admin.html      # admin panel
    js/
      app.js        # public site JS
      admin.js      # admin panel JS
    css/
      style.css
```

## Key Data Notes

- **Yahoo's `is_keeper` flag in `draftresults`** is always `false` — the app fetches keeper status from the `league/players` endpoint instead, which correctly returns `is_keeper.kept: true`
- **Player keys change each season** (e.g. `449.p.40993` in 2024 → `461.p.40993` in 2025) — all cross-season lookups use the numeric player ID suffix
- **Team IDs are not stable** across seasons — team matching uses team names
- **Yahoo's draft includes both keeper and fresh picks** — a pick is a keeper only if the same team drafted the player as had them on their week-14 roster the prior season

## Deployment (Fly.io)

### First-time deploy

```bash
fly apps create theyearsfantasy
fly volumes create fantasy_data --region ord --size 1

fly secrets set \
  YAHOO_CLIENT_ID="..." \
  YAHOO_CLIENT_SECRET="..." \
  FANTASYPROS_API_KEY="..." \
  ADMIN_PASSWORD="..." \
  SECRET_KEY="..." \
  OAUTH_REDIRECT_URI="https://theyearsfantasy.fly.dev/setup/callback"

fly deploy
```

### Subsequent deploys

```bash
fly deploy
```

### After deploying to a fresh environment

Re-authorize Yahoo at `https://theyearsfantasy.fly.dev/setup/authorize`, then follow the data setup steps from the admin panel (same as local setup above).

### Logs

```bash
fly logs
```

## Admin Panel Reference

| Section | Purpose |
|---|---|
| System Status | Auth state, ETL status, quick-action buttons |
| Historical League IDs | Map past seasons to Yahoo league IDs; trigger per-season fetches |
| Historical Franchise Tags | Mark which keeper picks from past seasons were franchise tags |
| Franchise Tag Management | Set franchise tags for the upcoming season |
| Draft Order | Set the snake draft order for the upcoming season |
| Pick Trades | Record traded picks; reflected on the draft board |
| Data Validation | Browse raw draft picks, transactions, and rosters by season |
| Historical Keeper Import | JSON import for keeper overrides when Yahoo data is insufficient |

"""
Yahoo Fantasy Sports API client.

Handles OAuth 2.0 token management and all data fetching for the league.
The app requires a one-time authorization via /setup/authorize before any
Yahoo data can be fetched. Tokens are stored in the database and auto-refreshed.
"""

import base64
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AppSetting, OAuthToken, Player, Team, YahooGameKey

logger = logging.getLogger(__name__)
settings = get_settings()

# Ineligible positions for keeper/franchise purposes
INELIGIBLE_POSITIONS = {"K", "DEF", "D"}


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def build_authorization_url(state: str = "") -> str:
    params = (
        f"client_id={settings.yahoo_client_id}"
        f"&redirect_uri={quote(settings.oauth_redirect_uri, safe='')}"
        f"&response_type=code"
        f"&scope={quote('fspt-r openid', safe='')}"
        f"&language=en-us"
    )
    if state:
        params += f"&state={state}"
    return f"{settings.yahoo_auth_url}?{params}"


def _basic_auth_header() -> str:
    creds = f"{settings.yahoo_client_id}:{settings.yahoo_client_secret}"
    encoded = base64.b64encode(creds.encode()).decode()
    return f"Basic {encoded}"


async def exchange_code_for_tokens(code: str, db: AsyncSession) -> OAuthToken:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            settings.yahoo_token_url,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "redirect_uri": settings.oauth_redirect_uri,
                "code": code,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    token = OAuthToken(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600)),
        yahoo_guid=data.get("xoauth_yahoo_guid"),
    )
    # Replace any existing token row
    existing = await db.execute(select(OAuthToken))
    for row in existing.scalars().all():
        await db.delete(row)
    db.add(token)
    await db.commit()
    await db.refresh(token)
    logger.info("Yahoo OAuth tokens stored")
    return token


async def _get_valid_token(db: AsyncSession) -> str:
    result = await db.execute(select(OAuthToken).limit(1))
    token: Optional[OAuthToken] = result.scalar_one_or_none()
    if not token:
        raise RuntimeError("Yahoo not authorized — visit /setup/authorize first")

    if datetime.utcnow() >= token.expires_at - timedelta(minutes=5):
        token = await _refresh_token(token, db)

    return token.access_token


async def _refresh_token(token: OAuthToken, db: AsyncSession) -> OAuthToken:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            settings.yahoo_token_url,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "redirect_uri": settings.oauth_redirect_uri,
                "refresh_token": token.refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    token.access_token = data["access_token"]
    token.refresh_token = data.get("refresh_token", token.refresh_token)
    token.expires_at = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))
    await db.commit()
    logger.info("Yahoo access token refreshed")
    return token


# ---------------------------------------------------------------------------
# Generic API caller
# ---------------------------------------------------------------------------

class YahooAccessError(Exception):
    """Raised when Yahoo returns 403 — league likely didn't exist for that season."""
    pass


async def _yahoo_get(path: str, db: AsyncSession, params: dict | None = None) -> dict:
    access_token = await _get_valid_token(db)
    url = f"{settings.yahoo_api_base}/{path}"
    if params is None:
        params = {}
    params["format"] = "json"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        if 400 <= resp.status_code < 500:
            raise YahooAccessError(f"HTTP {resp.status_code}: {url} — league may not exist for this season or request invalid")
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Game key resolution
# ---------------------------------------------------------------------------

async def get_game_key(season: int, db: AsyncSession) -> str:
    """Return the Yahoo game_key (e.g. '449') for a given NFL season year."""
    result = await db.execute(select(YahooGameKey).where(YahooGameKey.season == season))
    cached = result.scalar_one_or_none()
    if cached:
        return cached.game_key

    data = await _yahoo_get(f"games;game_codes=nfl;seasons={season}", db)
    games_raw = data.get("fantasy_content", {}).get("games", {})

    game_key = None
    count = int(games_raw.get("count", 0))
    for i in range(count):
        game = games_raw.get(str(i), {}).get("game", [{}])[0]
        if str(game.get("season")) == str(season):
            game_key = str(game.get("game_key", ""))
            break

    if not game_key:
        raise ValueError(f"Could not find Yahoo game_key for season {season}")

    db.add(YahooGameKey(season=season, game_key=game_key))
    await db.commit()
    logger.info(f"Resolved game_key={game_key} for season {season}")
    return game_key


async def get_league_id_for_season(season: int, db: AsyncSession) -> str:
    """Return the Yahoo league_id for a given season, falling back to settings default."""
    result = await db.execute(
        select(AppSetting).where(AppSetting.key == f"yahoo_league_id_{season}")
    )
    row = result.scalar_one_or_none()
    return row.value if row else settings.yahoo_league_id


def _league_key(game_key: str, league_id: str) -> str:
    return f"{game_key}.l.{league_id}"


def _team_key(game_key: str, team_id: int, league_id: str) -> str:
    return f"{game_key}.l.{league_id}.t.{team_id}"


# ---------------------------------------------------------------------------
# League / team data
# ---------------------------------------------------------------------------

async def get_league_info(season: int, db: AsyncSession) -> dict:
    gk = await get_game_key(season, db)
    lid = await get_league_id_for_season(season, db)
    data = await _yahoo_get(f"league/{_league_key(gk, lid)}", db)
    league_raw = data.get("fantasy_content", {}).get("league", [{}])
    return league_raw[0] if isinstance(league_raw, list) else league_raw


async def fetch_and_store_teams(season: int, db: AsyncSession) -> list[Team]:
    gk = await get_game_key(season, db)
    lid = await get_league_id_for_season(season, db)
    data = await _yahoo_get(f"league/{_league_key(gk, lid)}/teams", db)

    league_node = data.get("fantasy_content", {}).get("league", [{}, {}])
    teams_node = league_node[1].get("teams", {}) if len(league_node) > 1 else {}
    count = int(teams_node.get("count", 0))

    stored: list[Team] = []
    for i in range(count):
        raw_team = teams_node.get(str(i), {}).get("team", [])
        if not raw_team:
            continue

        info: dict = {}
        manager_info: dict = {}
        for chunk in raw_team:
            if isinstance(chunk, list):
                for item in chunk:
                    if isinstance(item, dict):
                        info.update(item)
            elif isinstance(chunk, dict):
                if "managers" in chunk:
                    mgrs = chunk["managers"]
                    if mgrs and isinstance(mgrs, list):
                        manager_info = mgrs[0].get("manager", {})
                else:
                    info.update(chunk)

        team_key = info.get("team_key", "")
        team_id = int(info.get("team_id", 0))

        # Get logo from team_logos list
        logos = info.get("team_logos", [{}])
        logo_url = logos[0].get("team_logo", {}).get("url") if logos else None

        team = Team(
            team_key=team_key,
            season=season,
            team_id=team_id,
            name=info.get("name", f"Team {team_id}"),
            manager_name=manager_info.get("nickname") or manager_info.get("manager_id"),
            manager_guid=manager_info.get("guid"),
            logo_url=logo_url,
            waiver_priority=int(info.get("waiver_priority", 0)) or None,
        )
        # Upsert
        existing = await db.execute(
            select(Team).where(Team.team_key == team_key, Team.season == season)
        )
        if existing.scalar_one_or_none():
            await db.execute(
                Team.__table__.update()
                .where(Team.team_key == team_key, Team.season == season)
                .values(
                    name=team.name,
                    manager_name=team.manager_name,
                    manager_guid=team.manager_guid,
                    logo_url=team.logo_url,
                )
            )
        else:
            db.add(team)
        stored.append(team)

    await db.commit()
    logger.info(f"Stored {len(stored)} teams for season {season}")
    return stored


# ---------------------------------------------------------------------------
# Draft results
# ---------------------------------------------------------------------------

async def _fetch_keeper_flags(league_key: str, player_keys: list[str], db: AsyncSession) -> dict[str, dict]:
    """Batch-fetch is_keeper data from the league players endpoint.
    Returns {player_key: {"kept": bool, "cost": int|None}}.
    Yahoo omits is_keeper from draftresults but populates it on the players endpoint.
    """
    flags: dict[str, dict] = {}
    chunk_size = 25
    for i in range(0, len(player_keys), chunk_size):
        chunk = player_keys[i:i + chunk_size]
        keys_param = ",".join(chunk)
        data = await _yahoo_get(f"league/{league_key}/players;player_keys={keys_param}", db)
        league_node = data.get("fantasy_content", {}).get("league", [{}, {}])
        players_node = league_node[1].get("players", {}) if len(league_node) > 1 else {}
        count = int(players_node.get("count", 0))
        for j in range(count):
            player_list = players_node.get(str(j), {}).get("player", [])
            if not player_list or not isinstance(player_list[0], list):
                continue
            player_key = None
            keeper_info: dict = {}
            for item in player_list[0]:
                if not isinstance(item, dict):
                    continue
                if "player_key" in item:
                    player_key = item["player_key"]
                if "is_keeper" in item:
                    keeper_info = item["is_keeper"]
            if player_key:
                cost_raw = keeper_info.get("cost", False)
                flags[player_key] = {
                    "kept": keeper_info.get("kept", False) is True,
                    "cost": int(cost_raw) if cost_raw and cost_raw is not False else None,
                }
    return flags


async def fetch_and_store_draft_results(season: int, db: AsyncSession) -> list[dict]:
    gk = await get_game_key(season, db)
    lid = await get_league_id_for_season(season, db)
    lk = _league_key(gk, lid)
    data = await _yahoo_get(f"league/{lk}/draftresults", db)

    league_node = data.get("fantasy_content", {}).get("league", [{}, {}])
    draft_node = league_node[1].get("draft_results", {}) if len(league_node) > 1 else {}
    count = int(draft_node.get("count", 0))

    from app.models import DraftPick
    picks: list[dict] = []

    for i in range(count):
        raw = draft_node.get(str(i), {}).get("draft_result", {})
        if not raw:
            continue

        pick_num = int(raw.get("pick", i + 1))
        round_num = int(raw.get("round", 1))
        team_key = raw.get("team_key", "")
        player_key = raw.get("player_key") or None

        pick_data = {
            "season": season,
            "pick_number": pick_num,
            "round_number": round_num,
            "team_key": team_key,
            "player_key": player_key,
            "is_keeper": False,
            "keeper_cost_round": None,
        }

        existing = await db.execute(
            select(DraftPick).where(
                DraftPick.season == season, DraftPick.pick_number == pick_num
            )
        )
        existing_row = existing.scalar_one_or_none()
        if existing_row:
            existing_row.player_key = player_key
            existing_row.team_key = team_key
            existing_row.is_keeper = False
            existing_row.keeper_cost_round = None
        else:
            db.add(DraftPick(**pick_data))

        picks.append(pick_data)

    await db.commit()

    # Fetch real keeper flags from the players endpoint (draftresults never populates is_keeper)
    player_keys = [p["player_key"] for p in picks if p.get("player_key")]
    if player_keys:
        keeper_flags = await _fetch_keeper_flags(f"{gk}.l.{lid}", player_keys, db)
        for pk, info in keeper_flags.items():
            if not info["kept"]:
                continue
            row_res = await db.execute(
                select(DraftPick).where(DraftPick.season == season, DraftPick.player_key == pk)
            )
            row = row_res.scalar_one_or_none()
            if row:
                row.is_keeper = True
                row.keeper_cost_round = info["cost"]
        await db.commit()

    logger.info(f"Stored {len(picks)} draft picks for season {season}")
    return picks


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

async def fetch_and_store_transactions(season: int, db: AsyncSession) -> int:
    """
    Fetches all add/drop/trade transactions for the season.
    Yahoo paginates transactions; fetches up to 2500 total.
    """
    from app.models import Transaction

    gk = await get_game_key(season, db)
    lid = await get_league_id_for_season(season, db)
    lk = _league_key(gk, lid)

    total_stored = 0
    start = 0
    count = 250  # Yahoo max per request

    while True:
        data = await _yahoo_get(
            f"league/{lk}/transactions;types=add,drop,trade,commissioner;count={count};start={start}",
            db,
        )
        league_node = data.get("fantasy_content", {}).get("league", [{}, {}])
        txn_node = league_node[1].get("transactions", {}) if len(league_node) > 1 else {}
        txn_count = int(txn_node.get("count", 0))

        if txn_count == 0:
            break

        for i in range(txn_count):
            raw_txn = txn_node.get(str(i), {}).get("transaction", [{}])
            txn_info = raw_txn[0] if raw_txn else {}

            txn_key = txn_info.get("transaction_key", f"{season}_{start}_{i}")
            txn_type = txn_info.get("type", "unknown")

            ts_raw = txn_info.get("timestamp")
            ts = datetime.fromtimestamp(int(ts_raw)) if ts_raw else None

            existing = await db.execute(
                select(Transaction).where(Transaction.transaction_key == txn_key)
            )
            existing_row = existing.scalar_one_or_none()
            if existing_row:
                existing_row.details_json = json.dumps(raw_txn)
            else:
                db.add(
                    Transaction(
                        transaction_key=txn_key,
                        season=season,
                        transaction_type=txn_type,
                        timestamp=ts,
                        details_json=json.dumps(raw_txn),
                    )
                )
                total_stored += 1

        await db.commit()
        start += txn_count
        if txn_count < count:
            break

    logger.info(f"Stored {total_stored} transactions for season {season}")
    return total_stored


# ---------------------------------------------------------------------------
# Roster snapshots (week 14 end)
# ---------------------------------------------------------------------------

async def fetch_and_store_week14_rosters(season: int, db: AsyncSession, week: int = 14) -> int:
    """
    Fetches rosters for all teams at end of the given week number.
    Falls back to current/final roster if the week-based query fails.
    """
    from app.models import RosterSnapshot

    gk = await get_game_key(season, db)
    lid = await get_league_id_for_season(season, db)

    # Get all teams for the season
    team_result = await db.execute(select(Team).where(Team.season == season))
    teams = team_result.scalars().all()

    if not teams:
        teams_fetched = await fetch_and_store_teams(season, db)
        teams = teams_fetched

    total_stored = 0
    for team in teams:
        team_id = team.team_id
        team_key = team.team_key

        # Try week-based query first, fall back to current roster
        try:
            data = await _yahoo_get(
                f"team/{_team_key(gk, team_id, lid)}/roster;week={week}",
                db,
            )
        except YahooAccessError:
            logger.warning(f"Week {week} roster query failed for team {team_key}, trying current roster")
            data = await _yahoo_get(
                f"team/{_team_key(gk, team_id, lid)}/roster",
                db,
            )
        team_node = data.get("fantasy_content", {}).get("team", [{}, {}])
        roster_node = team_node[1].get("roster", {}) if len(team_node) > 1 else {}
        players_node = roster_node.get("0", {}).get("players", {})
        player_count = int(players_node.get("count", 0))

        for j in range(player_count):
            player_raw = players_node.get(str(j), {}).get("player", [])
            if not player_raw:
                continue

            player_info: dict = {}
            for chunk in player_raw:
                if isinstance(chunk, list):
                    for item in chunk:
                        if isinstance(item, dict):
                            player_info.update(item)
                elif isinstance(chunk, dict):
                    player_info.update(chunk)

            player_key = player_info.get("player_key", "")
            if not player_key:
                continue

            selected_pos = player_info.get("selected_position", [{}])
            roster_pos = selected_pos[1].get("position") if len(selected_pos) > 1 else None
            acq_type = player_info.get("acquisition_type", "unknown")

            existing = await db.execute(
                select(RosterSnapshot).where(
                    RosterSnapshot.season == season,
                    RosterSnapshot.team_key == team_key,
                    RosterSnapshot.player_key == player_key,
                )
            )
            if not existing.scalar_one_or_none():
                db.add(
                    RosterSnapshot(
                        season=season,
                        team_key=team_key,
                        player_key=player_key,
                        roster_position=roster_pos,
                        acquisition_type=acq_type,
                    )
                )
                total_stored += 1

            # Also upsert the player record
            await _upsert_player_from_info(player_info, db)

        await db.commit()
        logger.info(f"Stored roster snapshot for team {team.name} ({season})")

    return total_stored


async def _upsert_player_from_info(info: dict, db: AsyncSession) -> None:
    player_key = info.get("player_key", "")
    if not player_key:
        return

    name_info = info.get("name", {})
    position_data = info.get("eligible_positions", [])
    primary_pos = "UNK"
    if isinstance(position_data, list) and position_data:
        first = position_data[0]
        if isinstance(first, dict):
            primary_pos = first.get("position", "UNK")
        elif isinstance(first, str):
            primary_pos = first

    existing = await db.execute(select(Player).where(Player.player_key == player_key))
    if existing.scalar_one_or_none():
        return

    db.add(
        Player(
            player_key=player_key,
            player_id=int(info.get("player_id", 0)),
            name_full=name_info.get("full", info.get("player_key", "")),
            name_first=name_info.get("first"),
            name_last=name_info.get("last"),
            position=primary_pos,
            nfl_team=info.get("editorial_team_abbr"),
            bye_week=int(info.get("bye_weeks", {}).get("week", 0)) or None,
        )
    )


# ---------------------------------------------------------------------------
# Player info (bulk fetch by player keys)
# ---------------------------------------------------------------------------

async def fetch_players_by_keys(player_keys: list[str], db: AsyncSession) -> None:
    """Fetches and stores player metadata for a list of Yahoo player_keys."""
    if not player_keys:
        return

    # Batch into groups of 25 (Yahoo limit)
    batch_size = 25
    for i in range(0, len(player_keys), batch_size):
        batch = player_keys[i : i + batch_size]
        keys_param = ",".join(batch)
        try:
            data = await _yahoo_get(f"players;player_keys={keys_param}", db)
            players_node = data.get("fantasy_content", {}).get("players", {})
            count = int(players_node.get("count", 0))
            for j in range(count):
                player_raw = players_node.get(str(j), {}).get("player", [])
                if not player_raw:
                    continue
                player_info: dict = {}
                for chunk in player_raw:
                    if isinstance(chunk, list):
                        for item in chunk:
                            if isinstance(item, dict):
                                player_info.update(item)
                    elif isinstance(chunk, dict):
                        player_info.update(chunk)
                await _upsert_player_from_info(player_info, db)
            await db.commit()
        except Exception as exc:
            logger.warning(f"Failed to fetch player batch: {exc}")


async def is_authorized(db: AsyncSession) -> bool:
    result = await db.execute(select(OAuthToken).limit(1))
    return result.scalar_one_or_none() is not None

"""
FantasyPros API client for consensus ADP rankings (Standard scoring, non-PPR).

The hourly job calls refresh_adp_data() which replaces all ADP rows for the
upcoming season so the keeper eligibility engine always has fresh data.
"""

import logging
import re
from datetime import datetime

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AdpData, Player

logger = logging.getLogger(__name__)
settings = get_settings()


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/suffixes for fuzzy matching."""
    name = name.lower()
    name = re.sub(r"\s+(jr\.?|sr\.?|ii|iii|iv|v)$", "", name)
    name = re.sub(r"[^a-z ]", "", name)
    return name.strip()


async def fetch_adp_rankings(season: int) -> list[dict]:
    """
    Fetch consensus rankings from FantasyPros public API.
    Uses the /public/v2/json/NFL/players endpoint with ECR and standard scoring.
    """
    url = f"{settings.fantasypros_api_base}/players"
    params = {
        "show": "pos_rank",
        "ecr": "included",
        "sport": "NFL",
        "scoring": "STD",
        "type": "draft",
    }
    headers = {"x-api-key": settings.fantasypros_api_key}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    players = data.get("players", []) or data.get("data", []) or []
    if not players:
        raise Exception(f"FantasyPros returned empty player list from {url}")

    logger.info(f"Fetched {len(players)} players from FantasyPros")
    return _parse_players(players, season)


def _parse_players(players: list, season: int) -> list[dict]:
    results: list[dict] = []
    for idx, p in enumerate(players):
        results.append(
            {
                "fantasypros_player_id": p.get("player_id") or p.get("id"),
                "player_name": p.get("player_name") or p.get("name", ""),
                "position": (p.get("position_id") or p.get("player_position_id") or p.get("position", "")).upper(),
                "nfl_team": (p.get("team_id") or p.get("player_team_id") or p.get("team", "")).upper() or None,
                "adp_rank": int(p.get("rank_ecr") or p.get("rank") or idx + 1),
                "adp_value": float(p.get("rank_adp") or p.get("avg") or p.get("adp") or idx + 1),
            }
        )
    return results



async def refresh_adp_data(db: AsyncSession) -> int:
    """Replace all ADP rows for the upcoming season, then re-match Yahoo players."""
    season = settings.yahoo_upcoming_season

    raw = await fetch_adp_rankings(season)

    await db.execute(delete(AdpData).where(AdpData.season == season))

    now = datetime.utcnow()
    for entry in raw:
        db.add(
            AdpData(
                season=season,
                fantasypros_player_id=entry["fantasypros_player_id"],
                player_name=entry["player_name"],
                position=entry["position"],
                nfl_team=entry["nfl_team"],
                adp_rank=entry["adp_rank"],
                adp_value=entry["adp_value"],
                updated_at=now,
            )
        )

    await db.commit()
    await _match_adp_to_yahoo_players(db, season)
    logger.info(f"ADP data refreshed: {len(raw)} players for season {season}")
    return len(raw)


async def _match_adp_to_yahoo_players(db: AsyncSession, season: int) -> None:
    """
    Attempt to match FantasyPros player entries to Yahoo player_keys by
    normalized name + position. Updates AdpData.yahoo_player_key and
    Player.fantasypros_id / Player.fantasypros_name.
    """
    adp_result = await db.execute(select(AdpData).where(AdpData.season == season))
    adp_rows = adp_result.scalars().all()

    yahoo_result = await db.execute(select(Player))
    yahoo_players = yahoo_result.scalars().all()

    # Build lookup: (normalized_name, position) -> Player
    yahoo_lookup: dict[tuple[str, str], Player] = {}
    for p in yahoo_players:
        key = (_normalize_name(p.name_full), _canon_position(p.position))
        yahoo_lookup[key] = p

    matched = 0
    for adp in adp_rows:
        norm_name = _normalize_name(adp.player_name)
        canon_pos = _canon_position(adp.position)
        yahoo_player = yahoo_lookup.get((norm_name, canon_pos))

        if yahoo_player:
            adp.yahoo_player_key = yahoo_player.player_key
            yahoo_player.fantasypros_id = adp.fantasypros_player_id
            yahoo_player.fantasypros_name = adp.player_name
            matched += 1

    await db.commit()
    logger.info(f"Matched {matched}/{len(adp_rows)} FantasyPros players to Yahoo")


def _canon_position(pos: str) -> str:
    pos = pos.upper().strip()
    if pos in ("DST", "D/ST", "DEF"):
        return "DEF"
    return pos


async def get_adp_for_player_key(player_key: str, season: int, db: AsyncSession) -> AdpData | None:
    result = await db.execute(
        select(AdpData).where(
            AdpData.yahoo_player_key == player_key,
            AdpData.season == season,
        )
    )
    return result.scalar_one_or_none()


async def get_adp_for_player_name(name: str, position: str, season: int, db: AsyncSession) -> AdpData | None:
    norm = _normalize_name(name)
    canon = _canon_position(position)
    adp_result = await db.execute(select(AdpData).where(AdpData.season == season))
    for adp in adp_result.scalars().all():
        if _normalize_name(adp.player_name) == norm and _canon_position(adp.position) == canon:
            return adp
    return None

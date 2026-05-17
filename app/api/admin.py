"""
Admin API — password-protected endpoints for managing franchise tags,
triggering ETL, and importing historical keeper overrides.
"""

import difflib
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import (
    AdpData,
    AppSetting,
    DraftOrder,
    DraftPick,
    FranchiseTag,
    HistoricalKeeperOverride,
    KeeperEligibility,
    PickOwnership,
    Player,
    RosterSnapshot,
    Team,
    Transaction,
)
from app.services import keeper_service
from app.services.etl_service import get_app_status, import_pick_trades_from_transactions, run_initial_etl
from app.services.yahoo_service import YahooAccessError, fetch_and_store_draft_results, fetch_and_store_teams, fetch_and_store_transactions, fetch_and_store_week14_rosters, fetch_players_by_keys

router = APIRouter()
settings = get_settings()

ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str


def _create_token() -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": "admin", "exp": expire}, settings.secret_key, algorithm=ALGORITHM)


def _verify_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = authorization.removeprefix("Bearer ")
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        if payload.get("sub") != "admin":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        return "admin"
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


@router.post("/login")
async def login(req: LoginRequest):
    if req.password != settings.admin_password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password")
    return {"access_token": _create_token(), "token_type": "bearer"}


# ---------------------------------------------------------------------------
# ETL management
# ---------------------------------------------------------------------------

@router.get("/status")
async def admin_status(db: AsyncSession = Depends(get_db), _: str = Depends(_verify_token)):
    return await get_app_status(db)


@router.post("/etl/run")
async def trigger_etl(db: AsyncSession = Depends(get_db), _: str = Depends(_verify_token)):
    result = await run_initial_etl(db)
    return result


@router.post("/etl/recalculate-keepers")
async def recalc_keepers(db: AsyncSession = Depends(get_db), _: str = Depends(_verify_token)):
    await keeper_service.calculate_keeper_eligibility(db)
    return {"status": "recalculated"}


@router.post("/etl/import-pick-trades")
async def trigger_import_pick_trades(db: AsyncSession = Depends(get_db), _: str = Depends(_verify_token)):
    count = await import_pick_trades_from_transactions(db)
    return {"status": "done", "picks_imported": count}


# ---------------------------------------------------------------------------
# Historical league ID management
# ---------------------------------------------------------------------------

class SeasonLeagueRequest(BaseModel):
    season: int
    league_id: str


@router.get("/historical-leagues")
async def get_historical_leagues(db: AsyncSession = Depends(get_db), _: str = Depends(_verify_token)):
    result = await db.execute(
        select(AppSetting).where(AppSetting.key.like("yahoo_league_id_%"))
    )
    rows = result.scalars().all()
    entries = []
    for r in rows:
        try:
            season = int(r.key.split("yahoo_league_id_")[1])
            entries.append({"season": season, "league_id": r.value})
        except (IndexError, ValueError):
            pass
    return sorted(entries, key=lambda x: x["season"], reverse=True)


@router.post("/historical-leagues")
async def set_historical_league(
    req: SeasonLeagueRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    key = f"yahoo_league_id_{req.season}"
    result = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = req.league_id
    else:
        db.add(AppSetting(key=key, value=req.league_id))
    await db.commit()
    return {"status": "saved"}


@router.delete("/historical-leagues/{season}")
async def delete_historical_league(
    season: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    key = f"yahoo_league_id_{season}"
    result = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return {"status": "deleted"}


@router.post("/etl/fetch-season")
async def fetch_season(
    season: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    """Fetch teams, draft picks, and transactions for a single historical season."""
    results: dict = {}
    try:
        await fetch_and_store_teams(season, db)
        await fetch_and_store_draft_results(season, db)
        await fetch_and_store_transactions(season, db)
        roster_count = await fetch_and_store_week14_rosters(season, db, week=14)
        results["data"] = "done"
        results["rosters"] = f"{roster_count} entries"
    except YahooAccessError as e:
        return {"status": "failed", "error": str(e)}

    # Fetch player metadata for any new draft pick keys
    from app.models import DraftPick as DP
    keys_result = await db.execute(
        select(DP.player_key).where(DP.season == season, DP.player_key.isnot(None))
    )
    keys = list({k for k in keys_result.scalars().all() if k})
    await fetch_players_by_keys(keys, db)
    results["players"] = f"{len(keys)} player keys"

    await keeper_service.calculate_keeper_eligibility(db)
    results["keeper_eligibility"] = "recalculated"

    return {"status": "done", "season": season, "results": results}


# ---------------------------------------------------------------------------
# ADP matching report + manual link
# ---------------------------------------------------------------------------

def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


@router.get("/validation/adp-matching")
async def validation_adp_matching(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    from app.services.fantasypros_service import _canon_position, _normalize_name

    upcoming = settings.yahoo_upcoming_season
    prev = settings.yahoo_previous_season

    adp_rows = (await db.execute(
        select(AdpData).where(AdpData.season == upcoming).order_by(AdpData.adp_rank)
    )).scalars().all()

    yahoo_players = (await db.execute(select(Player))).scalars().all()
    yahoo_by_key = {p.player_key: p for p in yahoo_players}

    roster_keys = set((await db.execute(
        select(RosterSnapshot.player_key).where(RosterSnapshot.season == prev)
    )).scalars().all())

    # Build position-bucketed Yahoo lookup for candidate search
    yahoo_by_pos: dict[str, list] = {}
    for p in yahoo_players:
        pos = _canon_position(p.position)
        yahoo_by_pos.setdefault(pos, []).append(p)

    matched_yahoo_keys: set[str] = set()
    unmatched_fp: list[dict] = []

    for adp in adp_rows:
        if adp.yahoo_player_key:
            matched_yahoo_keys.add(adp.yahoo_player_key)
            continue

        pos = _canon_position(adp.position)
        if pos in ("DEF", "K"):
            continue  # ineligible positions — not worth surfacing

        norm_fp = _normalize_name(adp.player_name)
        candidates = []
        for yp in yahoo_by_pos.get(pos, []):
            ratio = _sim(norm_fp, _normalize_name(yp.name_full))
            if ratio >= 0.70:
                candidates.append({
                    "player_key": yp.player_key,
                    "yahoo_name": yp.name_full,
                    "on_roster": yp.player_key in roster_keys,
                    "ratio": round(ratio, 2),
                })
        candidates.sort(key=lambda x: (-x["on_roster"], -x["ratio"]))

        unmatched_fp.append({
            "fp_player_id": adp.fantasypros_player_id,
            "fp_name": adp.player_name,
            "position": pos,
            "adp_rank": adp.adp_rank,
            "candidates": candidates[:4],
        })

    # Yahoo non-K/DEF roster players with no ADP match
    unmatched_yahoo: list[dict] = []
    fp_by_pos: dict[str, list] = {}
    for adp in adp_rows:
        pos = _canon_position(adp.position)
        fp_by_pos.setdefault(pos, []).append(adp)

    for pk in roster_keys:
        if pk in matched_yahoo_keys:
            continue
        p = yahoo_by_key.get(pk)
        if not p or _canon_position(p.position) in ("DEF", "K", "DST", "D/ST"):
            continue
        pos = _canon_position(p.position)
        norm_y = _normalize_name(p.name_full)
        candidates = []
        for adp in fp_by_pos.get(pos, []):
            ratio = _sim(norm_y, _normalize_name(adp.player_name))
            if ratio >= 0.70:
                candidates.append({
                    "fp_player_id": adp.fantasypros_player_id,
                    "fp_name": adp.player_name,
                    "adp_rank": adp.adp_rank,
                    "ratio": round(ratio, 2),
                })
        candidates.sort(key=lambda x: -x["ratio"])
        unmatched_yahoo.append({
            "player_key": pk,
            "yahoo_name": p.name_full,
            "position": pos,
            "nfl_team": p.nfl_team,
            "candidates": candidates[:4],
        })

    unmatched_yahoo.sort(key=lambda x: x["yahoo_name"])

    return {
        "season": upcoming,
        "total_fp": len(adp_rows),
        "matched": len(matched_yahoo_keys),
        "unmatched_fp": unmatched_fp,
        "unmatched_yahoo_roster": unmatched_yahoo,
    }


class AdpManualLinkRequest(BaseModel):
    fp_player_id: int
    yahoo_player_key: str


@router.post("/adp/manual-link")
async def adp_manual_link(
    req: AdpManualLinkRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    upcoming = settings.yahoo_upcoming_season
    result = await db.execute(
        select(AdpData).where(
            AdpData.fantasypros_player_id == req.fp_player_id,
            AdpData.season == upcoming,
        )
    )
    adp = result.scalar_one_or_none()
    if not adp:
        raise HTTPException(status_code=404, detail="FP player not found in ADP data")
    adp.yahoo_player_key = req.yahoo_player_key
    await db.commit()
    await keeper_service.calculate_keeper_eligibility(db)
    return {"status": "linked"}


# ---------------------------------------------------------------------------
# Team roster view for admin
# ---------------------------------------------------------------------------

@router.get("/teams")
async def admin_teams(db: AsyncSession = Depends(get_db), _: str = Depends(_verify_token)):
    result = await db.execute(
        select(Team).where(Team.season == settings.yahoo_previous_season)
    )
    teams = result.scalars().all()
    return [
        {"team_key": t.team_key, "team_id": t.team_id, "name": t.name, "manager_name": t.manager_name}
        for t in sorted(teams, key=lambda x: x.team_id)
    ]


@router.get("/teams/{team_key:path}/roster")
async def admin_team_roster(
    team_key: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    prev = settings.yahoo_previous_season
    upcoming = settings.yahoo_upcoming_season

    roster_result = await db.execute(
        select(RosterSnapshot).where(
            RosterSnapshot.team_key == team_key,
            RosterSnapshot.season == prev,
        )
    )
    snapshots = roster_result.scalars().all()

    ft_result = await db.execute(
        select(FranchiseTag).where(
            FranchiseTag.team_key == team_key,
            FranchiseTag.season == upcoming,
            FranchiseTag.is_active == True,  # noqa: E712
        )
    )
    current_fts = {ft.player_key: ft for ft in ft_result.scalars().all()}

    players = []
    for snap in snapshots:
        p_result = await db.execute(select(Player).where(Player.player_key == snap.player_key))
        player = p_result.scalar_one_or_none()

        elig_result = await db.execute(
            select(KeeperEligibility).where(
                KeeperEligibility.player_key == snap.player_key,
                KeeperEligibility.team_key == team_key,
                KeeperEligibility.upcoming_season == upcoming,
            )
        )
        elig = elig_result.scalar_one_or_none()

        ft = current_fts.get(snap.player_key)

        players.append({
            "player_key": snap.player_key,
            "player_name": player.name_full if player else snap.player_key,
            "position": player.position if player else "UNK",
            "nfl_team": player.nfl_team if player else None,
            "keeper_eligible": elig.is_keeper_eligible if elig else False,
            "keep_round": elig.keep_round if elig else None,
            "original_draft_year": elig.original_draft_year if elig else None,
            "original_draft_round": elig.original_draft_round if elig else None,
            "franchise_eligible": elig.is_franchise_eligible if elig else False,
            "franchise_round": elig.franchise_round if elig else None,
            "adp_rank": elig.adp_rank if elig else None,
            "ineligibility_reason": elig.ineligibility_reason if elig else None,
            "franchise_ineligibility_reason": elig.franchise_ineligibility_reason if elig else None,
            "is_franchised": snap.player_key in current_fts,
            "franchise_tag_id": ft.id if ft else None,
            "franchise_notes": ft.notes if ft else None,
        })

    return sorted(players, key=lambda x: (x["position"], x["player_name"]))


# ---------------------------------------------------------------------------
# Franchise tag management
# ---------------------------------------------------------------------------

class FranchiseTagRequest(BaseModel):
    team_key: str
    player_key: str
    season: int
    notes: Optional[str] = None


@router.post("/franchise-tags")
async def add_franchise_tag(
    req: FranchiseTagRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    existing = await db.execute(
        select(FranchiseTag).where(
            FranchiseTag.team_key == req.team_key,
            FranchiseTag.player_key == req.player_key,
            FranchiseTag.season == req.season,
            FranchiseTag.is_active == True,  # noqa: E712
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Franchise tag already exists for this player")

    db.add(
        FranchiseTag(
            season=req.season,
            team_key=req.team_key,
            player_key=req.player_key,
            notes=req.notes,
            is_active=True,
        )
    )
    await db.commit()
    await keeper_service.calculate_keeper_eligibility(db)
    return {"status": "added"}


@router.delete("/franchise-tags/{tag_id}")
async def remove_franchise_tag(
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    result = await db.execute(select(FranchiseTag).where(FranchiseTag.id == tag_id))
    tag = result.scalar_one_or_none()
    if not tag:
        raise HTTPException(status_code=404, detail="Franchise tag not found")
    tag.is_active = False
    await db.commit()
    await keeper_service.calculate_keeper_eligibility(db)
    return {"status": "removed"}


# ---------------------------------------------------------------------------
# Historical franchise tag management
# ---------------------------------------------------------------------------

@router.get("/historical-keeper-picks")
async def get_historical_keeper_picks(
    season: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    """Returns all keeper picks for a season with their franchise-tag status.
    Uses previous season's week-14 rosters as source of truth — more reliable
    than the is_keeper flag, which Yahoo sometimes omits for valid keeper picks.
    """
    # Collect numeric player IDs that appeared on any week-14 roster the prior season
    prev_roster_result = await db.execute(
        select(RosterSnapshot.player_key).where(RosterSnapshot.season == season - 1)
    )
    prev_player_ids = {
        k.split(".p.")[-1]
        for k in prev_roster_result.scalars().all()
        if k and ".p." in k
    }

    # Fetch all picks for this season that have a player assigned
    picks_result = await db.execute(
        select(DraftPick)
        .where(DraftPick.season == season, DraftPick.player_key.isnot(None))
        .order_by(DraftPick.round_number, DraftPick.pick_number)
    )
    all_picks = picks_result.scalars().all()

    # Keep only picks where the player was on a prior-season roster (i.e. a keeper)
    picks = [
        p for p in all_picks
        if p.player_key and p.player_key.split(".p.")[-1] in prev_player_ids
    ]

    player_result = await db.execute(select(Player))
    players_by_key = {p.player_key: p for p in player_result.scalars().all()}

    team_result = await db.execute(select(Team).where(Team.season == season))
    teams_by_key = {t.team_key: t for t in team_result.scalars().all()}

    ft_result = await db.execute(
        select(FranchiseTag).where(
            FranchiseTag.season == season,
            FranchiseTag.is_active == True,  # noqa: E712
        )
    )
    ft_keys = {ft.player_key for ft in ft_result.scalars().all()}

    return [
        {
            "pick_number": p.pick_number,
            "round_number": p.round_number,
            "team_key": p.team_key,
            "team_name": teams_by_key[p.team_key].name if p.team_key in teams_by_key else p.team_key,
            "player_key": p.player_key,
            "player_name": players_by_key[p.player_key].name_full if p.player_key and p.player_key in players_by_key else (p.player_key or "—"),
            "position": players_by_key[p.player_key].position if p.player_key and p.player_key in players_by_key else "UNK",
            "is_franchise_tag": p.player_key in ft_keys,
        }
        for p in picks
    ]


class HistoricalFranchiseTagRequest(BaseModel):
    season: int
    player_key: str
    team_key: str


@router.post("/historical-franchise-tags")
async def set_historical_franchise_tag(
    req: HistoricalFranchiseTagRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    existing = await db.execute(
        select(FranchiseTag).where(
            FranchiseTag.season == req.season,
            FranchiseTag.player_key == req.player_key,
        )
    )
    row = existing.scalar_one_or_none()
    if row:
        row.is_active = True
        row.team_key = req.team_key
    else:
        db.add(FranchiseTag(season=req.season, team_key=req.team_key, player_key=req.player_key, is_active=True))
    await db.commit()
    await keeper_service.calculate_keeper_eligibility(db)
    return {"status": "marked"}


@router.delete("/historical-franchise-tags/{season}/{player_key:path}")
async def unset_historical_franchise_tag(
    season: int,
    player_key: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    result = await db.execute(
        select(FranchiseTag).where(
            FranchiseTag.season == season,
            FranchiseTag.player_key == player_key,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.is_active = False
        await db.commit()
        await keeper_service.calculate_keeper_eligibility(db)
    return {"status": "unmarked"}


# ---------------------------------------------------------------------------
# Historical keeper override import
# ---------------------------------------------------------------------------

class KeeperOverrideItem(BaseModel):
    season: int
    player_key: str
    team_key: str
    original_draft_year: int
    original_draft_round: int
    was_franchise_tagged: bool = False
    notes: Optional[str] = None


@router.post("/historical-keepers/import")
async def import_historical_keepers(
    items: list[KeeperOverrideItem],
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    added = 0
    for item in items:
        existing = await db.execute(
            select(HistoricalKeeperOverride).where(
                HistoricalKeeperOverride.season == item.season,
                HistoricalKeeperOverride.player_key == item.player_key,
            )
        )
        if existing.scalar_one_or_none():
            continue
        db.add(
            HistoricalKeeperOverride(
                season=item.season,
                player_key=item.player_key,
                team_key=item.team_key,
                original_draft_year=item.original_draft_year,
                original_draft_round=item.original_draft_round,
                was_franchise_tagged=item.was_franchise_tagged,
                notes=item.notes,
            )
        )
        added += 1

    await db.commit()
    await keeper_service.calculate_keeper_eligibility(db)
    return {"status": "imported", "added": added}


@router.get("/historical-keepers")
async def list_historical_keepers(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    result = await db.execute(
        select(HistoricalKeeperOverride).order_by(
            HistoricalKeeperOverride.season.desc(),
            HistoricalKeeperOverride.original_draft_year.asc(),
        )
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "season": r.season,
            "player_key": r.player_key,
            "team_key": r.team_key,
            "original_draft_year": r.original_draft_year,
            "original_draft_round": r.original_draft_round,
            "was_franchise_tagged": r.was_franchise_tagged,
            "notes": r.notes,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Draft order management
# ---------------------------------------------------------------------------

class DraftOrderItem(BaseModel):
    team_key: str
    draft_position: int


@router.get("/draft-order")
async def get_draft_order(db: AsyncSession = Depends(get_db), _: str = Depends(_verify_token)):
    upcoming = settings.yahoo_upcoming_season
    result = await db.execute(
        select(DraftOrder)
        .where(DraftOrder.season == upcoming)
        .order_by(DraftOrder.draft_position)
    )
    rows = result.scalars().all()
    team_result = await db.execute(select(Team).where(Team.season == settings.yahoo_previous_season))
    teams_by_key = {t.team_key: t for t in team_result.scalars().all()}
    return [
        {
            "id": row.id,
            "draft_position": row.draft_position,
            "team_key": row.team_key,
            "team_name": teams_by_key[row.team_key].name if row.team_key in teams_by_key else row.team_key,
        }
        for row in rows
    ]


@router.post("/draft-order")
async def set_draft_order(
    items: list[DraftOrderItem],
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    upcoming = settings.yahoo_upcoming_season
    await db.execute(delete(DraftOrder).where(DraftOrder.season == upcoming))
    for item in items:
        db.add(DraftOrder(season=upcoming, draft_position=item.draft_position, team_key=item.team_key))
    await db.commit()
    return {"status": "saved", "count": len(items)}


# ---------------------------------------------------------------------------
# Pick ownership (trade tracker)
# ---------------------------------------------------------------------------

class PickOwnershipRequest(BaseModel):
    original_team_key: str
    round_number: int
    current_owner_team_key: str
    note: Optional[str] = None


@router.get("/pick-ownership")
async def get_pick_ownership(db: AsyncSession = Depends(get_db), _: str = Depends(_verify_token)):
    upcoming = settings.yahoo_upcoming_season
    result = await db.execute(select(PickOwnership).where(PickOwnership.season == upcoming))
    rows = result.scalars().all()
    team_result = await db.execute(select(Team).where(Team.season == settings.yahoo_previous_season))
    teams_by_key = {t.team_key: t for t in team_result.scalars().all()}
    return [
        {
            "id": row.id,
            "original_team_key": row.original_team_key,
            "original_team_name": teams_by_key[row.original_team_key].name if row.original_team_key in teams_by_key else row.original_team_key,
            "round_number": row.round_number,
            "current_owner_team_key": row.current_owner_team_key,
            "current_owner_name": teams_by_key[row.current_owner_team_key].name if row.current_owner_team_key in teams_by_key else row.current_owner_team_key,
            "note": row.note,
        }
        for row in rows
    ]


@router.post("/pick-ownership")
async def set_pick_ownership(
    req: PickOwnershipRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    upcoming = settings.yahoo_upcoming_season
    existing = await db.execute(
        select(PickOwnership).where(
            PickOwnership.season == upcoming,
            PickOwnership.original_team_key == req.original_team_key,
            PickOwnership.round_number == req.round_number,
        )
    )
    row = existing.scalar_one_or_none()
    if row:
        row.current_owner_team_key = req.current_owner_team_key
        row.note = req.note
    else:
        db.add(PickOwnership(
            season=upcoming,
            original_team_key=req.original_team_key,
            round_number=req.round_number,
            current_owner_team_key=req.current_owner_team_key,
            note=req.note,
        ))
    await db.commit()
    return {"status": "saved"}


@router.delete("/pick-ownership/{row_id}")
async def delete_pick_ownership(
    row_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    result = await db.execute(select(PickOwnership).where(PickOwnership.id == row_id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    await db.delete(row)
    await db.commit()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Data validation — browse raw season data
# ---------------------------------------------------------------------------

@router.get("/validation/draft-picks")
async def validation_draft_picks(
    season: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    result = await db.execute(
        select(DraftPick).where(DraftPick.season == season).order_by(DraftPick.pick_number)
    )
    picks = result.scalars().all()
    player_result = await db.execute(select(Player))
    players_by_key = {p.player_key: p for p in player_result.scalars().all()}
    team_result = await db.execute(select(Team).where(Team.season == season))
    teams_by_key = {t.team_key: t for t in team_result.scalars().all()}
    return [
        {
            "pick_number": p.pick_number,
            "round_number": p.round_number,
            "team_key": p.team_key,
            "team_name": teams_by_key[p.team_key].name if p.team_key in teams_by_key else p.team_key,
            "player_key": p.player_key,
            "player_name": players_by_key[p.player_key].name_full if p.player_key and p.player_key in players_by_key else (p.player_key or "—"),
            "is_keeper": p.is_keeper,
            "keeper_cost_round": p.keeper_cost_round,
        }
        for p in picks
    ]


@router.get("/validation/transactions")
async def validation_transactions(
    season: int,
    txn_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    import json as _json

    query = select(Transaction).where(Transaction.season == season)
    if txn_type:
        query = query.where(Transaction.transaction_type == txn_type)
    query = query.order_by(Transaction.timestamp.desc())
    result = await db.execute(query)
    txns = result.scalars().all()

    def _parse_details(details_json: str) -> tuple[list[dict], list[dict]]:
        """Returns (players, picks) extracted from stored transaction JSON."""
        try:
            raw = _json.loads(details_json)
        except Exception:
            return [], []

        txn_info = raw[0] if isinstance(raw, list) else raw

        # Players are in raw[1]["players"] as a dict with "count" + numeric string keys
        players_dict: dict = {}
        if isinstance(raw, list) and len(raw) > 1 and isinstance(raw[1], dict):
            players_dict = raw[1].get("players", {})
        if not players_dict:
            players_dict = txn_info.get("players", {})

        result_players = []
        for j in range(int(players_dict.get("count", 0)) if isinstance(players_dict, dict) else 0):
            p_raw = players_dict.get(str(j), {}).get("player", [])
            p_info: dict = {}
            txn_data: dict = {}
            for chunk in p_raw:
                if isinstance(chunk, list):
                    for item in chunk:
                        if isinstance(item, dict):
                            p_info.update(item)
                elif isinstance(chunk, dict):
                    if "transaction_data" in chunk:
                        td = chunk["transaction_data"]
                        txn_data = td[0] if isinstance(td, list) and td else (td if isinstance(td, dict) else {})
                    else:
                        p_info.update(chunk)
            name_info = p_info.get("name", {})
            result_players.append({
                "player_name": name_info.get("full") or p_info.get("player_key", "?"),
                "source_team_key": txn_data.get("source_team_key"),
                "destination_team_key": txn_data.get("destination_team_key"),
                "move_type": txn_data.get("type"),
            })

        # Picks are in raw[0]["picks"] as a plain list of {"pick": {...}}
        picks_list = txn_info.get("picks", [])
        result_picks = []
        if isinstance(picks_list, list):
            for item in picks_list:
                pk = item.get("pick", {}) if isinstance(item, dict) else {}
                if pk:
                    result_picks.append({
                        "season": pk.get("season"),
                        "round": pk.get("round"),
                        "original_team_key": pk.get("original_team_key"),
                        "source_team_key": pk.get("source_team_key") or pk.get("original_team_key"),
                        "destination_team_key": pk.get("destination_team_key"),
                    })

        return result_players, result_picks

    # Load team names for key→name lookup
    team_result = await db.execute(select(Team).where(Team.season == season))
    teams_by_key = {t.team_key: t.name for t in team_result.scalars().all()}

    def _resolve(key: str | None) -> str:
        return teams_by_key.get(key or "", key or "—")

    out = []
    for t in txns:
        players, picks = _parse_details(t.details_json)
        for p in players:
            p["source_team"] = _resolve(p["source_team_key"])
            p["destination_team"] = _resolve(p["destination_team_key"])
        for pk in picks:
            pk["source_team"] = _resolve(pk["source_team_key"])
            pk["destination_team"] = _resolve(pk["destination_team_key"])
            pk["original_team"] = _resolve(pk["original_team_key"])
        out.append({
            "id": t.id,
            "transaction_key": t.transaction_key,
            "transaction_type": t.transaction_type,
            "timestamp": t.timestamp.isoformat() if t.timestamp else None,
            "players": players,
            "picks": picks,
        })
    return out


@router.get("/validation/transaction-raw/{txn_key:path}")
async def transaction_raw(
    txn_key: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    import json as _json
    result = await db.execute(select(Transaction).where(Transaction.transaction_key == txn_key))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return _json.loads(t.details_json)
    except Exception:
        return {"raw": t.details_json}


@router.get("/validation/rosters")
async def validation_rosters(
    season: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(_verify_token),
):
    result = await db.execute(select(RosterSnapshot).where(RosterSnapshot.season == season))
    snapshots = result.scalars().all()
    player_result = await db.execute(select(Player))
    players_by_key = {p.player_key: p for p in player_result.scalars().all()}
    team_result = await db.execute(select(Team).where(Team.season == season))
    teams_by_key = {t.team_key: t for t in team_result.scalars().all()}

    by_team: dict[str, list] = {}
    for snap in snapshots:
        tname = teams_by_key[snap.team_key].name if snap.team_key in teams_by_key else snap.team_key
        if tname not in by_team:
            by_team[tname] = []
        player = players_by_key.get(snap.player_key)
        by_team[tname].append({
            "player_key": snap.player_key,
            "player_name": player.name_full if player else snap.player_key,
            "position": player.position if player else "UNK",
            "nfl_team": player.nfl_team if player else None,
            "roster_position": snap.roster_position,
            "acquisition_type": snap.acquisition_type,
        })

    return by_team

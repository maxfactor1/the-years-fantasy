from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import DraftOrder, KeeperEligibility, PickOwnership, Player, RosterSnapshot, Team
from app.services import keeper_service
from app.services.etl_service import get_app_status

router = APIRouter()
settings = get_settings()

# Distinct palette — one color per team slot (up to 14)
TEAM_COLORS = [
    "#ef4444",  # red
    "#f97316",  # orange
    "#eab308",  # yellow
    "#84cc16",  # lime
    "#22c55e",  # green
    "#14b8a6",  # teal
    "#06b6d4",  # cyan
    "#38bdf8",  # sky
    "#6366f1",  # indigo
    "#a855f7",  # purple
    "#ec4899",  # pink
    "#f43f5e",  # rose
    "#fb923c",  # amber-orange
    "#4ade80",  # light green
]


@router.get("/status")
async def app_status(db: AsyncSession = Depends(get_db)):
    return await get_app_status(db)


@router.get("/teams")
async def list_teams(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Team).where(Team.season == settings.yahoo_previous_season)
    )
    teams = result.scalars().all()
    return [
        {
            "team_key": t.team_key,
            "team_id": t.team_id,
            "name": t.name,
            "manager_name": t.manager_name,
            "logo_url": t.logo_url,
        }
        for t in sorted(teams, key=lambda x: x.team_id)
    ]


@router.get("/teams/{team_key:path}/keepers")
async def team_keepers(team_key: str, db: AsyncSession = Depends(get_db)):
    summary = await keeper_service.get_team_keeper_summary(team_key, db)
    if not summary:
        raise HTTPException(status_code=404, detail="Team not found")
    return summary


@router.get("/draft-board")
async def draft_board(db: AsyncSession = Depends(get_db)):
    """
    Returns the pick-ownership draft board for the upcoming season.
    Each cell represents one pick slot (original_team × round) and shows
    who currently owns it — color-coded by owning team.
    """
    upcoming = settings.yahoo_upcoming_season
    prev = settings.yahoo_previous_season
    rounds = settings.league_draft_rounds
    num_teams = settings.league_team_count

    # Load draft order (manual, set by admin)
    order_result = await db.execute(
        select(DraftOrder)
        .where(DraftOrder.season == upcoming)
        .order_by(DraftOrder.draft_position)
    )
    draft_order_rows = order_result.scalars().all()

    # Load teams from previous season for names/logos
    team_result = await db.execute(select(Team).where(Team.season == prev))
    teams_by_key: dict = {t.team_key: t for t in team_result.scalars().all()}

    # Build ordered team list — from draft order if set, else alphabetical fallback
    if draft_order_rows:
        ordered_teams = []
        for row in draft_order_rows:
            t = teams_by_key.get(row.team_key)
            ordered_teams.append({
                "team_key": row.team_key,
                "name": t.name if t else row.team_key,
                "manager_name": t.manager_name if t else None,
                "draft_position": row.draft_position,
            })
    else:
        ordered_teams = [
            {
                "team_key": t.team_key,
                "name": t.name,
                "manager_name": t.manager_name,
                "draft_position": i + 1,
            }
            for i, t in enumerate(sorted(teams_by_key.values(), key=lambda x: x.team_id))
        ]

    # Assign a stable color to each team by draft position
    color_map: dict[str, str] = {
        t["team_key"]: TEAM_COLORS[i % len(TEAM_COLORS)]
        for i, t in enumerate(ordered_teams)
    }

    # Load pick ownership overrides
    ownership_result = await db.execute(
        select(PickOwnership).where(PickOwnership.season == upcoming)
    )
    # (original_team_key, round) → current_owner_team_key
    ownership_map: dict[tuple, str] = {
        (row.original_team_key, row.round_number): row.current_owner_team_key
        for row in ownership_result.scalars().all()
    }

    # Build the board: list of rounds, each containing pick slots in snake order
    board_rounds = []
    for rnd in range(1, rounds + 1):
        # Snake: odd rounds go forward, even rounds go backward
        slots_in_order = ordered_teams if rnd % 2 == 1 else list(reversed(ordered_teams))
        picks = []
        for slot_idx, orig_team in enumerate(slots_in_order):
            overall_pick = (rnd - 1) * num_teams + slot_idx + 1
            orig_key = orig_team["team_key"]
            owner_key = ownership_map.get((orig_key, rnd), orig_key)
            owner_team = teams_by_key.get(owner_key)
            is_traded = owner_key != orig_key

            picks.append({
                "overall_pick": overall_pick,
                "pick_in_round": slot_idx + 1,
                "original_team_key": orig_key,
                "original_team_name": orig_team["name"],
                "owner_team_key": owner_key,
                "owner_team_name": owner_team.name if owner_team else owner_key,
                "is_traded": is_traded,
                "color": color_map.get(owner_key, "#6c63ff"),
                "original_color": color_map.get(orig_key, "#6c63ff"),
            })
        board_rounds.append({"round": rnd, "picks": picks})

    return {
        "upcoming_season": upcoming,
        "teams": ordered_teams,
        "color_map": color_map,
        "rounds": board_rounds,
        "draft_order_set": bool(draft_order_rows),
    }


@router.get("/players")
async def list_players(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Player).order_by(Player.name_last))
    players = result.scalars().all()
    return [
        {
            "player_key": p.player_key,
            "name_full": p.name_full,
            "position": p.position,
            "nfl_team": p.nfl_team,
        }
        for p in players
    ]


@router.get("/teams/{team_key:path}/roster")
async def team_roster(team_key: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(RosterSnapshot).where(
            RosterSnapshot.team_key == team_key,
            RosterSnapshot.season == settings.yahoo_previous_season,
        )
    )
    snapshots = result.scalars().all()

    roster = []
    for snap in snapshots:
        p_result = await db.execute(select(Player).where(Player.player_key == snap.player_key))
        player = p_result.scalar_one_or_none()
        elig_result = await db.execute(
            select(KeeperEligibility).where(
                KeeperEligibility.player_key == snap.player_key,
                KeeperEligibility.team_key == team_key,
                KeeperEligibility.upcoming_season == settings.yahoo_upcoming_season,
            )
        )
        elig = elig_result.scalar_one_or_none()

        roster.append({
            "player_key": snap.player_key,
            "player_name": player.name_full if player else snap.player_key,
            "position": player.position if player else "UNK",
            "nfl_team": player.nfl_team if player else None,
            "roster_position": snap.roster_position,
            "acquisition_type": snap.acquisition_type,
            "keeper_eligible": elig.is_keeper_eligible if elig else False,
            "keep_round": elig.keep_round if elig else None,
            "franchise_eligible": elig.is_franchise_eligible if elig else False,
            "franchise_round": elig.franchise_round if elig else None,
            "adp_rank": elig.adp_rank if elig else None,
        })

    return sorted(roster, key=lambda x: (x["position"], x["player_name"]))

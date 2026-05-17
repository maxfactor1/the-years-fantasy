"""
Keeper eligibility calculation engine.

Rules summary (2026 draft):
  Regular keepers
  ----------------
  - Max 3 per team
  - Only drafted players (drafted picks in any season 2022-2025, including those
    later dropped and re-picked up — original draft year is preserved)
  - Round 1 original picks are NEVER eligible
  - Keep round = original_draft_round - years_since_original_draft
  - Keep round must be >= 2 (can't cost a 1st-round pick)
  - Max 4 years of keeping (5 total seasons on roster since original draft)
  - Kickers (K) and Defenses (DEF) ineligible

  Franchise tags
  ---------------
  - Max 4 per team (each extra beyond 1 costs a regular keeper slot)
  - Any player (drafted OR undrafted) on the week-14 roster
  - Cannot re-franchise a player who was franchise-tagged last season
    UNLESS they were dropped in-season (loses designation)
  - Round is determined by FantasyPros ADP rank using the table below:
      ADP 1-8   → Round 3
      ADP 9-16  → Round 4
      ADP 17-24 → Round 5
      ADP 25-36 → Round 6
      ADP 37-48 → Round 7
      ADP 49-60 → Round 10
      ADP 61-72 → Round 12
      ADP 73-84 → Round 14
      ADP 85+   → Round 16
  - Kickers (K) and Defenses (DEF) ineligible

  Carry-over cap
  ---------------
  - Max 4 total players carried over (keepers + franchise tags combined)
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    AdpData,
    DraftPick,
    FranchiseTag,
    HistoricalKeeperOverride,
    KeeperEligibility,
    Player,
    RosterSnapshot,
    Team,
)

logger = logging.getLogger(__name__)
settings = get_settings()

INELIGIBLE_POSITIONS = {"K", "DEF", "D/ST", "DST", "D"}

# ADP rank → franchise tag round
_ADP_ROUND_TABLE: list[tuple[int, int]] = [
    (8,  3),
    (16, 4),
    (24, 5),
    (36, 6),
    (48, 7),
    (60, 10),
    (72, 12),
    (84, 14),
]
_ADP_ROUND_DEFAULT = 16


def adp_rank_to_franchise_round(adp_rank: int) -> int:
    for threshold, rd in _ADP_ROUND_TABLE:
        if adp_rank <= threshold:
            return rd
    return _ADP_ROUND_DEFAULT


def keeper_round(original_draft_round: int, original_draft_year: int, upcoming_season: int) -> Optional[int]:
    """
    Returns the round this player would cost as a regular keeper in upcoming_season,
    or None if ineligible.
    """
    years_since = upcoming_season - original_draft_year

    if years_since > settings.max_keeper_years:
        return None  # exceeded 5-year total roster limit

    if original_draft_round == 1:
        return None  # 1st-round picks never keepable

    cost = original_draft_round - years_since
    if cost <= 1:
        return None  # would cost a 1st-round pick or less

    return cost


# ---------------------------------------------------------------------------
# Original draft resolution
# ---------------------------------------------------------------------------

async def _get_original_draft(player_key: str, db: AsyncSession) -> Optional[tuple[int, int]]:
    """
    Returns (original_draft_year, original_draft_round) for a player,
    checking historical overrides first, then Yahoo draft data.
    Returns None if player was never drafted in this league.

    Yahoo's is_keeper flag is unreliable (often 0 even for keeper picks) and
    player_key prefixes change each season (e.g. 449.p.40993 → 461.p.40993).
    We normalise by matching on the numeric player ID suffix and take the
    earliest draft pick across all seasons — that's always the original draft.
    """
    # 1. Check manual overrides
    result = await db.execute(
        select(HistoricalKeeperOverride)
        .where(HistoricalKeeperOverride.player_key == player_key)
        .order_by(HistoricalKeeperOverride.original_draft_year.asc())
    )
    overrides = result.scalars().all()
    if overrides:
        earliest = overrides[0]
        return earliest.original_draft_year, earliest.original_draft_round

    # 2. Normalise player key to numeric suffix ("461.p.40993" → "40993")
    #    so we match picks from any season regardless of game-key prefix.
    #    Yahoo's is_keeper flag is unreliable (often always 0), so we infer
    #    whether each pick is a keeper by checking if its round matches the
    #    expected keeper cost given the current origin.  If it doesn't match,
    #    the player was dropped and re-drafted fresh — reset the clock.
    player_id_str = player_key.split(".p.")[-1]

    result = await db.execute(
        select(DraftPick)
        .where(DraftPick.player_key.like(f"%.p.{player_id_str}"))
        .order_by(DraftPick.season.asc(), DraftPick.round_number.asc())
    )
    picks = result.scalars().all()

    if not picks:
        return None  # truly undrafted in this league

    orig_year, orig_round = picks[0].season, picks[0].round_number

    for pick in picks[1:]:
        if not pick.is_keeper:
            # Not flagged as a keeper pick — player was released and re-drafted fresh
            orig_year, orig_round = pick.season, pick.round_number

    return orig_year, orig_round


async def _was_franchise_tagged_last_season(player_key: str, db: AsyncSession) -> bool:
    """Returns True if this player held a STILL-ACTIVE franchise tag last season."""
    prev_season = settings.yahoo_upcoming_season - 1
    result = await db.execute(
        select(FranchiseTag).where(
            FranchiseTag.player_key == player_key,
            FranchiseTag.season == prev_season,
            FranchiseTag.is_active == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Main calculation
# ---------------------------------------------------------------------------

async def calculate_keeper_eligibility(db: AsyncSession) -> None:
    """
    Recalculates KeeperEligibility for ALL teams for the upcoming season.
    Overwrites existing rows. Called after each ADP refresh and after admin changes.
    """
    upcoming = settings.yahoo_upcoming_season
    prev = settings.yahoo_previous_season

    # Clear existing calculations for upcoming season
    from sqlalchemy import delete
    await db.execute(delete(KeeperEligibility).where(KeeperEligibility.upcoming_season == upcoming))
    await db.commit()

    # Get all teams for previous season
    team_result = await db.execute(select(Team).where(Team.season == prev))
    teams = team_result.scalars().all()

    for team in teams:
        await _calculate_for_team(team, upcoming, prev, db)

    await db.commit()
    logger.info(f"Keeper eligibility calculated for all teams ({upcoming} season)")


async def _calculate_for_team(team: Team, upcoming: int, prev: int, db: AsyncSession) -> None:
    # Get all players on this team's week-14 roster
    roster_result = await db.execute(
        select(RosterSnapshot).where(
            RosterSnapshot.season == prev,
            RosterSnapshot.team_key == team.team_key,
        )
    )
    roster = roster_result.scalars().all()

    for snapshot in roster:
        player_key = snapshot.player_key

        # Fetch player info
        p_result = await db.execute(select(Player).where(Player.player_key == player_key))
        player: Optional[Player] = p_result.scalar_one_or_none()

        position = player.position.upper() if player else "UNK"
        player_name = player.name_full if player else player_key

        # Fetch ADP data
        adp: Optional[AdpData] = None
        adp_result = await db.execute(
            select(AdpData).where(
                AdpData.yahoo_player_key == player_key,
                AdpData.season == upcoming,
            )
        )
        adp = adp_result.scalar_one_or_none()

        adp_rank = adp.adp_rank if adp else None
        adp_value = adp.adp_value if adp else None

        # ---------------------------------------------------------------
        # Regular keeper eligibility
        # ---------------------------------------------------------------
        is_keeper_eligible = False
        keep_rd = None
        orig_year = None
        orig_round = None
        years_since = None
        inelig_reason = None

        was_ft_last_season = await _was_franchise_tagged_last_season(player_key, db)

        if position in INELIGIBLE_POSITIONS:
            inelig_reason = f"{position} is not keepable"
        elif was_ft_last_season:
            inelig_reason = "Was franchise tagged last season — cannot keep"
        else:
            draft_info = await _get_original_draft(player_key, db)
            if draft_info is None:
                inelig_reason = "Undrafted — use Franchise Tag if eligible"
            else:
                orig_year, orig_round = draft_info
                years_since = upcoming - orig_year

                if orig_round == 1:
                    inelig_reason = "Originally a 1st-round pick — never keepable"
                elif years_since > settings.max_keeper_years:
                    inelig_reason = f"Exceeded 5-year roster maximum ({orig_year} draft)"
                else:
                    rd = keeper_round(orig_round, orig_year, upcoming)
                    if rd is None:
                        inelig_reason = f"Keep would cost a 1st-round pick (drafted Rd {orig_round} in {orig_year})"
                    else:
                        keep_rd = rd
                        is_keeper_eligible = True

        # ---------------------------------------------------------------
        # Franchise tag eligibility
        # ---------------------------------------------------------------
        is_franchise_eligible = False
        franchise_rd = None
        franchise_inelig_reason = None

        _keep_cost_too_high = (
            inelig_reason is not None
            and inelig_reason.startswith("Keep would cost a 1st-round pick")
        )

        if position in INELIGIBLE_POSITIONS:
            franchise_inelig_reason = f"{position} cannot be franchise tagged"
        elif was_ft_last_season:
            franchise_inelig_reason = "Was franchise tagged last season — cannot re-franchise"
        elif orig_round == 1:
            franchise_inelig_reason = "Originally a 1st-round pick — cannot franchise tag"
        elif _keep_cost_too_high:
            franchise_inelig_reason = "Keep would cost a 1st-round pick — cannot franchise tag"
        elif adp_rank is None:
            franchise_inelig_reason = "No ADP data — cannot determine franchise round"
        else:
            franchise_rd = adp_rank_to_franchise_round(adp_rank)
            is_franchise_eligible = True

        db.add(
            KeeperEligibility(
                upcoming_season=upcoming,
                team_key=team.team_key,
                player_key=player_key,
                is_keeper_eligible=is_keeper_eligible,
                keep_round=keep_rd,
                original_draft_year=orig_year,
                original_draft_round=orig_round,
                years_since_draft=years_since,
                ineligibility_reason=inelig_reason,
                is_franchise_eligible=is_franchise_eligible,
                franchise_round=franchise_rd,
                franchise_ineligibility_reason=franchise_inelig_reason,
                adp_rank=adp_rank,
                adp_value=adp_value,
                updated_at=datetime.utcnow(),
            )
        )


# ---------------------------------------------------------------------------
# Team-level summary helpers (called by API routes)
# ---------------------------------------------------------------------------

async def get_team_keeper_summary(team_key: str, db: AsyncSession) -> dict:
    upcoming = settings.yahoo_upcoming_season

    team_result = await db.execute(
        select(Team).where(Team.team_key == team_key, Team.season == settings.yahoo_previous_season)
    )
    team = team_result.scalar_one_or_none()

    eligibility_result = await db.execute(
        select(KeeperEligibility).where(
            KeeperEligibility.team_key == team_key,
            KeeperEligibility.upcoming_season == upcoming,
        )
    )
    all_rows = eligibility_result.scalars().all()

    # times_kept is derived from years_since_draft rather than is_keeper flags
    # because Yahoo's is_keeper field is unreliable (often always 0).

    # Load franchise tags set by admin for upcoming season
    ft_result = await db.execute(
        select(FranchiseTag).where(
            FranchiseTag.team_key == team_key,
            FranchiseTag.season == upcoming,
            FranchiseTag.is_active == True,  # noqa: E712
        )
    )
    franchise_tags = ft_result.scalars().all()
    franchised_keys = {ft.player_key for ft in franchise_tags}

    eligible_keepers = []
    franchise_eligible = []
    ineligible = []

    for row in all_rows:
        p_result = await db.execute(select(Player).where(Player.player_key == row.player_key))
        player = p_result.scalar_one_or_none()

        entry = {
            "player_key": row.player_key,
            "player_name": player.name_full if player else row.player_key,
            "position": player.position if player else "UNK",
            "nfl_team": player.nfl_team if player else None,
            "adp_rank": row.adp_rank,
            "adp_value": row.adp_value,
            "is_franchised": row.player_key in franchised_keys,
            "times_kept": max(0, (row.years_since_draft or 1) - 1),
        }

        is_franchised = row.player_key in franchised_keys

        if row.is_keeper_eligible:
            eligible_keepers.append({
                **entry,
                "keep_round": row.keep_round,
                "original_draft_year": row.original_draft_year,
                "original_draft_round": row.original_draft_round,
                "years_since_draft": row.years_since_draft,
                "franchise_round": row.franchise_round,
            })

        # Franchise section: FT-eligible non-keepers (undrafted, 1st-round picks,
        # 5yr-exceeded) OR keeper-eligible players who have been tagged by admin.
        if row.is_franchise_eligible and (not row.is_keeper_eligible or is_franchised):
            franchise_eligible.append({
                **entry,
                "franchise_round": row.franchise_round,
            })

        # Truly ineligible: not keeper-eligible AND not FT-eligible.
        if not row.is_keeper_eligible and not row.is_franchise_eligible:
            ineligible.append({
                **entry,
                "keeper_reason": row.ineligibility_reason,
                "franchise_reason": row.franchise_ineligibility_reason,
            })

    # Sort: keepers by round asc, franchise by round asc, ADP as tiebreaker
    eligible_keepers.sort(key=lambda x: (x["keep_round"] or 99, x["adp_rank"] or 999))
    franchise_eligible.sort(key=lambda x: (x["franchise_round"] or 99, x["adp_rank"] or 999))

    num_ft = len(franchised_keys)
    ft_penalty = max(0, num_ft - 1)
    available_keeper_slots = settings.max_regular_keepers - ft_penalty

    return {
        "team_key": team_key,
        "team_name": team.name if team else team_key,
        "manager_name": team.manager_name if team else None,
        "logo_url": team.logo_url if team else None,
        "upcoming_season": upcoming,
        "eligible_keepers": eligible_keepers,
        "franchise_eligible": franchise_eligible,
        "ineligible": ineligible,
        "franchise_tags_set": [ft.player_key for ft in franchise_tags],
        "num_franchise_tags": num_ft,
        "ft_keeper_penalty": ft_penalty,
        "available_keeper_slots": available_keeper_slots,
        "max_carryovers": settings.max_total_carryovers,
    }


async def get_draft_board(db: AsyncSession) -> dict:
    """
    Returns the full draft board: for each (team, round) cell, what keeper/FT
    is occupying it, if any.
    """
    upcoming = settings.yahoo_upcoming_season
    prev = settings.yahoo_previous_season

    team_result = await db.execute(select(Team).where(Team.season == prev))
    teams = sorted(team_result.scalars().all(), key=lambda t: t.team_id)

    # Load all franchise tags
    ft_result = await db.execute(
        select(FranchiseTag).where(
            FranchiseTag.season == upcoming,
            FranchiseTag.is_active == True,  # noqa: E712
        )
    )
    franchise_tags = ft_result.scalars().all()
    ft_map: dict[str, FranchiseTag] = {ft.player_key: ft for ft in franchise_tags}

    # Load all eligibility
    elig_result = await db.execute(
        select(KeeperEligibility).where(KeeperEligibility.upcoming_season == upcoming)
    )
    all_elig = elig_result.scalars().all()

    # Build per-team cell map: team_key -> {round -> player_entry}
    board: dict[str, dict] = {}
    for team in teams:
        board[team.team_key] = {
            "team_name": team.name,
            "manager_name": team.manager_name,
            "team_id": team.team_id,
            "logo_url": team.logo_url,
            "cells": {},  # round_number -> cell_data
        }

    for row in all_elig:
        tkey = row.team_key
        if tkey not in board:
            continue

        p_result = await db.execute(select(Player).where(Player.player_key == row.player_key))
        player = p_result.scalar_one_or_none()
        player_name = player.name_full if player else row.player_key
        position = player.position if player else "UNK"

        is_ft = row.player_key in ft_map

        if is_ft and row.is_franchise_eligible and row.franchise_round:
            board[tkey]["cells"][row.franchise_round] = {
                "player_key": row.player_key,
                "player_name": player_name,
                "position": position,
                "type": "franchise_tag",
                "adp_rank": row.adp_rank,
            }
        elif row.is_keeper_eligible and row.keep_round and not is_ft:
            # Only show on board if the owner hasn't marked them as FT
            round_key = row.keep_round
            # Don't overwrite a franchise tag cell
            if round_key not in board[tkey]["cells"]:
                board[tkey]["cells"][round_key] = {
                    "player_key": row.player_key,
                    "player_name": player_name,
                    "position": position,
                    "type": "keeper",
                    "adp_rank": row.adp_rank,
                }

    return {
        "upcoming_season": upcoming,
        "teams": list(board.values()),
        "rounds": list(range(1, settings.league_draft_rounds + 1)),
    }

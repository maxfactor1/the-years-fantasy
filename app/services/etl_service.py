"""
ETL orchestration.

run_initial_etl()  — one-time full load of Yahoo data + initial ADP pull
run_hourly_refresh() — lightweight job: refresh ADP, recalculate keepers, update timestamp
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import AppSetting, PickOwnership, Transaction
from app.services import fantasypros_service, keeper_service, yahoo_service
from app.services.yahoo_service import YahooAccessError

logger = logging.getLogger(__name__)
settings = get_settings()

ETL_SEASONS = list(range(
    settings.yahoo_upcoming_season - settings.max_keeper_years,
    settings.yahoo_upcoming_season,
))  # e.g. [2022, 2023, 2024, 2025]


async def _set_setting(key: str, value: str, db: AsyncSession) -> None:
    existing = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = existing.scalar_one_or_none()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=key, value=value))
    await db.commit()


async def get_setting(key: str, db: AsyncSession) -> str | None:
    result = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else None


async def run_initial_etl(db: AsyncSession) -> dict:
    """
    Full first-time ETL. Fetches:
      - Teams for each relevant season
      - Draft picks for each relevant season
      - Transactions for each relevant season
      - Week-14 rosters for the previous season
      - FantasyPros ADP
    Then calculates keeper eligibility.
    """
    await _set_setting("etl_status", "running", db)
    results: dict = {}

    try:
        skipped_seasons = []
        for season in ETL_SEASONS:
            logger.info(f"ETL: processing season {season}")
            try:
                await yahoo_service.fetch_and_store_teams(season, db)
                await yahoo_service.fetch_and_store_draft_results(season, db)
                await yahoo_service.fetch_and_store_transactions(season, db)
                results[f"season_{season}"] = "done"
            except YahooAccessError as e:
                logger.warning(f"Season {season} skipped — {e}")
                results[f"season_{season}"] = "skipped (league not accessible for this season)"
                skipped_seasons.append(season)

        if skipped_seasons:
            results["skipped_seasons"] = skipped_seasons
            results["note"] = (
                "Some seasons were skipped because the league didn't exist or the token lacks access. "
                "Use the Historical Keeper Import in the admin panel to manually add keeper records for those years."
            )

        # Week-14 roster only needs the previous (most recent) season
        prev = settings.yahoo_previous_season
        try:
            count = await yahoo_service.fetch_and_store_week14_rosters(prev, db, week=14)
            results["week14_rosters"] = f"done ({count} player-roster entries stored)"
        except YahooAccessError as e:
            logger.warning(f"Week-14 roster fetch failed: {e}")
            results["week14_rosters"] = f"failed: {e}"

        # Collect all player_keys encountered in draft picks and rosters and
        # bulk-fetch their metadata from Yahoo
        from app.models import DraftPick, RosterSnapshot
        from sqlalchemy import select as sa_select

        draft_keys_result = await db.execute(
            sa_select(DraftPick.player_key).where(DraftPick.player_key.isnot(None))
        )
        roster_keys_result = await db.execute(sa_select(RosterSnapshot.player_key))

        all_keys = list(
            {k for k in draft_keys_result.scalars().all() if k}
            | {k for k in roster_keys_result.scalars().all() if k}
        )
        await yahoo_service.fetch_players_by_keys(all_keys, db)
        results["player_metadata"] = f"{len(all_keys)} players"

        # ADP — non-fatal; keeper rounds still work without it (franchise rounds will be unavailable)
        try:
            adp_count = await fantasypros_service.refresh_adp_data(db)
            results["adp"] = f"done ({adp_count} players)"
        except Exception as adp_exc:
            logger.warning(f"FantasyPros ADP fetch failed: {adp_exc}")
            results["adp"] = f"failed: {adp_exc}"

        # Keeper eligibility
        await keeper_service.calculate_keeper_eligibility(db)
        results["keeper_eligibility"] = "done"

        # Auto-import pick trades from last season's Yahoo transactions
        try:
            pick_trade_count = await import_pick_trades_from_transactions(db)
            results["pick_trades"] = f"done ({pick_trade_count} picks imported)"
        except Exception as pt_exc:
            logger.warning(f"Pick trade import failed: {pt_exc}")
            results["pick_trades"] = f"failed: {pt_exc}"

        await _set_setting("etl_status", "complete", db)
        await _set_setting("etl_completed_at", datetime.utcnow().isoformat(), db)
        await _set_setting("last_updated", datetime.now(timezone.utc).isoformat(), db)
        await _set_setting("etl_error", "", db)

        logger.info("Initial ETL complete")
        return {"status": "complete", "results": results}

    except Exception as exc:
        logger.error(f"ETL failed: {exc}", exc_info=True)
        await _set_setting("etl_status", "failed", db)
        await _set_setting("etl_error", str(exc), db)
        return {"status": "failed", "error": str(exc)}


async def run_hourly_refresh() -> None:
    """Lightweight hourly job — no Yahoo API calls, only FantasyPros + keeper recalc."""
    async with AsyncSessionLocal() as db:
        etl_status = await get_setting("etl_status", db)
        if etl_status != "complete":
            logger.info("Skipping hourly refresh — initial ETL not complete")
            return

        try:
            await fantasypros_service.refresh_adp_data(db)
            await keeper_service.calculate_keeper_eligibility(db)
            await _set_setting("last_updated", datetime.now(timezone.utc).isoformat(), db)
            logger.info("Hourly refresh complete")
        except Exception as exc:
            logger.error(f"Hourly refresh failed: {exc}", exc_info=True)


async def import_pick_trades_from_transactions(db: AsyncSession) -> int:
    """
    Parse pick trades from last season's Yahoo transactions and upsert into PickOwnership.
    Processes chronologically so if a pick is traded multiple times, the final destination wins.
    Only touches rows created by this function (note='auto-imported'), leaving manual entries intact.
    """
    prev = settings.yahoo_previous_season
    upcoming = settings.yahoo_upcoming_season

    result = await db.execute(
        select(Transaction)
        .where(Transaction.season == prev, Transaction.transaction_type == "trade")
        .order_by(Transaction.timestamp)
    )
    txns = result.scalars().all()

    upserted = 0
    for txn in txns:
        if not txn.details_json:
            continue
        try:
            raw = json.loads(txn.details_json)
        except Exception:
            continue

        txn_info = raw[0] if isinstance(raw, list) else raw
        picks_list = txn_info.get("picks", [])
        if not isinstance(picks_list, list):
            continue

        for item in picks_list:
            pk = item.get("pick", {}) if isinstance(item, dict) else {}
            if not pk:
                continue

            original_team_key = pk.get("original_team_key")
            destination_team_key = pk.get("destination_team_key")
            try:
                round_number = int(pk.get("round"))
            except (TypeError, ValueError):
                continue

            if not all([original_team_key, destination_team_key, round_number]):
                continue

            existing = await db.execute(
                select(PickOwnership).where(
                    PickOwnership.season == upcoming,
                    PickOwnership.original_team_key == original_team_key,
                    PickOwnership.round_number == round_number,
                )
            )
            row = existing.scalar_one_or_none()

            if destination_team_key == original_team_key:
                # Pick traded back to its original owner — remove any auto-imported entry
                if row and row.note == "auto-imported":
                    await db.delete(row)
                continue

            if row:
                if row.note == "auto-imported":
                    row.current_owner_team_key = destination_team_key
            else:
                db.add(PickOwnership(
                    season=upcoming,
                    original_team_key=original_team_key,
                    round_number=round_number,
                    current_owner_team_key=destination_team_key,
                    note="auto-imported",
                ))
                upserted += 1

    await db.commit()
    return upserted


async def get_app_status(db: AsyncSession) -> dict:
    etl_status = await get_setting("etl_status", db)
    last_updated = await get_setting("last_updated", db)
    etl_completed_at = await get_setting("etl_completed_at", db)
    etl_error = await get_setting("etl_error", db)
    is_authorized = await yahoo_service.is_authorized(db)

    return {
        "is_authorized": is_authorized,
        "etl_status": etl_status or "not_started",
        "etl_completed_at": etl_completed_at,
        "etl_error": etl_error,
        "last_updated": last_updated,
        "upcoming_season": settings.yahoo_upcoming_season,
        "previous_season": settings.yahoo_previous_season,
        "league_id": settings.yahoo_league_id,
    }

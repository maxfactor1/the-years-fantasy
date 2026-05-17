from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    yahoo_guid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class YahooGameKey(Base):
    """Caches Yahoo game_id (e.g. 449) for each NFL season year."""
    __tablename__ = "yahoo_game_keys"

    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_key: Mapped[str] = mapped_column(String(16))


class Team(Base):
    __tablename__ = "teams"

    team_key: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "449.l.179398.t.1"
    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(128))
    manager_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    manager_guid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    logo_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    waiver_priority: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class Player(Base):
    __tablename__ = "players"

    player_key: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "449.p.12345"
    player_id: Mapped[int] = mapped_column(Integer)
    name_full: Mapped[str] = mapped_column(String(128))
    name_first: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    name_last: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    position: Mapped[str] = mapped_column(String(16))       # QB, RB, WR, TE, K, DEF
    nfl_team: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    bye_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Cross-reference to FantasyPros
    fantasypros_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fantasypros_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)


class DraftPick(Base):
    """One row per pick in each season's draft, including keeper and regular picks."""
    __tablename__ = "draft_picks"
    __table_args__ = (UniqueConstraint("season", "pick_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season: Mapped[int] = mapped_column(Integer)
    pick_number: Mapped[int] = mapped_column(Integer)
    round_number: Mapped[int] = mapped_column(Integer)
    team_key: Mapped[str] = mapped_column(String(64))
    player_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # null for traded picks
    is_keeper: Mapped[bool] = mapped_column(Boolean, default=False)
    keeper_cost_round: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_key: Mapped[str] = mapped_column(String(64), unique=True)
    season: Mapped[int] = mapped_column(Integer)
    transaction_type: Mapped[str] = mapped_column(String(32))  # add, drop, trade, commissioner
    timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # JSON blob of full transaction details
    details_json: Mapped[str] = mapped_column(Text, default="{}")


class RosterSnapshot(Base):
    """End-of-week-14 roster for each season — determines keeper eligibility."""
    __tablename__ = "roster_snapshots"
    __table_args__ = (UniqueConstraint("season", "team_key", "player_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season: Mapped[int] = mapped_column(Integer)
    team_key: Mapped[str] = mapped_column(String(64))
    player_key: Mapped[str] = mapped_column(String(64))
    roster_position: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    acquisition_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # draft, waivers, trade


class FranchiseTag(Base):
    """Admin-managed franchise tag designations."""
    __tablename__ = "franchise_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season: Mapped[int] = mapped_column(Integer)           # season the tag applies TO (e.g. 2026)
    team_key: Mapped[str] = mapped_column(String(64))
    player_key: Mapped[str] = mapped_column(String(64))
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdpData(Base):
    """FantasyPros consensus ADP rankings (refreshed hourly)."""
    __tablename__ = "adp_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season: Mapped[int] = mapped_column(Integer)
    fantasypros_player_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    player_name: Mapped[str] = mapped_column(String(128))
    position: Mapped[str] = mapped_column(String(16))
    nfl_team: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    adp_rank: Mapped[int] = mapped_column(Integer)          # overall rank (1 = best)
    adp_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Matched Yahoo player key (may be null until matched)
    yahoo_player_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class KeeperEligibility(Base):
    """Pre-computed keeper eligibility for each player on each team for the upcoming season."""
    __tablename__ = "keeper_eligibility"
    __table_args__ = (UniqueConstraint("upcoming_season", "team_key", "player_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upcoming_season: Mapped[int] = mapped_column(Integer)
    team_key: Mapped[str] = mapped_column(String(64))
    player_key: Mapped[str] = mapped_column(String(64))

    # Regular keeper fields
    is_keeper_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    keep_round: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    original_draft_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    original_draft_round: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    years_since_draft: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ineligibility_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Franchise tag fields
    is_franchise_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    franchise_round: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    franchise_ineligibility_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # ADP context
    adp_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    adp_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DraftOrder(Base):
    """
    Manual draft order: which draft position each team holds for the upcoming season.
    Position 1 = first overall pick. Snake ordering is applied automatically.
    """
    __tablename__ = "draft_order"
    __table_args__ = (UniqueConstraint("season", "draft_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season: Mapped[int] = mapped_column(Integer)
    draft_position: Mapped[int] = mapped_column(Integer)   # 1–12
    team_key: Mapped[str] = mapped_column(String(64))


class PickOwnership(Base):
    """
    Tracks who currently owns each draft pick after trades.
    One row per (season, original_team, round). original_team_key is
    the team that was assigned that round slot in the draft order;
    current_owner_team_key is who actually holds the pick today.
    """
    __tablename__ = "pick_ownership"
    __table_args__ = (UniqueConstraint("season", "original_team_key", "round_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season: Mapped[int] = mapped_column(Integer)
    original_team_key: Mapped[str] = mapped_column(String(64))
    round_number: Mapped[int] = mapped_column(Integer)
    current_owner_team_key: Mapped[str] = mapped_column(String(64))
    note: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)


class HistoricalKeeperOverride(Base):
    """
    Manually seeded historical keeper records.
    Used to prime the 4-year keeper history when Yahoo data alone is ambiguous.
    """
    __tablename__ = "historical_keeper_overrides"
    __table_args__ = (UniqueConstraint("season", "player_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season: Mapped[int] = mapped_column(Integer)          # the season the player was KEPT (not drafted)
    player_key: Mapped[str] = mapped_column(String(64))
    team_key: Mapped[str] = mapped_column(String(64))
    original_draft_year: Mapped[int] = mapped_column(Integer)
    original_draft_round: Mapped[int] = mapped_column(Integer)
    was_franchise_tagged: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Yahoo
    yahoo_client_id: str = ""
    yahoo_client_secret: str = ""
    yahoo_league_id: str = "179398"
    yahoo_previous_season: int = 2025
    yahoo_upcoming_season: int = 2026

    # FantasyPros
    fantasypros_api_key: str = ""

    # Admin auth
    admin_password: str = "changeme"
    secret_key: str = "change-this-secret-key-to-something-random"

    # OAuth
    oauth_redirect_uri: str = "http://localhost:8000/setup/callback"

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/fantasy.db"

    # League constants
    league_team_count: int = 12
    league_draft_rounds: int = 16
    max_regular_keepers: int = 3
    max_franchise_tags: int = 4
    max_total_carryovers: int = 4
    max_keeper_years: int = 4          # years a player can be kept (draftyear + 4 keeps = 5 total)
    week14_roster_date: str = "2025-12-15"  # date for fetching end-of-week-14 rosters

    # Yahoo OAuth endpoints
    yahoo_auth_url: str = "https://api.login.yahoo.com/oauth2/request_auth"
    yahoo_token_url: str = "https://api.login.yahoo.com/oauth2/get_token"
    yahoo_api_base: str = "https://fantasysports.yahooapis.com/fantasy/v2"

    # FantasyPros
    fantasypros_api_base: str = "https://api.fantasypros.com/public/v2/json/NFL"


@lru_cache
def get_settings() -> Settings:
    return Settings()

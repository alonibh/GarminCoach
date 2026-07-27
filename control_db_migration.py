"""Small, in-place schema migrations for the control database.

These migrations never touch athlete databases.  They are deliberately
idempotent so deployments can safely run them during normal startup.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.engine import Engine


CALENDAR_SUBSCRIPTION_MIGRATION = "control_calendar_subscription_v1"


def run_control_migrations(engine: Engine) -> None:
    """Add the hashed outbound-calendar token column to older control stores."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS control_migration_versions "
            "(version VARCHAR(128) PRIMARY KEY, applied_at DATETIME NOT NULL)"
        )
        applied = connection.exec_driver_sql(
            "SELECT 1 FROM control_migration_versions WHERE version = ?",
            (CALENDAR_SUBSCRIPTION_MIGRATION,),
        ).first()
        if applied:
            return

        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(integration_routes)"
            )
        }
        if "calendar_feed_token_hash" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE integration_routes "
                "ADD COLUMN calendar_feed_token_hash VARCHAR(64)"
            )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ux_integration_routes_calendar_feed_token_hash "
            "ON integration_routes(calendar_feed_token_hash) "
            "WHERE calendar_feed_token_hash IS NOT NULL"
        )
        connection.exec_driver_sql(
            "INSERT INTO control_migration_versions(version, applied_at) "
            "VALUES (?, ?)",
            (
                CALENDAR_SUBSCRIPTION_MIGRATION,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

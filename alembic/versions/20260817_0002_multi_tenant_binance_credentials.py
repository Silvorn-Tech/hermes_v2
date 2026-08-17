"""Multi-tenant Binance credentials and per-user trading settings.

Hermes moves from one shared, operator-level Binance API key (`.env`
only) to per-user credentials, encrypted at rest (`MultiFernet` — see
`trading/credentials_encryption.py`) and stored in
`user_binance_credentials`. Ciphertext columns are never populated by
this migration; they're written only through
`binance_credentials_service.connect_credentials()`, which verifies a
key against Binance live before it's ever encrypted and persisted — no
existing data is backfilled here, and no row exists for any user until
they connect their own account through that verified path.

`user_trading_settings` holds each user's own risk limits (mirroring
`RiskEngine.RiskLimits`' six fields, same fail-closed "NULL means not
configured, reject on that dimension" semantics the env-var-based global
limits already have) and a personal trading switch
(`trading_enabled`, default `true` — an opt-out convenience pause, not
a security boundary; the platform's real kill switch stays the existing
`TRADING_ENABLED` env var, untouched by this migration). A user with no
row here is treated identically to one with every limit NULL and
`trading_enabled=true` — see `user_risk_settings_service.py`.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260817_0002"
down_revision = "20260817_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_binance_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("api_secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("api_key_last4", sa.String(length=4), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_binance_credentials_user_id",
        "user_binance_credentials",
        ["user_id"],
        unique=True,
    )

    op.create_table(
        "user_trading_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "trading_enabled",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
        sa.Column(
            "max_order_notional_quote",
            sa.Numeric(precision=28, scale=10),
            nullable=True,
        ),
        sa.Column(
            "max_symbol_exposure_pct", sa.Numeric(precision=6, scale=3), nullable=True
        ),
        sa.Column(
            "max_total_exposure_pct", sa.Numeric(precision=6, scale=3), nullable=True
        ),
        sa.Column(
            "max_daily_loss_pct", sa.Numeric(precision=6, scale=3), nullable=True
        ),
        sa.Column("max_open_positions", sa.Integer(), nullable=True),
        sa.Column(
            "allowed_symbols", postgresql.ARRAY(sa.String(length=20)), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_trading_settings_user_id",
        "user_trading_settings",
        ["user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_trading_settings_user_id", table_name="user_trading_settings"
    )
    op.drop_table("user_trading_settings")
    op.drop_index(
        "ix_user_binance_credentials_user_id", table_name="user_binance_credentials"
    )
    op.drop_table("user_binance_credentials")

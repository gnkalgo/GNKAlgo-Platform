"""Phase 8 versioned strategies, runs and signals."""
from alembic import op

from app.database import Base
from app import models  # noqa: F401

revision = "0004_phase_8_strategy_engine"
down_revision = "0003_phase_7_order_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in ("strategies", "strategy_versions", "strategy_runs", "strategy_signals"):
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    for table_name in ("strategy_signals", "strategy_runs", "strategy_versions", "strategies"):
        op.drop_table(table_name)

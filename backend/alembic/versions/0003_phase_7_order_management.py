"""Phase 7 order-management, execution, position and kill-switch schema."""
from alembic import op

from app.database import Base
from app import models  # noqa: F401

revision = "0003_phase_7_order_management"
down_revision = "0002_phase_6_market_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in ("trading_orders", "trade_executions", "trading_positions", "trading_controls"):
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    for table_name in ("trading_controls", "trading_positions", "trade_executions", "trading_orders"):
        op.drop_table(table_name)

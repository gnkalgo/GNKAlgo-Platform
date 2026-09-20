"""Phase 6 market-data schema."""
from alembic import op
from app.database import Base
from app import models  # noqa: F401

revision = "0002_phase_6_market_data"
down_revision = "0001_phase_1_phase_2"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    for table_name in ["instruments", "market_candles", "market_ws_tickets"]:
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)

def downgrade() -> None:
    for table_name in ["market_ws_tickets", "market_candles", "instruments"]:
        op.drop_table(table_name)

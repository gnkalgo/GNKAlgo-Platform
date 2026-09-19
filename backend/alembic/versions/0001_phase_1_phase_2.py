"""Phase 1 authentication and Phase 2 broker/API-key schema."""
from alembic import op
from app.database import Base
from app import models  # noqa: F401

revision = "0001_phase_1_phase_2"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)

def downgrade() -> None:
    # Explicit table order preserves foreign-key rollback safety.
    for table in ["audit_logs", "api_keys", "broker_connections", "oauth_states", "one_time_tokens", "user_sessions", "users"]:
        op.drop_table(table)

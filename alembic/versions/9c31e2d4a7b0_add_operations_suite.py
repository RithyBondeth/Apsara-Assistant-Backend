"""add operations suite

Revision ID: 9c31e2d4a7b0
Revises: f6d2b8a41c90
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "9c31e2d4a7b0"
down_revision = "f6d2b8a41c90"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("low_stock_email_enabled", sa.Boolean(), server_default="true", nullable=False))
    op.add_column("users", sa.Column("low_stock_telegram_enabled", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("users", sa.Column("low_stock_telegram_chat_id", sa.String(length=100)))
    op.create_table("suppliers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False), sa.Column("email", sa.String(320)),
        sa.Column("phone", sa.String(50)), sa.Column("address", sa.Text()), sa.Column("notes", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_index("ix_suppliers_user_name", "suppliers", ["user_id", "name"])
    op.create_table("purchase_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("total_cost", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("expected_at", sa.DateTime()), sa.Column("notes", sa.Text()), sa.Column("ordered_at", sa.DateTime()),
        sa.Column("received_at", sa.DateTime()), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_index("ix_purchase_orders_user_id", "purchase_orders", ["user_id"])
    op.create_table("purchase_order_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("purchase_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_variants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("product_name", sa.String(), nullable=False), sa.Column("variant_name", sa.String(500), nullable=False),
        sa.Column("variant_sku", sa.String(100)), sa.Column("ordered_quantity", sa.Integer(), nullable=False),
        sa.Column("received_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit_cost", sa.Numeric(12, 2), nullable=False),
        sa.CheckConstraint("ordered_quantity > 0", name="ck_po_items_ordered_positive"),
        sa.CheckConstraint("received_quantity >= 0 AND received_quantity <= ordered_quantity", name="ck_po_items_received_valid"),
        sa.CheckConstraint("unit_cost >= 0", name="ck_po_items_cost_nonnegative"))
    op.create_index("ix_purchase_order_items_purchase_order_id", "purchase_order_items", ["purchase_order_id"])
    op.create_table("sales_returns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="requested"),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("notes", sa.Text()),
        sa.Column("refund_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("received_at", sa.DateTime()), sa.Column("refunded_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_index("ix_sales_returns_user_id", "sales_returns", ["user_id"])
    op.create_index("ix_sales_returns_order_id", "sales_returns", ["order_id"])
    op.create_table("sales_return_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sales_return_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sales_returns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_variants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("product_name", sa.String(), nullable=False), sa.Column("variant_name", sa.String(500), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False), sa.Column("restock_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("quantity > 0", name="ck_return_items_quantity_positive"),
        sa.CheckConstraint("restock_quantity >= 0 AND restock_quantity <= quantity", name="ck_return_items_restock_valid"))
    op.create_index("ix_sales_return_items_sales_return_id", "sales_return_items", ["sales_return_id"])
    op.create_table("low_stock_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_name", sa.String(), nullable=False), sa.Column("variant_name", sa.String(500), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False), sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("email_sent_at", sa.DateTime()), sa.Column("telegram_sent_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("resolved_at", sa.DateTime()))
    op.create_index("ix_low_stock_alerts_user_created", "low_stock_alerts", ["user_id", "created_at"])
    op.create_index("uq_open_low_stock_alert", "low_stock_alerts", ["variant_id"], unique=True,
                    postgresql_where=sa.text("resolved_at IS NULL"))
    op.add_column("inventory_movements", sa.Column("purchase_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_orders.id", ondelete="SET NULL")))
    op.add_column("inventory_movements", sa.Column("sales_return_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sales_returns.id", ondelete="SET NULL")))


def downgrade():
    op.drop_column("inventory_movements", "sales_return_id")
    op.drop_column("inventory_movements", "purchase_order_id")
    op.drop_table("low_stock_alerts")
    op.drop_table("sales_return_items")
    op.drop_table("sales_returns")
    op.drop_table("purchase_order_items")
    op.drop_table("purchase_orders")
    op.drop_table("suppliers")
    op.drop_column("users", "low_stock_telegram_chat_id")
    op.drop_column("users", "low_stock_telegram_enabled")
    op.drop_column("users", "low_stock_email_enabled")

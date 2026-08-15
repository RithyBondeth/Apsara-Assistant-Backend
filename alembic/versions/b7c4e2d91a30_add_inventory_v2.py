"""add inventory v2

Revision ID: b7c4e2d91a30
Revises: a4d7c2e91f30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7c4e2d91a30"
down_revision: Union[str, None] = "a4d7c2e91f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE products SET stock = 0 WHERE stock IS NULL")
    op.alter_column("products", "stock", existing_type=sa.Integer(), nullable=False)
    op.add_column(
        "products", sa.Column("reserved_stock", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "products",
        sa.Column("low_stock_threshold", sa.Integer(), nullable=False, server_default="5"),
    )
    op.create_check_constraint("ck_products_stock_nonnegative", "products", "stock >= 0")
    op.create_check_constraint(
        "ck_products_reserved_stock_nonnegative", "products", "reserved_stock >= 0"
    )
    op.create_check_constraint(
        "ck_products_low_stock_threshold_nonnegative", "products", "low_stock_threshold >= 0"
    )

    op.add_column("orders", sa.Column("reservation_expires_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_orders_reservation_expires_at", "orders", ["reservation_expires_at"], unique=False
    )

    op.create_table(
        "inventory_movements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("quantity_delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("reserved_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_inventory_movements_product_created",
        "inventory_movements",
        ["product_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_movements_user_created",
        "inventory_movements",
        ["user_id", "created_at"],
        unique=False,
    )

    # Existing active orders already reduced `stock`; reconstruct the portion
    # that is still reserved without changing their available balance.
    op.execute(
        """
        UPDATE products AS p
        SET reserved_stock = totals.quantity
        FROM (
            SELECT oi.product_id, SUM(oi.quantity)::integer AS quantity
            FROM order_items AS oi
            JOIN orders AS o ON o.id = oi.order_id
            WHERE o.status IN ('pending', 'confirmed', 'processing')
            GROUP BY oi.product_id
        ) AS totals
        WHERE p.id = totals.product_id
        """
    )
    op.execute(
        """
        UPDATE orders
        SET reservation_expires_at = created_at + INTERVAL '24 hours'
        WHERE status = 'pending'
        """
    )
    op.execute(
        """
        INSERT INTO inventory_movements
            (id, user_id, product_id, product_name, kind, quantity_delta, balance_after,
             reserved_after, reason, created_at)
        SELECT gen_random_uuid(), user_id, id, name, 'migration_snapshot', 0, stock,
               reserved_stock, 'Inventory V2 opening snapshot', CURRENT_TIMESTAMP
        FROM products
        """
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_movements_user_created", table_name="inventory_movements")
    op.drop_index("ix_inventory_movements_product_created", table_name="inventory_movements")
    op.drop_table("inventory_movements")
    op.drop_index("ix_orders_reservation_expires_at", table_name="orders")
    op.drop_column("orders", "reservation_expires_at")
    op.drop_constraint("ck_products_low_stock_threshold_nonnegative", "products", type_="check")
    op.drop_constraint("ck_products_reserved_stock_nonnegative", "products", type_="check")
    op.drop_constraint("ck_products_stock_nonnegative", "products", type_="check")
    op.drop_column("products", "low_stock_threshold")
    op.drop_column("products", "reserved_stock")
    op.alter_column("products", "stock", existing_type=sa.Integer(), nullable=True)

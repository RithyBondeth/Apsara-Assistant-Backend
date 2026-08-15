"""add product variants

Revision ID: f6d2b8a41c90
Revises: e2a9c4f71b60
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f6d2b8a41c90"
down_revision: Union[str, None] = "e2a9c4f71b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("option_values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("option_signature", sa.String(length=1000), nullable=False),
        sa.Column("sku", sa.String(length=100), nullable=True),
        sa.Column("barcode", sa.String(length=100), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("reserved_stock", sa.Integer(), nullable=False),
        sa.Column("low_stock_threshold", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("price >= 0", name="ck_product_variants_price_nonnegative"),
        sa.CheckConstraint("reserved_stock >= 0", name="ck_product_variants_reserved_nonnegative"),
        sa.CheckConstraint("stock >= 0", name="ck_product_variants_stock_nonnegative"),
        sa.CheckConstraint(
            "low_stock_threshold >= 0", name="ck_product_variants_threshold_nonnegative"
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_variants_product_id", "product_variants", ["product_id"])
    op.create_index("ix_product_variants_user_id", "product_variants", ["user_id"])
    op.create_index(
        "uq_product_variants_option_signature",
        "product_variants",
        ["product_id", "option_signature"],
        unique=True,
    )
    op.create_index(
        "uq_product_variants_default",
        "product_variants",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        "uq_product_variants_sku",
        "product_variants",
        ["user_id", "sku"],
        unique=True,
        postgresql_where=sa.text("sku IS NOT NULL"),
    )
    op.create_index(
        "uq_product_variants_barcode",
        "product_variants",
        ["user_id", "barcode"],
        unique=True,
        postgresql_where=sa.text("barcode IS NOT NULL"),
    )

    op.add_column("product_images", sa.Column("variant_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_product_images_variant_id",
        "product_images",
        "product_variants",
        ["variant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_product_images_variant_id", "product_images", ["variant_id"])

    op.add_column("inventory_movements", sa.Column("variant_id", postgresql.UUID(as_uuid=True)))
    op.add_column("inventory_movements", sa.Column("variant_name", sa.String(length=500)))
    op.add_column("inventory_movements", sa.Column("variant_sku", sa.String(length=100)))
    op.create_foreign_key(
        "fk_inventory_movements_variant_id",
        "inventory_movements",
        "product_variants",
        ["variant_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("order_items", sa.Column("variant_id", postgresql.UUID(as_uuid=True)))
    op.add_column("order_items", sa.Column("variant_name", sa.String(length=500)))
    op.add_column("order_items", sa.Column("variant_sku", sa.String(length=100)))
    op.add_column(
        "order_items", sa.Column("variant_options", postgresql.JSONB(astext_type=sa.Text()))
    )

    op.execute(sa.text("""
        INSERT INTO product_variants
            (id, user_id, product_id, option_values, option_signature, sku, barcode,
             price, stock, reserved_stock, low_stock_threshold, is_active, is_default,
             created_at, updated_at)
        SELECT
            gen_random_uuid(), user_id, id, '{}'::jsonb, '{}', NULL, NULL,
            price, stock, reserved_stock, low_stock_threshold, true, true,
            created_at, updated_at
        FROM products
    """))
    op.execute(sa.text("""
        UPDATE order_items AS item
        SET variant_id = variant.id,
            variant_name = 'Default',
            variant_options = '{}'::jsonb
        FROM product_variants AS variant
        WHERE item.product_id = variant.product_id AND variant.is_default
    """))
    op.execute(sa.text("""
        UPDATE inventory_movements AS movement
        SET variant_id = variant.id, variant_name = 'Default'
        FROM product_variants AS variant
        WHERE movement.product_id = variant.product_id AND variant.is_default
    """))

    op.alter_column("order_items", "variant_id", nullable=False)
    op.alter_column("order_items", "variant_name", nullable=False)
    op.alter_column("order_items", "variant_options", nullable=False)
    op.create_foreign_key(
        "fk_order_items_variant_id",
        "order_items",
        "product_variants",
        ["variant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_order_items_variant_id", "order_items", ["variant_id"])


def downgrade() -> None:
    op.drop_index("ix_order_items_variant_id", table_name="order_items")
    op.drop_constraint("fk_order_items_variant_id", "order_items", type_="foreignkey")
    op.drop_column("order_items", "variant_options")
    op.drop_column("order_items", "variant_sku")
    op.drop_column("order_items", "variant_name")
    op.drop_column("order_items", "variant_id")

    op.drop_constraint("fk_inventory_movements_variant_id", "inventory_movements", type_="foreignkey")
    op.drop_column("inventory_movements", "variant_sku")
    op.drop_column("inventory_movements", "variant_name")
    op.drop_column("inventory_movements", "variant_id")

    op.drop_index("ix_product_images_variant_id", table_name="product_images")
    op.drop_constraint("fk_product_images_variant_id", "product_images", type_="foreignkey")
    op.drop_column("product_images", "variant_id")

    op.drop_index("uq_product_variants_barcode", table_name="product_variants")
    op.drop_index("uq_product_variants_sku", table_name="product_variants")
    op.drop_index("uq_product_variants_default", table_name="product_variants")
    op.drop_index("uq_product_variants_option_signature", table_name="product_variants")
    op.drop_index("ix_product_variants_user_id", table_name="product_variants")
    op.drop_index("ix_product_variants_product_id", table_name="product_variants")
    op.drop_table("product_variants")

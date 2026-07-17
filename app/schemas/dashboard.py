from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class DashboardStats(BaseModel):
    """Aggregated headline metrics for the seller's dashboard."""

    products: int          # active products in the catalogue
    customers: int
    conversations: int
    open_conversations: int
    # Threads waiting on the seller: the AI escalated or failed, or the customer
    # has replied since they last looked. Drives the sidebar badge.
    needs_me_conversations: int
    orders: int
    pending_orders: int
    revenue: Decimal       # sum of non-cancelled order totals

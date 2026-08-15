from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from math import ceil
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.core.clock import utcnow
from app.database import get_db
from app.models.operations import (LowStockAlert, PurchaseOrder, PurchaseOrderItem,
                                   SalesReturn, SalesReturnItem, Supplier)
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.user import User
from app.schemas.operations import (AlertOut, BestSellerOut, ForecastOut, PurchaseOrderCreate,
                                    PurchaseOrderOut, PurchaseOrderUpdate, ReceivePurchase,
                                    ReportOut, ReturnCreate, ReturnOut, SalesDayOut,
                                    SupplierData, SupplierOut, SupplierUpdate)
from app.services.alerts import scan_low_stock
from app.services.inventory import move_stock

router = APIRouter()
PO_STATUSES = {"draft", "ordered", "partially_received", "received", "cancelled"}


@router.get("/alerts", response_model=list[AlertOut])
def alerts(include_resolved: bool = False, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
           current_user: User = Depends(get_current_user)):
    scan_low_stock(db, current_user.id)
    db.commit()
    query = db.query(LowStockAlert).filter(LowStockAlert.user_id == current_user.id)
    if not include_resolved:
        query = query.filter(LowStockAlert.resolved_at.is_(None))
    return query.order_by(LowStockAlert.created_at.desc()).limit(limit).all()


@router.get("/suppliers", response_model=list[SupplierOut])
def suppliers(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(Supplier).filter(Supplier.user_id == current_user.id).order_by(Supplier.name).all()


@router.post("/suppliers", response_model=SupplierOut, status_code=status.HTTP_201_CREATED)
def create_supplier(payload: SupplierData, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    supplier = Supplier(user_id=current_user.id, **payload.model_dump())
    db.add(supplier); db.commit(); db.refresh(supplier)
    return supplier


@router.patch("/suppliers/{supplier_id}", response_model=SupplierOut)
def update_supplier(supplier_id: UUID, payload: SupplierUpdate, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    supplier = db.query(Supplier).filter(Supplier.id == supplier_id, Supplier.user_id == current_user.id).first()
    if not supplier:
        raise HTTPException(404, "Supplier not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(supplier, key, value)
    db.commit(); db.refresh(supplier)
    return supplier


def _po(db: Session, po_id: UUID, user_id: UUID, lock=False):
    query = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id, PurchaseOrder.user_id == user_id)
    if not lock:
        query = query.options(joinedload(PurchaseOrder.supplier), joinedload(PurchaseOrder.items))
    po = query.with_for_update().first() if lock else query.first()
    if not po:
        raise HTTPException(404, "Purchase order not found")
    return po


@router.get("/purchase-orders", response_model=list[PurchaseOrderOut])
def purchase_orders(skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(PurchaseOrder).options(joinedload(PurchaseOrder.supplier), joinedload(PurchaseOrder.items)).filter(
        PurchaseOrder.user_id == current_user.id).order_by(PurchaseOrder.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/purchase-orders", response_model=PurchaseOrderOut, status_code=status.HTTP_201_CREATED)
def create_purchase_order(payload: PurchaseOrderCreate, db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    supplier = db.query(Supplier).filter(Supplier.id == payload.supplier_id,
                                         Supplier.user_id == current_user.id,
                                         Supplier.is_active == True).first()
    if not supplier:
        raise HTTPException(404, "Supplier not found")
    variant_ids = [item.variant_id for item in payload.items]
    if len(set(variant_ids)) != len(variant_ids):
        raise HTTPException(400, "Each variant can appear only once")
    variants = db.query(ProductVariant).filter(
        ProductVariant.id.in_(variant_ids), ProductVariant.user_id == current_user.id).all()
    by_id = {variant.id: variant for variant in variants}
    if len(by_id) != len(variant_ids):
        raise HTTPException(404, "A product variant was not found")
    po = PurchaseOrder(user_id=current_user.id, supplier_id=supplier.id,
                       currency=current_user.currency, expected_at=payload.expected_at,
                       notes=payload.notes, total_cost=0)
    db.add(po); db.flush()
    total = Decimal("0")
    for requested in payload.items:
        variant = by_id[requested.variant_id]
        total += requested.unit_cost * requested.ordered_quantity
        po.items.append(PurchaseOrderItem(
            product_id=variant.product_id, variant_id=variant.id,
            product_name=variant.product.name, variant_name=variant.name,
            variant_sku=variant.sku, ordered_quantity=requested.ordered_quantity,
            unit_cost=requested.unit_cost))
    po.total_cost = total
    db.commit()
    return _po(db, po.id, current_user.id)


@router.patch("/purchase-orders/{po_id}", response_model=PurchaseOrderOut)
def update_purchase_order(po_id: UUID, payload: PurchaseOrderUpdate, db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    po = _po(db, po_id, current_user.id, lock=True)
    data = payload.model_dump(exclude_unset=True)
    new_status = data.pop("status", None)
    if new_status:
        if new_status not in PO_STATUSES:
            raise HTTPException(400, "Invalid purchase order status")
        allowed = ((po.status == "draft" and new_status in {"ordered", "cancelled"}) or
                   (po.status == "ordered" and new_status == "cancelled"))
        if not allowed:
            raise HTTPException(409, "This purchase order can no longer make that transition")
        po.status = new_status
        if new_status == "ordered": po.ordered_at = utcnow()
    for key, value in data.items(): setattr(po, key, value)
    db.commit()
    return _po(db, po.id, current_user.id)


@router.post("/purchase-orders/{po_id}/receive", response_model=PurchaseOrderOut)
def receive_purchase(po_id: UUID, payload: ReceivePurchase, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    po = _po(db, po_id, current_user.id, lock=True)
    if po.status not in {"ordered", "partially_received"}:
        raise HTTPException(409, "Only ordered purchase orders can be received")
    requested = {item.item_id: item.quantity for item in payload.items}
    if len(requested) != len(payload.items): raise HTTPException(400, "Duplicate receive line")
    items = {item.id: item for item in po.items}
    if not set(requested) <= set(items): raise HTTPException(404, "Purchase order item not found")
    variant_ids = sorted({items[item_id].variant_id for item_id in requested}, key=str)
    variants = db.query(ProductVariant).filter(
        ProductVariant.id.in_(variant_ids), ProductVariant.user_id == current_user.id
    ).order_by(ProductVariant.id).with_for_update().all()
    by_id = {variant.id: variant for variant in variants}
    for item_id, quantity in requested.items():
        item = items[item_id]
        if item.received_quantity + quantity > item.ordered_quantity:
            raise HTTPException(400, f"Cannot receive more than ordered for {item.product_name}")
        variant = by_id.get(item.variant_id)
        if not variant: raise HTTPException(409, "A purchased variant no longer exists")
        move_stock(db, variant.product, variant, available_delta=quantity, kind="purchase_received",
                   reason=f"Received purchase order {po.id}", actor_user_id=current_user.id,
                   purchase_order_id=po.id)
        item.received_quantity += quantity
    complete = all(item.received_quantity == item.ordered_quantity for item in po.items)
    po.status = "received" if complete else "partially_received"
    if complete: po.received_at = utcnow()
    db.commit()
    return _po(db, po.id, current_user.id)


def _return(db: Session, return_id: UUID, user_id: UUID, lock=False):
    query = db.query(SalesReturn).filter(SalesReturn.id == return_id, SalesReturn.user_id == user_id)
    if not lock:
        query = query.options(joinedload(SalesReturn.items))
    value = query.with_for_update().first() if lock else query.first()
    if not value: raise HTTPException(404, "Return not found")
    return value


@router.get("/returns", response_model=list[ReturnOut])
def returns(skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(SalesReturn).options(joinedload(SalesReturn.items)).filter(
        SalesReturn.user_id == current_user.id).order_by(SalesReturn.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/returns", response_model=ReturnOut, status_code=status.HTTP_201_CREATED)
def create_return(payload: ReturnCreate, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    order = db.query(Order).filter(Order.id == payload.order_id,
        Order.user_id == current_user.id).with_for_update().first()
    if not order: raise HTTPException(404, "Order not found")
    if order.status not in {"shipped", "delivered"}: raise HTTPException(409, "Only fulfilled orders can be returned")
    requested = {item.order_item_id: item for item in payload.items}
    if len(requested) != len(payload.items): raise HTTPException(400, "Duplicate return line")
    order_items = {item.id: item for item in order.items}
    if not set(requested) <= set(order_items): raise HTTPException(404, "Order item not found")
    already = dict(db.query(SalesReturnItem.order_item_id, func.coalesce(func.sum(SalesReturnItem.quantity), 0))
                   .join(SalesReturn).filter(SalesReturn.order_id == order.id)
                   .group_by(SalesReturnItem.order_item_id).all())
    value = SalesReturn(user_id=current_user.id, order_id=order.id, reason=payload.reason,
                        notes=payload.notes, refund_amount=payload.refund_amount)
    db.add(value)
    for item_id, request in requested.items():
        sold = order_items[item_id]
        if request.restock_quantity > request.quantity: raise HTTPException(400, "Restock quantity cannot exceed returned quantity")
        if int(already.get(item_id, 0)) + request.quantity > sold.quantity:
            raise HTTPException(400, f"Returned quantity exceeds sold quantity for {sold.product.name}")
        value.items.append(SalesReturnItem(order_item_id=sold.id, product_id=sold.product_id,
            variant_id=sold.variant_id, product_name=sold.product.name, variant_name=sold.variant_name,
            quantity=request.quantity, restock_quantity=request.restock_quantity))
    db.commit(); db.refresh(value)
    return value


@router.post("/returns/{return_id}/receive", response_model=ReturnOut)
def receive_return(return_id: UUID, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    value = _return(db, return_id, current_user.id, lock=True)
    if value.received_at: raise HTTPException(409, "Return was already received")
    variant_ids = sorted({item.variant_id for item in value.items if item.restock_quantity}, key=str)
    variants = db.query(ProductVariant).filter(
        ProductVariant.id.in_(variant_ids), ProductVariant.user_id == current_user.id
    ).order_by(ProductVariant.id).with_for_update().all()
    by_id = {variant.id: variant for variant in variants}
    for item in value.items:
        if not item.restock_quantity: continue
        variant = by_id.get(item.variant_id)
        if not variant: raise HTTPException(409, "A returned variant no longer exists")
        move_stock(db, variant.product, variant, available_delta=item.restock_quantity,
                   kind="return_received", reason=f"Restocked return {value.id}",
                   actor_user_id=current_user.id, sales_return_id=value.id)
    value.received_at = utcnow()
    value.status = "refunded" if value.refunded_at else "received"
    db.commit(); return _return(db, value.id, current_user.id)


@router.post("/returns/{return_id}/refund", response_model=ReturnOut)
def refund_return(return_id: UUID, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    value = _return(db, return_id, current_user.id, lock=True)
    if value.refunded_at: raise HTTPException(409, "Return was already refunded")
    if value.refund_amount <= 0: raise HTTPException(400, "Set a refund amount before recording a refund")
    order = db.query(Order).filter(Order.id == value.order_id,
                                  Order.user_id == current_user.id).with_for_update().one()
    order_total = order.total_amount
    refunded = db.query(func.coalesce(func.sum(SalesReturn.refund_amount), 0)).filter(
        SalesReturn.order_id == value.order_id, SalesReturn.refunded_at.isnot(None)).scalar()
    if Decimal(refunded) + value.refund_amount > Decimal(order_total):
        raise HTTPException(400, "Refunds cannot exceed the order total")
    value.refunded_at = utcnow(); value.status = "refunded"
    db.commit(); return _return(db, value.id, current_user.id)


@router.get("/reports", response_model=ReportOut)
def reports(days: int = Query(30, ge=7, le=365), forecast_days: int = Query(30, ge=7, le=180),
            db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    cutoff = utcnow() - timedelta(days=days)
    rows = db.query(Order, OrderItem, Product.name).select_from(Order).join(
        OrderItem, OrderItem.order_id == Order.id).join(
        Product, Product.id == OrderItem.product_id).filter(
        Order.user_id == current_user.id, Order.status.in_(("shipped", "delivered")),
        Order.created_at >= cutoff).all()
    product_stats = defaultdict(lambda: {"units": 0, "revenue": Decimal("0"), "product": "", "variant": ""})
    daily = defaultdict(lambda: {"orders": set(), "units": 0, "revenue": Decimal("0")})
    for order, item, product_name in rows:
        key = (item.product_id, item.variant_id)
        stat = product_stats[key]; stat["product"] = product_name; stat["variant"] = item.variant_name
        stat["units"] += item.quantity; stat["revenue"] += item.subtotal
        day = order.created_at.date().isoformat(); daily[day]["orders"].add(order.id)
        daily[day]["units"] += item.quantity; daily[day]["revenue"] += item.subtotal
    returned_units = dict(db.query(SalesReturnItem.variant_id, func.coalesce(func.sum(SalesReturnItem.quantity), 0))
        .join(SalesReturn).filter(SalesReturn.user_id == current_user.id,
                                  SalesReturn.created_at >= cutoff,
                                  SalesReturn.status.in_(("received", "refunded")))
        .group_by(SalesReturnItem.variant_id).all())
    for (_, variant_id), stat in product_stats.items():
        stat["units"] = max(0, stat["units"] - int(returned_units.get(variant_id, 0)))
    best = [BestSellerOut(product_id=k[0], variant_id=k[1], product_name=v["product"],
                          variant_name=v["variant"], units_sold=v["units"], revenue=v["revenue"])
            for k, v in product_stats.items()]
    best.sort(key=lambda item: (item.units_sold, item.revenue), reverse=True)
    variants = db.query(ProductVariant).options(joinedload(ProductVariant.product)).filter(
        ProductVariant.user_id == current_user.id, ProductVariant.is_active == True).all()
    forecast = []
    for variant in variants:
        sold = product_stats[(variant.product_id, variant.id)]["units"]
        velocity = sold / days
        expected = ceil(velocity * forecast_days)
        cover = round(variant.stock / velocity, 1) if velocity else None
        forecast.append(ForecastOut(product_id=variant.product_id, product_name=variant.product.name,
            variant_id=variant.id, variant_name=variant.name, current_stock=variant.stock,
            units_sold=sold, daily_velocity=round(velocity, 2), forecast_units=expected,
            days_of_cover=cover, suggested_reorder=max(0, expected + variant.low_stock_threshold - variant.stock)))
    forecast.sort(key=lambda item: (item.suggested_reorder, item.units_sold), reverse=True)
    sales_days = [SalesDayOut(date=day, orders=len(value["orders"]), units=value["units"], revenue=value["revenue"])
                  for day, value in sorted(daily.items())]
    order_ids = {order.id for order, _, _ in rows}
    refunds = Decimal(db.query(func.coalesce(func.sum(SalesReturn.refund_amount), 0)).filter(
        SalesReturn.user_id == current_user.id, SalesReturn.refunded_at >= cutoff).scalar())
    return ReportOut(days=days, forecast_days=forecast_days, total_orders=len(order_ids),
                     total_units=sum(stat["units"] for stat in product_stats.values()),
                     total_revenue=sum((item.subtotal for _, item, _ in rows), Decimal("0")) - refunds,
                     best_sellers=best[:20], forecast=forecast, daily_sales=sales_days)

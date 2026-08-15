from fastapi import APIRouter

from app.api.v1.endpoints import attachments, auth, products, customers, conversations, orders, chat, webhooks, integrations, inventory

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(products.router, prefix="/products", tags=["products"])
api_router.include_router(customers.router, prefix="/customers", tags=["customers"])
api_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])
api_router.include_router(orders.router, prefix="/orders", tags=["orders"])
api_router.include_router(attachments.router, prefix="/attachments", tags=["attachments"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(integrations.router, prefix="/integrations", tags=["integrations"])
api_router.include_router(inventory.router, prefix="/inventory", tags=["inventory"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])

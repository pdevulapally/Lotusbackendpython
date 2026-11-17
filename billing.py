import time
from typing import Literal
from uuid import uuid4

import stripe
from fastapi import APIRouter, HTTPException, Request
from google.api_core.exceptions import GoogleAPICallError
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Route, Router

from auth import CurrentUser
from authorization import identity_key
from config import Settings
from firestore_access import FirestoreSession, document_segment
from observability import error_response


class BillingBinding(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    customer_id: str
    subscription_id: str


class SubscriptionState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    customer_id: str
    price_id: str | None
    valid_until: int
    livemode: bool


class CheckoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_id: str


router = APIRouter(prefix="/api/v1/billing")


@router.get("/plan")
def personal_plan(request: Request, user: CurrentUser):
    settings: Settings = request.app.state.settings
    try:
        name = request.app.state.resource_database.run(
            lambda tx: effective_plan(tx, settings, "individual", user["uid"])
        )
    except GoogleAPICallError, ValueError:
        raise HTTPException(503, "Your plan is temporarily unavailable.") from None
    return {"plan": name, **settings.model_plans[name].model_dump()}


@router.post("/checkout")
def begin_checkout(body: CheckoutInput, request: Request, user: CurrentUser):
    settings: Settings = request.app.state.settings
    key = settings.stripe_price_plans.get(body.price_id)
    if key is None or settings.model_plans[key].scope != "individual":
        raise HTTPException(422, "Choose an available individual subscription.")
    account = identity_key(user["uid"])
    root = settings.billing_collection
    path = (root, f"checkout-{account}")

    def reserve(tx: FirestoreSession):
        if tx.get((root, f"individual-{account}")) is not None:
            raise HTTPException(409, "This account already has a billing subscription.")
        pending = tx.get(path)
        if pending is not None:
            if pending["price_id"] != body.price_id or time.time() >= pending["created"] + 86400:
                # Stripe may prune idempotency keys after 24 hours.
                raise HTTPException(409, "Resolve your previous checkout before starting another.")
            return pending["attempt"]
        attempt = uuid4().hex
        tx.put(
            path,
            {"price_id": body.price_id, "created": int(time.time()), "attempt": attempt},
            exists=False,
        )
        return attempt

    try:
        attempt = request.app.state.resource_database.run(reserve)
        transport = stripe.RequestsClient(timeout=settings.stripe_timeout_seconds)
        try:
            client = stripe.StripeClient(
                settings.stripe_api_key.get_secret_value(),
                http_client=transport,
                max_network_retries=0,
            )
            session = client.v1.checkout.sessions.create(
                params={
                    "mode": "subscription",
                    "line_items": [{"price": body.price_id, "quantity": 1}],
                    "client_reference_id": account,
                    "metadata": {"lotus_attempt": attempt},
                    "success_url": str(settings.stripe_success_url),
                    "cancel_url": str(settings.stripe_cancel_url),
                },
                options={"idempotency_key": attempt},
            )
        finally:
            transport.close()
        if not session.url or session.livemode != settings.stripe_livemode:
            raise ValueError("Invalid Checkout session.")
        return {"url": session.url}
    except stripe.StripeError, GoogleAPICallError, ValueError:
        raise HTTPException(503, "Checkout is temporarily unavailable.") from None


def finish_checkout(request: Request, obj, completed: bool):
    settings = request.app.state.settings
    if obj.mode != "subscription" or not obj.metadata or not obj.metadata.get("lotus_attempt"):
        return
    account = document_segment(obj.client_reference_id)
    root = settings.billing_collection
    pending_path = (root, f"checkout-{account}")

    def finish(tx):
        pending = tx.get(pending_path)
        if pending is None or pending["attempt"] != obj.metadata["lotus_attempt"]:
            return
        if completed:
            binding_path = (root, f"individual-{account}")
            binding = BillingBinding(
                customer_id=document_segment(obj.customer),
                subscription_id=document_segment(obj.subscription),
            ).model_dump()
            existing = tx.get(binding_path)
            if existing is not None and existing != binding:
                raise ValueError("Conflicting checkout binding.")
            if existing is None:
                tx.put(binding_path, binding, exists=False)
        tx.delete(pending_path)

    request.app.state.resource_database.run(finish)


def effective_plan(
    tx: FirestoreSession, settings: Settings, scope: Literal["individual", "team"], identity: str
):
    root = settings.billing_collection
    binding_data = tx.get((root, f"{scope}-{identity_key(identity)}"))
    if binding_data is None and scope == "individual":
        return settings.free_plan_id
    if binding_data is None:
        raise HTTPException(403, "This workspace has no active team subscription.")
    binding = BillingBinding.model_validate(binding_data)
    data = tx.get((root, document_segment(binding.subscription_id)))
    if data is None:
        raise HTTPException(503, "Your subscription is still being confirmed.")
    state = SubscriptionState.model_validate(data)
    if state.customer_id != binding.customer_id or state.livemode != settings.stripe_livemode:
        raise ValueError("Invalid billing binding.")
    if state.price_id is None or state.valid_until <= time.time():
        if scope == "individual":
            return settings.free_plan_id
        raise HTTPException(403, "Your team's subscription is inactive.")
    key = settings.stripe_price_plans.get(state.price_id)
    if key is None or settings.model_plans[key].scope != scope:
        raise HTTPException(403, "This subscription does not grant access in this workspace.")
    return key


def reconcile(request: Request, subscription_id: str):
    settings: Settings = request.app.state.settings
    path = (settings.billing_collection, document_segment(subscription_id))

    def update(tx: FirestoreSession):
        previous = tx.get(path)
        # Fetch after the transactional read so a concurrent delivery cannot
        # overwrite a newer snapshot with an older event payload.
        transport = stripe.RequestsClient(timeout=settings.stripe_timeout_seconds)
        try:
            client = stripe.StripeClient(
                settings.stripe_api_key.get_secret_value(),
                http_client=transport,
                max_network_retries=0,
            )
            sub = client.v1.subscriptions.retrieve(subscription_id)
        finally:
            transport.close()
        if (
            sub.id != subscription_id
            or sub.livemode != settings.stripe_livemode
            or not isinstance(sub.customer, str)
        ):
            raise ValueError("Invalid Stripe subscription.")
        price_id = None
        valid_until = 0
        if sub.status == "active" and not sub.items.has_more and len(sub.items.data) == 1:
            item = sub.items.data[0]
            price_id = item.price.id
            valid_until = item.current_period_end
        state = SubscriptionState(
            customer_id=sub.customer,
            price_id=price_id,
            valid_until=valid_until,
            livemode=sub.livemode,
        ).model_dump()
        if previous != state:
            tx.put(path, state, exists=previous is not None)

    request.app.state.resource_database.run(update)


async def receive_stripe_event(request: Request):
    settings: Settings = request.app.state.settings
    try:
        event = stripe.Webhook.construct_event(
            await request.body(),
            request.headers.get("Stripe-Signature"),
            settings.stripe_webhook_secret.get_secret_value(),
            tolerance=settings.stripe_signature_tolerance_seconds,
        )
        if event.livemode != settings.stripe_livemode or event.object != "event":
            raise ValueError("Unexpected event.")
        if event.type in {"checkout.session.completed", "checkout.session.expired"}:
            try:
                await run_in_threadpool(
                    finish_checkout,
                    request,
                    event.data.object,
                    event.type == "checkout.session.completed",
                )
            except GoogleAPICallError:
                return error_response(
                    request.scope, 503, "Billing synchronization is temporarily unavailable."
                )
            return Response(status_code=204)
        if event.type not in {
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
            "customer.subscription.paused",
            "customer.subscription.resumed",
        }:
            return Response(status_code=204)
        obj = event.data.object
        if obj.object != "subscription" or not obj.id.startswith("sub_"):
            raise ValueError("Unexpected subscription.")
        subscription_id = document_segment(obj.id)
    except ValueError, AttributeError, TypeError, KeyError, stripe.SignatureVerificationError:
        return error_response(request.scope, 400, "Invalid billing notification.")
    try:
        await run_in_threadpool(reconcile, request, subscription_id)
    except stripe.StripeError, GoogleAPICallError, ValueError, AttributeError:
        return error_response(
            request.scope, 503, "Billing synchronization is temporarily unavailable."
        )
    return Response(status_code=204)


webhooks = Router(routes=[Route("/stripe", receive_stripe_event, methods=["POST"])])

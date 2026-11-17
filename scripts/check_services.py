"""Explicit smoke check against configured real Firebase, Firestore, and Redis."""

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from google.api_core.exceptions import GoogleAPICallError

from authorization_models import AuthorizationPolicy
from config import IntegrationSettings, Settings, load_settings
from main import create_app


def check_authorization_policy(app, settings: Settings) -> None:
    try:
        value = app.state.resource_database.run(
            lambda tx: tx.get(
                (settings.authorization_collections.policies, settings.authorization_policy_id)
            )
        )
        if value is None:
            raise ValueError("Policy missing")
        AuthorizationPolicy.model_validate_json(json.dumps(value, allow_nan=False))
    except GoogleAPICallError, ValueError, TypeError:
        raise RuntimeError("Real Firestore authorization policy verification failed.") from None


def main() -> None:
    settings = load_settings()
    integration = IntegrationSettings.model_validate({})
    if settings.api_environment != integration.target_environment:
        raise RuntimeError(
            "The configured API environment does not match INTEGRATION_TARGET_ENVIRONMENT."
        )
    if integration.api_origin.host not in settings.allowed_hosts:
        raise RuntimeError("INTEGRATION_API_ORIGIN must use a configured allowed host.")
    if settings.require_https and integration.api_origin.scheme != "https":
        raise RuntimeError("INTEGRATION_API_ORIGIN must use HTTPS when REQUIRE_HTTPS is true.")

    # Separate expiring counter keys; never clear or modify existing user counters.
    values = settings.model_dump()
    values["rate_limit_key_prefix"] = f"{settings.rate_limit_key_prefix}:integration:{uuid4().hex}"
    settings = Settings.model_validate(values)
    app = create_app(settings)
    with TestClient(
        app,
        base_url=str(integration.api_origin),
        client=(str(integration.client_ip), 0),
    ) as client:
        if client.get("/health/ready").status_code != 200:
            raise RuntimeError("Real-service readiness check failed.")
        check_authorization_policy(app, settings)
        response = client.get(
            "/api/v1/users/account",
            headers={"Authorization": f"Bearer {integration.id_token.get_secret_value()}"},
        )
        if response.status_code != 200 or response.json().get("uid") != integration.expected_uid:
            raise RuntimeError("Real Firebase account verification failed.")
        if client.get("/api/v1/users/account").status_code != 401:
            raise RuntimeError("Unauthenticated request was not rejected correctly.")
        response = client.get(
            "/api/v1/users/account", headers={"Authorization": "Bearer invalid-token"}
        )
        if response.status_code != 401:
            raise RuntimeError("Invalid Firebase token was not rejected correctly.")
    print("Real Firebase, Firestore policy and Redis checks passed for the selected environment.")


if __name__ == "__main__":
    main()

import ipaddress
import logging
import os
import re
from http import HTTPMethod
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyAddress,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from firestore_access import document_segment

PositiveInt = Annotated[int, Field(gt=0)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Policy(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    requests: PositiveInt
    window_seconds: PositiveInt


class AuthorizationCollections(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    resources: NonEmpty
    teams: NonEmpty
    members: NonEmpty
    grants: NonEmpty
    links: NonEmpty
    policies: NonEmpty

    @field_validator("*")
    @classmethod
    def validate_collection(cls, value: str) -> str:
        return document_segment(value)

    @model_validator(mode="after")
    def distinct_roots(self):
        if len({self.resources, self.teams, self.links, self.policies}) != 4:
            raise ValueError("Authorization root collections must be distinct.")
        return self


class ModelPlan(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    model_id: NonEmpty
    max_output_tokens: PositiveInt
    request_limit: Policy
    scope: Literal["individual", "team"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(hide_input_in_errors=True, extra="forbid")

    api_environment: NonEmpty
    api_docs_enabled: bool
    require_https: bool
    firebase_project_id: NonEmpty
    firebase_timeout_seconds: PositiveSeconds
    firestore_database_id: NonEmpty
    firestore_timeout_seconds: PositiveSeconds
    authorization_collections: AuthorizationCollections
    authorization_policy_id: NonEmpty
    authorization_link_token_bytes: Annotated[int, Field(ge=32)]
    authorization_max_parent_depth: PositiveInt
    authorization_max_batch_resources: PositiveInt
    project_resource_kind: NonEmpty
    project_create_permission: NonEmpty
    project_read_permission: NonEmpty
    project_name_max_bytes: PositiveInt
    project_sessions_collection: NonEmpty
    project_session_write_permission: NonEmpty
    project_run_permission: NonEmpty
    session_question_max_bytes: PositiveInt
    worker_context_max_bytes: PositiveInt = Field(lt=1024**2)
    model_plans: dict[NonEmpty, ModelPlan]
    free_plan_id: NonEmpty
    stripe_price_plans: dict[NonEmpty, NonEmpty]
    billing_collection: NonEmpty
    ai_runs_collection: NonEmpty
    ai_run_timeout_seconds: PositiveSeconds
    ai_answer_max_bytes: PositiveInt
    stripe_webhook_secret: SecretStr
    stripe_api_key: SecretStr
    stripe_livemode: bool
    stripe_timeout_seconds: PositiveSeconds
    stripe_success_url: AnyHttpUrl
    stripe_cancel_url: AnyHttpUrl
    stripe_signature_tolerance_seconds: PositiveInt
    rate_limit_redis_url: SecretStr
    rate_limit_key_prefix: Annotated[str, Field(min_length=1, pattern=r"^[a-zA-Z0-9:_-]+$")]
    rate_limit_timeout_seconds: PositiveSeconds
    rate_limit_policies: dict[str, Policy]
    rate_limit_default_policy: NonEmpty
    pre_auth_rate_limit: Policy
    cors_allowed_origins: list[NonEmpty]
    cors_allowed_methods: Annotated[list[NonEmpty], Field(min_length=1)]
    cors_allowed_headers: Annotated[list[NonEmpty], Field(min_length=1)]
    cors_expose_headers: list[NonEmpty]
    cors_max_age_seconds: Annotated[int, Field(ge=0)]
    allowed_hosts: Annotated[list[NonEmpty], Field(min_length=1)]
    request_max_body_bytes: PositiveInt
    request_timeout_seconds: PositiveSeconds
    caddy_upstream_timeout_seconds: PositiveSeconds
    log_level: NonEmpty

    @field_validator(
        "authorization_policy_id",
        "project_sessions_collection",
        "billing_collection",
        "ai_runs_collection",
    )
    @classmethod
    def validate_policy_id(cls, value: str) -> str:
        return document_segment(value)

    @field_validator("cors_allowed_methods")
    @classmethod
    def validate_cors_methods(cls, values: list[str]) -> list[str]:
        for value in values:
            if value not in HTTPMethod:
                raise ValueError("CORS methods must be supported HTTP method names.")
        return values

    @field_validator("cors_allowed_headers", "cors_expose_headers")
    @classmethod
    def validate_cors_headers(cls, values: list[str]) -> list[str]:
        for value in values:
            if value == "*" or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value) is None:
                raise ValueError("CORS headers must be explicit valid HTTP header names.")
        return values

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        if value not in logging.getLevelNamesMapping():
            raise ValueError("LOG_LEVEL must be a supported Python logging level.")
        return value

    @field_validator("rate_limit_redis_url")
    @classmethod
    def validate_redis_url(cls, value: SecretStr) -> SecretStr:
        url = urlsplit(value.get_secret_value())
        if url.scheme not in {"redis", "rediss"} or not url.hostname:
            raise ValueError("Use a redis:// or rediss:// URL with a hostname.")
        if url.query or url.fragment:
            raise ValueError("Redis URL query options and fragments are not supported.")
        if url.port is not None and url.port == 0:
            raise ValueError("Redis port must be positive.")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, hosts: list[str]) -> list[str]:
        for host in hosts:
            if any(character in host for character in "*/:@?# "):
                raise ValueError(
                    "Allowed hosts must be exact hostnames without ports or wildcards."
                )
            url = urlsplit(f"//{host}")
            if url.hostname != host.lower() or url.path:
                raise ValueError("Allowed hosts must be exact hostnames.")
        return [host.lower() for host in hosts]

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.billing_collection in self.authorization_collections.model_dump().values():
            raise ValueError("Use a separate root billing collection.")
        if self.ai_runs_collection in {
            *self.authorization_collections.model_dump().values(),
            self.billing_collection,
        }:
            raise ValueError("Use a separate root AI run collection.")
        if (
            self.free_plan_id not in self.model_plans
            or self.model_plans[self.free_plan_id].scope != "individual"
        ):
            raise ValueError("Configure an individual free plan.")
        if any(
            key not in self.model_plans or key == self.free_plan_id
            for key in self.stripe_price_plans.values()
        ):
            raise ValueError("Each configured Stripe Price must map to a paid plan.")
        if any(
            not key.get_secret_value().strip()
            for key in (self.stripe_api_key, self.stripe_webhook_secret)
        ):
            raise ValueError("Configure Stripe credentials.")
        if self.stripe_timeout_seconds >= self.request_timeout_seconds:
            raise ValueError("Stripe timeout must be shorter than the API timeout.")
        for url in (self.stripe_success_url, self.stripe_cancel_url):
            if url.scheme != "https" or url.username is not None or url.password is not None:
                raise ValueError(
                    "Checkout return URLs must use HTTPS without embedded credentials."
                )
        if os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"):
            raise ValueError(
                "Unset FIREBASE_AUTH_EMULATOR_HOST to use real Firebase authentication."
            )
        if os.environ.get("FIRESTORE_EMULATOR_HOST"):
            raise ValueError("Unset FIRESTORE_EMULATOR_HOST to use real Firestore.")
        if self.rate_limit_default_policy not in self.rate_limit_policies:
            raise ValueError("RATE_LIMIT_DEFAULT_POLICY must name a configured rate-limit policy.")
        for origin in self.cors_allowed_origins:
            url = urlsplit(origin)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.path
                or url.query
                or url.fragment
                or "*" in origin
                or (url.port is not None and url.port == 0)
            ):
                raise ValueError(
                    "CORS origins must be exact http(s) origins without paths or wildcards."
                )
            if self.require_https and url.scheme != "https":
                raise ValueError("CORS origins must use HTTPS when REQUIRE_HTTPS is true.")
        if self.firebase_timeout_seconds >= self.request_timeout_seconds:
            raise ValueError("Firebase timeout must be shorter than the request timeout.")
        if self.project_sessions_collection == self.authorization_collections.grants:
            raise ValueError("Session and resource grant subcollections must be distinct.")
        if self.firestore_timeout_seconds >= self.request_timeout_seconds:
            raise ValueError("Firestore timeout must be shorter than the request timeout.")
        if self.rate_limit_timeout_seconds >= self.request_timeout_seconds:
            raise ValueError("Redis timeout must be shorter than the request timeout.")
        if self.caddy_upstream_timeout_seconds <= self.request_timeout_seconds:
            raise ValueError("Caddy upstream timeout must be longer than the API request timeout.")
        return self


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERVER_", hide_input_in_errors=True)

    host: NonEmpty
    port: Annotated[int, Field(gt=0, le=65535)]
    workers: PositiveInt
    limit_concurrency: PositiveInt
    keep_alive_seconds: PositiveInt
    graceful_shutdown_seconds: PositiveInt
    trusted_proxy_ips: list[NonEmpty]

    @field_validator("trusted_proxy_ips")
    @classmethod
    def validate_proxy_ips(cls, values: list[str]) -> list[str]:
        for value in values:
            network = ipaddress.ip_network(value, strict=True)
            if network.prefixlen == 0:
                raise ValueError("Trust only the actual Caddy IP addresses or restricted networks.")
        return values


class IntegrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INTEGRATION_", hide_input_in_errors=True)

    target_environment: NonEmpty
    api_origin: AnyHttpUrl
    client_ip: IPvAnyAddress
    id_token: SecretStr
    expected_uid: NonEmpty

    @field_validator("api_origin")
    @classmethod
    def validate_api_origin(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if (
            value.username is not None
            or value.password is not None
            or value.path not in {None, "/"}
            or value.query is not None
            or value.fragment is not None
            or value.port == 0
        ):
            raise ValueError(
                "INTEGRATION_API_ORIGIN must be an exact origin without credentials or a path."
            )
        return value

    @field_validator("id_token")
    @classmethod
    def require_id_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("INTEGRATION_ID_TOKEN is required.")
        return value


def load_settings() -> Settings:
    try:
        # BaseSettings reads its configured environment sources during validation.
        return Settings.model_validate({})
    except ValidationError as error:
        fields = sorted(
            {str(item["loc"][0]) if item["loc"] else "configuration" for item in error.errors()}
        )
        raise RuntimeError(
            "Invalid API configuration. Check these settings: " + ", ".join(fields)
        ) from None
    except SettingsError:
        raise RuntimeError(
            "Invalid API configuration. Check JSON-valued environment settings."
        ) from None

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth, credentials, delete_app, initialize_app
from firebase_admin.exceptions import FirebaseError
from google.auth.exceptions import DefaultCredentialsError, TransportError

from config import Settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings

    credential = credentials.ApplicationDefault()
    try:
        credential.get_credential()
    except DefaultCredentialsError:
        raise RuntimeError(
            "Configure Google Application Default Credentials before starting the server. "
            "For local development, set GOOGLE_APPLICATION_CREDENTIALS to your service-account file."
        ) from None

    firebase_app = initialize_app(
        credential,
        options={
            "projectId": settings.firebase_project_id,
            "httpTimeout": settings.firebase_timeout_seconds,
        },
    )
    app.state.firebase_app = firebase_app
    try:
        yield
    finally:
        delete_app(firebase_app)


bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    token: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> dict:
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Please sign in to continue.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return auth.verify_id_token(
            token.credentials,
            app=request.app.state.firebase_app,
            check_revoked=True,
        )
    except (
        auth.InvalidIdTokenError,
        auth.ExpiredIdTokenError,
        auth.RevokedIdTokenError,
        auth.UserDisabledError,
        auth.UserNotFoundError,
    ):
        raise HTTPException(
            status_code=401,
            detail="Your session is no longer valid. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except auth.CertificateFetchError, FirebaseError, TransportError:
        raise HTTPException(
            status_code=503,
            detail="We couldn't check your sign-in. Please try again later.",
        ) from None


CurrentUser = Annotated[dict, Depends(get_current_user)]

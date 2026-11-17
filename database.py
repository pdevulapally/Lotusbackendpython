from contextlib import asynccontextmanager

from fastapi import FastAPI
from google.cloud.firestore_v1.services.firestore import FirestoreClient
from starlette.concurrency import run_in_threadpool

from config import Settings
from firestore_access import FirestoreAccessStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    client = FirestoreClient(credentials=app.state.firebase_app.credential.get_credential())
    store = FirestoreAccessStore(
        client,
        settings.firebase_project_id,
        settings.firestore_database_id,
        settings.firestore_timeout_seconds,
    )
    app.state.resource_database = store
    try:
        yield
    finally:
        await run_in_threadpool(store.close)

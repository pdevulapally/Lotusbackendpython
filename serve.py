import uvicorn

from config import ServerSettings, load_settings


def main() -> None:
    # Validate application and server settings before spawning worker processes.
    load_settings()
    settings = ServerSettings.model_validate({})
    uvicorn.run(
        "main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        limit_concurrency=settings.limit_concurrency,
        timeout_keep_alive=settings.keep_alive_seconds,
        timeout_graceful_shutdown=settings.graceful_shutdown_seconds,
        proxy_headers=True,
        forwarded_allow_ips=",".join(settings.trusted_proxy_ips),
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()

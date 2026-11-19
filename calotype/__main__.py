"""Start the API: ``python -m calotype``. Listens on $PORT (default 8080)."""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "calotype.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        access_log=False,  # the app logs requests itself, without query strings
        # Client addresses are read only for the in-memory key-creation limit;
        # see CALOTYPE_TRUSTED_PROXY_HOPS in ratelimit.client_ip.
        proxy_headers=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()

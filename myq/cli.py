"""Command line interface.

    python -m myq.cli login          # one-time browser login, saves tokens locally
    python -m myq.cli push-tokens    # upload local tokens to AWS SSM
    python -m myq.cli pull-tokens    # download SSM tokens to the local file
    python -m myq.cli status         # show stored token state
    python -m myq.cli test           # hit MyQ and print the door state
    python -m myq.cli serve          # run the API locally
    python -m myq.cli open|close     # send a command directly
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from .client import MyQClient
from .config import get_settings
from .errors import MyQError
from .tokens import FileTokenStore, SSMTokenStore, build_token_store


def _file_store():
    return FileTokenStore(get_settings().tokens_file)


def _ssm_store():
    settings = get_settings()
    return SSMTokenStore(settings.ssm_parameter, settings.aws_region)


def cmd_login(args) -> int:
    """Log in, headlessly by default and via the browser on request."""
    from .login import InvalidCredentials, LoginBlocked, automatic_login, interactive_login

    settings = get_settings()
    store = _file_store()

    if getattr(args, "browser", False):
        tokens = interactive_login(store)
    else:
        try:
            tokens = automatic_login(settings.myq_email, settings.myq_password, store)
        except InvalidCredentials as exc:
            # Wrong password is worth stating plainly — falling back to the
            # browser would just fail again, more slowly.
            print(f"\n{exc.message}", file=sys.stderr)
            print(
                "\nCheck MYQ_EMAIL and MYQ_PASSWORD in .env. If you can sign in at\n"
                "https://account.myq-cloud.com but not here, run:\n"
                "    python -m myq.cli login --browser",
                file=sys.stderr,
            )
            return 1
        except LoginBlocked as exc:
            print(f"\n{exc.message}", file=sys.stderr)
            print("\nFalling back to browser login...\n")
            tokens = interactive_login(store)

    print()
    print("Login successful. Tokens saved locally.")
    print(f"  account_id:    {tokens.account_id}")
    print(f"  device_serial: {tokens.device_serial}")
    return 0


def cmd_setup(_args) -> int:
    """Log in and upload the tokens in one step — the whole first-run flow."""
    rc = cmd_login(_args)
    if rc != 0:
        return rc
    print("Uploading tokens to AWS...")
    return cmd_push_tokens(_args)


def cmd_status(_args) -> int:
    settings = get_settings()
    store = build_token_store(settings)
    tokens = store.load()
    print(f"Token store:  {store.location}")
    if not tokens:
        print("Tokens:       NONE — run 'python -m myq.cli login'")
        return 1
    remaining = int(tokens.expires_at - time.time())
    print(f"Access token: present")
    print(f"Refresh token:{' present' if tokens.refresh_token else ' MISSING'}")
    print(f"Expires:      {remaining}s ({'expired' if remaining <= 0 else 'valid'})")
    print(f"Account ID:   {tokens.account_id or 'not set'}")
    print(f"Door serial:  {tokens.device_serial or 'not set'}")
    print(f"API key set:  {'yes' if settings.api_key else 'NO — API will refuse requests'}")
    return 0


def cmd_push_tokens(_args) -> int:
    tokens = _file_store().load()
    if not tokens:
        print("No local tokens to push. Run 'python -m myq.cli login' first.", file=sys.stderr)
        return 1
    store = _ssm_store()
    store.save(tokens)
    print(f"Pushed tokens to {store.location}")
    print(f"  account_id={tokens.account_id} device_serial={tokens.device_serial}")
    return 0


def cmd_pull_tokens(_args) -> int:
    tokens = _ssm_store().load()
    if not tokens:
        print("No tokens found in SSM.", file=sys.stderr)
        return 1
    _file_store().save(tokens)
    print(f"Wrote SSM tokens to {get_settings().tokens_file}")
    return 0


async def _with_client(fn):
    settings = get_settings()
    client = MyQClient(build_token_store(settings), settings)
    try:
        return await fn(client)
    finally:
        await client.close()


def cmd_test(_args) -> int:
    async def run(client: MyQClient):
        doors = await client.list_doors()
        if not doors:
            print("No garage doors on this account.")
            return 1
        for door in doors:
            online = "online" if door.online else "OFFLINE"
            print(f"{door.name}: {door.state} ({online}) serial={door.serial_number}")
        print("\nMyQ connection is working.")
        return 0

    return _run(run)


def cmd_command(args) -> int:
    async def run(client: MyQClient):
        before = await client.send_command(args.action, args.serial)
        print(f"Sent '{args.action}' to {before.name} (was {before.state})")
        if args.wait:
            target = "open" if args.action == "open" else "closed"
            final = await client.wait_for_state(target, args.serial)
            print(f"{final.name} is now {final.state}")
        return 0

    return _run(run)


def cmd_serve(_args) -> int:
    import uvicorn

    settings = get_settings()
    if not settings.api_key:
        print("WARNING: API_KEY is not set — protected endpoints will return 503.\n")
    print(f"MyQ API on http://{settings.host}:{settings.port}  (docs at /docs)")
    uvicorn.run("myq.api:app", host=settings.host, port=settings.port, reload=False)
    return 0


def _run(fn) -> int:
    try:
        return asyncio.run(_with_client(fn)) or 0
    except MyQError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"  {exc.detail}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="myq", description="MyQ garage door controller")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="Log in and upload tokens to AWS (first-run)")
    p_setup.add_argument("--browser", action="store_true", help="Use the browser flow")
    p_setup.set_defaults(fn=cmd_setup)

    p_login = sub.add_parser("login", help="Log in to MyQ and save tokens locally")
    p_login.add_argument(
        "--browser",
        action="store_true",
        help="Sign in via a real browser (needed for MFA/SSO accounts)",
    )
    p_login.set_defaults(fn=cmd_login)
    sub.add_parser("status", help="Show stored token state").set_defaults(fn=cmd_status)
    sub.add_parser("push-tokens", help="Upload local tokens to SSM").set_defaults(fn=cmd_push_tokens)
    sub.add_parser("pull-tokens", help="Download tokens from SSM").set_defaults(fn=cmd_pull_tokens)
    sub.add_parser("test", help="Print door state from MyQ").set_defaults(fn=cmd_test)
    sub.add_parser("serve", help="Run the API locally").set_defaults(fn=cmd_serve)

    for action in ("open", "close"):
        p = sub.add_parser(action, help=f"{action.capitalize()} the door")
        p.add_argument("--wait", action="store_true", help="Wait for the door to finish moving")
        p.add_argument("--serial", default=None, help="Target a specific door")
        p.set_defaults(fn=cmd_command, action=action)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

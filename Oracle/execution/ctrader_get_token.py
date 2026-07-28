"""
Oracle cTrader — One-Time Access Token Setup
===============================================
Run this ONCE (locally or on your VPS, wherever you have a web browser
available or can copy a URL into one) to exchange your app's Client ID /
Client Secret for an access token + account ID, then hand those to
CTraderBroker via environment variables.

This follows the same OAuth flow as Spotware's own official sample
(github.com/spotware/OpenApiPy/samples/ConsoleSample) — this script is
just a friendlier, narrower version of that flow, focused only on getting
the token, not exercising every API command.

Prerequisites:
  pip install ctrader-open-api --break-system-packages

  NOTE: If you see an `AttributeError` from `OpenSSL.crypto` on first run
  (e.g., `module 'lib' has no attribute 'GEN_EMAIL'`), it's likely due to
  an incompatible version of the `cryptography` package. This can typically
  be fixed by running:
    `pip install --upgrade --force-reinstall cryptography pyOpenSSL`


Before running, make sure your app on connect.spotware.com has a
Redirect URI registered — for a personal/sandbox setup, something like
http://localhost is standard (check what you registered under app "Universe").

Usage:
    python ctrader_get_token.py

What it does:
  1. Asks for your Client ID, Client Secret, and Redirect URI.
  2. Builds the authorization URL and asks you to open it in a browser,
     log in with your cTrader ID, and approve access for your demo account.
  3. cTrader redirects you to your Redirect URI with a ?code=... in the URL
     (the page itself may show an error/blank — that's fine, the code is
     what matters, it's right there in the address bar).
  4. You paste that code back here; the script exchanges it for an access
     token (and refresh token) and fetches your account list so you can
     confirm the right account ID (471086545, per this conversation).
  5. Prints the exact environment variables to set before running the bot.

IMPORTANT — testing limitation (same as ctrader_broker.py): this sandbox
has no network access and cannot install ctrader-open-api, so this script
has not been run end-to-end. The flow itself mirrors Spotware's own sample
closely, but the first real run may need adjustment (e.g. if your
Redirect URI doesn't match what's registered for the app, step 2 will
show an error from cTrader's side — that's a config fix on
connect.spotware.com, not a bug in this script).
"""
from __future__ import annotations

import sys
import webbrowser


def main() -> None:
    try:
        from ctrader_open_api import Auth
    except ImportError:
        print("ctrader-open-api is not installed here.")
        print("Run this script on your VPS (or wherever you'll run the bot), after:")
        print("    pip install ctrader-open-api --break-system-packages")
        sys.exit(1)

    print("=" * 60)
    print(" cTrader Open API — Access Token Setup")
    print("=" * 60)

    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()
    redirect_uri = input("Redirect URI (as registered for app 'Universe', "
                          "e.g. http://localhost): ").strip()

    auth = Auth(client_id, client_secret, redirect_uri)
    auth_uri = auth.getAuthUri()

    print("\nOpening this URL in your browser (or copy/paste it manually):")
    print(f"  {auth_uri}\n")
    try:
        webbrowser.open_new(auth_uri)
    except Exception:
        pass  # fine if there's no browser available on this machine — just paste the URL manually

    print("Log in with your cTrader ID and approve access for account 471086545.")
    print("You'll be redirected to your Redirect URI with '?code=...' in the address bar.")
    print("The page itself may look blank or show an error — that's expected;")
    print("the code in the URL is all we need.\n")

    auth_code = input("Paste the code (the part after '?code=' in the redirected URL): ").strip()

    token = auth.getToken(auth_code)
    if "accessToken" not in token:
        print("\nSomething went wrong exchanging the code for a token:")
        print(token)
        sys.exit(1)

    access_token = token["accessToken"]
    refresh_token = token.get("refreshToken", "")

    print("\n✅ Got an access token.\n")

    # Fetch the account list tied to this token so the user can confirm
    # which ctidTraderAccountId corresponds to account 471086545.
    _print_account_list(client_id, client_secret, access_token)

    print("\nSet these environment variables before running the bot:")
    print(f"  export CTRADER_CLIENT_ID='{client_id}'")
    print(f"  export CTRADER_CLIENT_SECRET='{client_secret}'")
    print(f"  export CTRADER_ACCESS_TOKEN='{access_token}'")
    print("  export CTRADER_ACCOUNT_ID='<the ctidTraderAccountId matching 471086545 above>'")
    if refresh_token:
        print(f"\n(Refresh token, for later renewal — keep this private too: {refresh_token})")


def _print_account_list(client_id: str, client_secret: str, access_token: str) -> None:
    """
    Connects briefly just to call ProtoOAGetAccountListByAccessTokenReq and
    print the account IDs available to this token, so the user can match
    471086545 (the account number shown in the Pepperstone UI) to its
    ctidTraderAccountId (the numeric ID the Open API actually uses).
    """
    try:
        from ctrader_open_api import Client, TcpProtocol, EndPoints
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAApplicationAuthReq, ProtoOAGetAccountListByAccessTokenReq,
            ProtoOAApplicationAuthRes, ProtoOAGetAccountListByAccessTokenRes,
        )
        from ctrader_open_api import Protobuf
        from twisted.internet import reactor
        import threading
    except ImportError as exc:
        print(f"(Could not fetch account list automatically: {exc})")
        print("You can find your ctidTraderAccountId in the cTrader platform's account settings instead.")
        return

    done = threading.Event()
    accounts = []

    def on_connected(client):
        req = ProtoOAApplicationAuthReq()
        req.clientId = client_id
        req.clientSecret = client_secret
        client.send(req)

    def on_message(client, message):
        if message.payloadType == ProtoOAApplicationAuthRes().payloadType:
            req = ProtoOAGetAccountListByAccessTokenReq()
            req.accessToken = access_token
            client.send(req)
        elif message.payloadType == ProtoOAGetAccountListByAccessTokenRes().payloadType:
            res = Protobuf.extract(message)
            for acc in res.ctidTraderAccount:
                accounts.append({
                    "ctidTraderAccountId": acc.ctidTraderAccountId,
                    "traderLogin": acc.traderLogin,
                    "isLive": acc.isLive,
                })
            done.set()

    client = Client(EndPoints.PROTOBUF_DEMO_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)
    client.setConnectedCallback(on_connected)
    client.setMessageReceivedCallback(on_message)
    client.startService()

    reactor_thread = threading.Thread(
        target=lambda: reactor.run(installSignalHandlers=False), daemon=True)
    reactor_thread.start()

    if not done.wait(timeout=15):
        print("(Account list lookup timed out — you can find your ctidTraderAccountId "
              "in the cTrader platform's account settings instead.)")
        reactor.callFromThread(reactor.stop)
        return

    print("Accounts available to this token:")
    for acc in accounts:
        marker = " <- traderLogin matches 471086545" if acc["traderLogin"] == 471086545 else ""
        print(f"  ctidTraderAccountId={acc['ctidTraderAccountId']}  "
              f"traderLogin={acc['traderLogin']}  live={acc['isLive']}{marker}")

    reactor.callFromThread(reactor.stop)


if __name__ == "__main__":
    main()
"""Send run commands to the Multi-agent Gateway."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("MAF_GATEWAY_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--timeout", type=float, default=600)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run")
    run.add_argument("request")
    run.add_argument("--project", required=True)
    for action in ("status", "replay", "approve", "reject", "spec", "continue", "stop"):
        command = commands.add_parser(action)
        command.add_argument("run_id")
        if action == "replay":
            command.add_argument("--after", type=int, default=0)
        if action in ("approve", "reject", "continue", "stop"):
            command.add_argument("--feedback", default="")
        if action == "spec":
            command.add_argument("request")
        if action not in ("status", "replay"):
            command.add_argument("--gate-id")
    args = parser.parse_args()

    token = os.environ.get("MAF_GATEWAY_TOKEN", "").strip()
    if not token:
        parser.error("Set MAF_GATEWAY_TOKEN in .env or in the environment")

    url = args.url.rstrip("/") + "/runs"
    body = None
    if args.action == "run":
        body = {"request": args.request, "project_id": args.project}
    else:
        url += "/" + quote(args.run_id, safe="")
        if args.action == "replay":
            url += "/events?" + urlencode({"after": args.after})
        elif args.action != "status":
            url += "/" + args.action
            body = {"gate_id": args.gate_id}
            if args.action == "spec":
                body["request"] = args.request
            else:
                body["feedback"] = args.feedback

    request = Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=args.timeout) as response:
            print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
    except HTTPError as error:
        print(f"Gateway HTTP {error.code}: {error.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError) as error:
        print(f"Gateway connection error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

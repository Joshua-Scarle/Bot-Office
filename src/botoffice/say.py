"""Post a job-scoped progress note from desk or bot-office-say."""
import json
import os
import sys
import urllib.error
import urllib.request


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    try:
        token = os.environ.get("BOT_OFFICE_AGENT_TOKEN")
        url = os.environ.get("BOT_OFFICE_API_URL")
        if not token or not url or not os.environ.get("BOT_OFFICE_BOT_ID"):
            raise ValueError("BOT_OFFICE_AGENT_TOKEN, BOT_OFFICE_API_URL and BOT_OFFICE_BOT_ID are required")
        if not args:
            raise ValueError("supply TEXT or - for stdin")
        text = sys.stdin.read() if args == ["-"] else " ".join(args)
        request = urllib.request.Request(url.rstrip("/") + "/v1/agent/say",
                                         data=json.dumps({"text": text}).encode(),
                                         headers={"Authorization": "Bearer " + token,
                                                  "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            item = json.load(response)
        print(f"Progress note {item['id']}")
        return 0
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("detail", str(exc))
        except (ValueError, AttributeError):
            detail = str(exc)
        print(f"bot-office-say: {detail}", file=sys.stderr)
        return 1
    except (ValueError, OSError, urllib.error.URLError) as exc:
        print(f"bot-office-say: {exc}", file=sys.stderr)
        return 1

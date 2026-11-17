"""Job-scoped question helper, shared by desk and bot-office-ask."""
import json
import os
import sys
import time
import urllib.error
import urllib.request


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    no_wait = "--no-wait" in args
    if no_wait:
        args.remove("--no-wait")
    try:
        token = os.environ.get("BOT_OFFICE_AGENT_TOKEN")
        url = os.environ.get("BOT_OFFICE_API_URL")
        if not token or not url or not os.environ.get("BOT_OFFICE_BOT_ID"):
            raise ValueError("BOT_OFFICE_AGENT_TOKEN, BOT_OFFICE_API_URL and "
                             "BOT_OFFICE_BOT_ID are required")
        if len(args) > 1:
            raise ValueError("supply one JSON object on argv or stdin")
        payload = json.loads(args[0] if args else sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("question must be a JSON object")
        payload["blocking"] = not no_wait
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

        def call(path, data=None):
            request = urllib.request.Request(url.rstrip("/") + path, headers=headers,
                                             data=(json.dumps(data).encode()
                                                   if data is not None else None))
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)

        item = call("/v1/agent/ask", payload)
        if no_wait:
            print(json.dumps({"id": item["id"]}))
            return 0
        while item["status"] == "open":
            time.sleep(0.5)
            item = call("/v1/agent/questions/" + item["id"])
        print(json.dumps({"id": item["id"], "status": item["status"],
                          "skipped": item["status"] == "skipped", "answer": item["answer"]}))
        return 0
    except (ValueError, OSError, urllib.error.URLError) as exc:
        print(f"bot-office-ask: {exc}", file=sys.stderr)
        return 1

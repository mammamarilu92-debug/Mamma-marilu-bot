#!/usr/bin/env python3
"""Script helper: crea shortlink PostTap e stampa il risultato su stdout.
Usato dal proxy Mastra (Node.js) che chiama Python perché httpx funziona."""
import sys, os, httpx, json

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "missing url"}))
        sys.exit(1)

    amazon_url = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else "link"

    # Leggi cookie dal file
    cookies_file = os.path.join(os.path.dirname(__file__), "posttap_cookies.txt")
    try:
        with open(cookies_file) as f:
            cookie_str = f.read().strip()
    except Exception:
        cookie_str = os.getenv("POSTTAP_COOKIES", "")

    if not cookie_str:
        print(json.dumps({"error": "no_cookies"}))
        sys.exit(1)

    try:
        r = httpx.post(
            "https://creators.posttap.com/api/create-shortlink",
            json={"name": name, "url": amazon_url, "tags": []},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://creators.posttap.com",
                "Referer": "https://creators.posttap.com/dashboard",
                "Cookie": cookie_str,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            },
            timeout=15,
            follow_redirects=True,
        )
        if r.status_code in [200, 201]:
            data = r.json()
            obj = data.get("object", {})
            shortlink = obj.get("shortlink") or obj.get("shortLink") or data.get("shortlink")
            if shortlink:
                print(json.dumps({"shortlink": shortlink}))
                sys.exit(0)
        print(json.dumps({"error": f"status_{r.status_code}", "body": r.text[:200]}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()

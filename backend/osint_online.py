"""
Opt-in ONLINE OSINT enrichment (network calls, public sources only):

  * ip_geolocation(ip)  -> approximate location of an IP (ip-api.com)
  * username_scan(user) -> public presence of a username across sites

Boundary: this is public-record OSINT. IP geolocation is the location of the
*network/ISP*, not a person, and is city-level at best. Username scanning only
checks whether a public profile URL exists. Nothing here derives a person's live
GPS, and there is deliberately no phishing/consent-bypass path (unlike Seeker).

Uses only the stdlib (urllib) so no extra dependencies. Every call is best-effort
and fails soft: network errors become a status field, never an exception upward.
"""

import concurrent.futures
import json
import urllib.error
import urllib.request
from typing import Any

_UA = "Mozilla/5.0 (compatible; OSINT-MVP-Research/0.1)"


def _http_get(url: str, timeout: float = 4.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.getcode(), resp.read()


# --- IP geolocation ---------------------------------------------------------
def ip_geolocation(ip: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "ip", "input": ip,
        "source": "ip-api.com", "source_kind": "online_api",
        "note": ("Location of the IP/ISP, approximate (city-level). It is the "
                 "network's location, not a person's, and not a live GPS fix."),
    }
    fields = ("status,message,country,countryCode,regionName,city,zip,lat,lon,"
              "timezone,isp,org,as,proxy,hosting,mobile,query")
    try:
        code, body = _http_get(f"http://ip-api.com/json/{ip}?fields={fields}")
        data = json.loads(body)
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        return result

    if data.get("status") != "success":
        result["status"] = "fail"
        result["error"] = data.get("message", "lookup failed (private/reserved IP?)")
        return result

    result.update({
        "status": "success",
        "country": data.get("country"), "country_code": data.get("countryCode"),
        "region": data.get("regionName"), "city": data.get("city"),
        "zip": data.get("zip"), "latitude": data.get("lat"),
        "longitude": data.get("lon"), "timezone": data.get("timezone"),
        "isp": data.get("isp"), "org": data.get("org"), "asn": data.get("as"),
        "is_proxy": data.get("proxy"), "is_hosting": data.get("hosting"),
        "is_mobile": data.get("mobile"),
    })
    return result


# --- Username scan ----------------------------------------------------------
# Sites whose HTTP status is a reliable existence signal (200 exists / 404 not).
_CHECKABLE = {
    "GitHub": "https://github.com/{u}",
    "GitLab": "https://gitlab.com/{u}",
    "Reddit": "https://www.reddit.com/user/{u}/about.json",
    "Dev.to": "https://dev.to/{u}",
    "Pinterest": "https://www.pinterest.com/{u}/",
    "Replit": "https://replit.com/@{u}",
}
# Sites that block server-side existence checks (login/JS walls) -> link only.
_MANUAL = {
    "Instagram": "https://instagram.com/{u}",
    "X (Twitter)": "https://x.com/{u}",
    "TikTok": "https://www.tiktok.com/@{u}",
    "Facebook": "https://facebook.com/{u}",
    "YouTube": "https://www.youtube.com/@{u}",
}


def _check_site(name: str, url: str) -> dict[str, Any]:
    try:
        code, _ = _http_get(url, timeout=5.0)
        status = "found" if code == 200 else "unknown"
    except urllib.error.HTTPError as exc:
        status = "not_found" if exc.code == 404 else "unknown"
    except (urllib.error.URLError, TimeoutError, ValueError):
        status = "unknown"
    return {"site": name, "url": url, "status": status}


def username_scan(username: str) -> dict[str, Any]:
    u = username.lstrip("@")
    results: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_check_site, name, tmpl.format(u=u))
                   for name, tmpl in _CHECKABLE.items()]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    for name, tmpl in _MANUAL.items():
        results.append({"site": name, "url": tmpl.format(u=u), "status": "manual"})

    results.sort(key=lambda r: {"found": 0, "manual": 1, "unknown": 2,
                                "not_found": 3}[r["status"]])
    found = sum(1 for r in results if r["status"] == "found")
    return {
        "type": "username", "input": username, "normalized": u,
        "source": "public profile probes", "source_kind": "online_api",
        "found_count": found, "results": results,
        "note": ("Existence check of public profile URLs only. 'manual' sites "
                 "block server-side checks - open the link to verify."),
    }

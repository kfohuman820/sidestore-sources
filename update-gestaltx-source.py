#!/usr/bin/env python3
"""Regenerate GestaltX-source.json for SideStore, newest releases first.

Why tags and not only releases:
    Upstream sometimes tags a version and writes its changelog BEFORE the IPA
    asset is uploaded (v1.0.6 and v1.0.8 both did this). The releases API then
    does not list the version at all, so a releases-only updater would miss it.
    We therefore walk the tag list and probe the canonical download URL with a
    HEAD request - the version becomes available the moment its IPA is uploaded,
    even if the release object has not been published yet.

Output is the SideStore "source" JSON schema:

    { "apps": [ { "name": ..., "bundleID": ..., "versions": [ {version,url,date} ] } ] }

SideStore polls this file, sees a newer version of an installed app, and
re-signs + installs it by itself.

Usage:  python update-gestaltx-source.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = "huamidev/GestaltX"
API = f"https://api.github.com/repos/{REPO}"
BUNDLE_ID = "com.huami.gestultx"
MAX_VERSIONS = 10
UA = {"User-Agent": "sidestore-gestaltx-source-updater"}

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "GestaltX-source.json")


def get_json(url):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def version_key(ver):
    parts = []
    for chunk in ver.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def probe(url, attempts=3, pause=2.0):
    """HEAD the direct download URL.

    Returns:
        (length, None)  -> asset is live
        (None, "404")   -> asset genuinely not published yet
        (None, reason)  -> transient / unknown error, must NOT be treated as "absent"
    """
    last = "unknown"
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                length = resp.headers.get("Content-Length")
                return (int(length) if length and length.isdigit() else 0), None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "404"
            last = f"HTTP {e.code}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < attempts:
            time.sleep(pause)
    return None, last


def main():
    print(f"reading tags from {REPO} ...")
    tags = [t["name"].lstrip("vV") for t in get_json(f"{API}/tags")]
    tags.sort(key=version_key, reverse=True)

    print("reading release metadata ...")
    releases = {}
    try:
        for rel in get_json(f"{API}/releases"):
            ver = (rel.get("tag_name") or "").lstrip("vV")
            ipa = None
            zip_asset = None
            for a in rel.get("assets", []):
                name = a.get("name", "").lower()
                if name.endswith(".ipa") and ipa is None:
                    ipa = a
                elif name.endswith(".zip") and zip_asset is None:
                    zip_asset = a
            releases[ver] = {
                "date": (rel.get("published_at") or "")[:10],
                "ipa": ipa,
                "zip": zip_asset,
            }
    except Exception as exc:  # metadata is a nice-to-have, never fatal
        print(f"  (release metadata unavailable: {exc})")

    versions = []
    unknown = []
    zip_only = []
    for ver in tags:
        meta = releases.get(ver, {})
        canonical = f"https://github.com/{REPO}/releases/download/v{ver}/GestaltX_{ver}.ipa"

        # Preferred: a real .ipa asset on the release (its URL may differ from the
        # canonical one, e.g. when the release was re-uploaded).
        asset = meta.get("ipa")
        url = asset["browser_download_url"] if asset else canonical

        size, reason = probe(url)
        if reason == "404":
            if meta.get("zip") and not meta.get("ipa"):
                zip_only.append(ver)
                print(f"  v{ver:7s} published as .zip only (no .ipa asset) - skipped for SideStore")
            else:
                print(f"  v{ver:7s} IPA not published yet (or release withdrawn) - skipped")
            continue
        if reason is not None:
            unknown.append((ver, reason))
            print(f"  v{ver:7s} probe inconclusive ({reason}) - skipped, will retry next run")
            continue

        versions.append({"version": ver, "url": url, "date": meta.get("date", "")})
        print(f"  v{ver:7s} available ({size} bytes)")
        if len(versions) >= MAX_VERSIONS:
            break

    if not versions:
        raise SystemExit("no downloadable GestaltX IPA found - refusing to write an empty catalog")

    catalog = {"apps": [{"name": "GestaltX", "bundleID": BUNDLE_ID, "versions": versions}]}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nwrote {OUT}")
    print(f"{len(versions)} version(s); newest = {versions[0]['version']}")
    if zip_only:
        print("note: zip-only upstream (SideStore needs a direct .ipa) -> " + ", ".join(zip_only))
    if unknown:
        print("note: inconclusive probes -> " + ", ".join(f"v{v}({r})" for v, r in unknown))
        print("      (those versions may already be published; the next run will pick them up)")


if __name__ == "__main__":
    # Keep the console's own code page (936 on zh-CN); this script prints ASCII
    # only, but the workflow log must never explode on encoding.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    main()

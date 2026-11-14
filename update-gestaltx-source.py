#!/usr/bin/env python3
"""Regenerate the SideStore source catalog for GestaltX.

The JSON format is NOT the AltStore-style {"apps":[...]} wrapper. SideStore
decodes the file straight into its `Source` CoreData entity
(AltStore/Core/Model/Source.swift -> init(from:)), so the schema is:

    Source   (root)      : name REQUIRED, version?, subtitle?, websiteURL?,
                            localizedDescription?, iconURL?, headerImageURL?,
                            patreonURL?, tintColor?, userInfo?, groupID?,
                            featuredApps?, news?, apps?
    StoreApp (in "apps") : name REQUIRED, bundleIdentifier REQUIRED,
                            developerName REQUIRED, localizedDescription REQUIRED,
                            iconURL REQUIRED, versions[...],
                            subtitle?, marketplaceID?, tintColor?, category?
    AppVersion           : version REQUIRED, date REQUIRED, downloadURL REQUIRED,
                            size REQUIRED, buildVersion?, sha256?,
                            localizedDescription?, minOSVersion?, maxOSVersion?

Note: "bundleIdentifier" (not "bundleID") and "downloadURL" (not "url"),
and "size" is mandatory.

Why tags and not only releases:
    Upstream sometimes tags a version and writes its changelog BEFORE the IPA
    asset is uploaded (v1.0.6 / v1.0.8 / v1.0.9 all did this). We therefore
    walk the tag list AND the release list, then probe the real asset URL.

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
APP_NAME = "GestaltX"
DEVELOPER = "huamidev"
DESCRIPTION = "GestaltX - on-device MobileGestalt editing"
ICON_URL = "https://github.com/huamidev.png"
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
    """HEAD a URL.

    Returns (length, None)      -> live
            (None, "404")       -> genuinely absent
            (None, reason)      -> transient error, must NOT be treated as absent
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


def validate(catalog):
    """Fail loudly if the catalog does not match what SideStore can decode."""
    assert catalog.get("name"), "root 'name' is required by SideStore"
    apps = catalog.get("apps")
    assert isinstance(apps, list) and apps, "'apps' must be a non-empty array"
    for app in apps:
        for key in ("name", "bundleIdentifier", "developerName",
                    "localizedDescription", "iconURL"):
            assert app.get(key), f"app '{app.get('bundleIdentifier')}' missing '{key}'"
        assert isinstance(app.get("versions"), list) and app["versions"], \
            f"app '{app['bundleIdentifier']}' needs a non-empty 'versions' array"
        for v in app["versions"]:
            for key in ("version", "date", "downloadURL", "size"):
                assert v.get(key) not in (None, ""), \
                    f"version {v.get('version')} missing '{key}'"
            assert isinstance(v["size"], int), f"version {v['version']} size must be an int"
            assert v["downloadURL"].startswith("http"), \
                f"version {v['version']} downloadURL must be http(s)"
    return True


def main():
    print(f"reading tags from {REPO} ...")
    tags = [t["name"].lstrip("vV") for t in get_json(f"{API}/tags")]

    print("reading releases ...")
    releases = {}
    try:
        for rel in get_json(f"{API}/releases"):
            if rel.get("draft") or rel.get("prerelease"):
                continue
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
    except Exception as exc:
        print(f"  (release metadata unavailable: {exc})")

    # union of tags and released versions, newest first
    candidates = sorted(set(tags) | set(releases), key=version_key, reverse=True)

    versions = []
    unknown = []
    zip_only = []
    for ver in candidates:
        meta = releases.get(ver, {})
        asset = meta.get("ipa")
        url = asset["browser_download_url"] if asset else \
            f"https://github.com/{REPO}/releases/download/v{ver}/GestaltX_{ver}.ipa"

        size, reason = probe(url)
        if reason == "404":
            if meta.get("zip") and not meta.get("ipa"):
                zip_only.append(ver)
                print(f"  v{ver:7s} published as .zip only - skipped (SideStore needs a direct .ipa)")
            else:
                print(f"  v{ver:7s} no IPA published yet (or release withdrawn) - skipped")
            continue
        if reason is not None:
            unknown.append((ver, reason))
            print(f"  v{ver:7s} probe inconclusive ({reason}) - skipped, retried next run")
            continue

        versions.append({
            "version": ver,
            "date": meta.get("date", ""),
            "downloadURL": url,
            "size": size,
            **({"sha256": (asset.get("digest") or "").replace("sha256:", "").strip()}
               if asset and asset.get("digest") else {}),
        })
        print(f"  v{ver:7s} available ({size} bytes)")
        if len(versions) >= MAX_VERSIONS:
            break

    if not versions:
        raise SystemExit("no downloadable GestaltX IPA found - refusing to write an empty catalog")

    catalog = {
        "name": "GestaltX",
        "version": 1,
        "apps": [
            {
                "name": APP_NAME,
                "bundleIdentifier": BUNDLE_ID,
                "developerName": DEVELOPER,
                "localizedDescription": DESCRIPTION,
                "iconURL": ICON_URL,
                "versions": versions,
            }
        ],
    }

    validate(catalog)

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nwrote {OUT}")
    print(f"{len(versions)} version(s); newest = {versions[0]['version']}  (schema validated)")
    if zip_only:
        print("note: zip-only upstream -> " + ", ".join(zip_only))
    if unknown:
        print("note: inconclusive probes -> " + ", ".join(f"v{v}({r})" for v, r in unknown))


if __name__ == "__main__":
    # Keep the console's own code page (936 on zh-CN); the workflow log must
    # never explode on encoding.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    main()

"""
Builds the authoritative per-version command catalogue for Relic (characters, 4/5-star weapons,
4/5-star artifact sets) straight from the game's own ExcelBinOutput, plus the matching UI icons.

Why this exists: hand-typed id tables drift and silently send the wrong item (the app previously
shipped 23300 as the artifact example -- that is a ONE-star flower, not a 5-star piece). Everything
here is derived from the real game data instead.

Version accuracy comes from Dimbreath/AnimeGameData's history: each supported version is pinned to
the commit that actually carries that build's data, so "what existed in 1.6" is a fact, not a guess.
    1.6 -> 72c9112a7c5e  "game_1.5.1_1.6.0_diff"
    2.8 -> d56ed231c451  "OSRELWin2.8.0_R8078355_S8017153_D8078038"

Icons come from Enka's UI mirror (the game's own UI_* sprites); the game's local .blk archives are
MiHoYo-encrypted and not extractable.

Usage (from the repo root):
    python build/make_catalog.py                 # data + icons
    python build/make_catalog.py --no-icons      # data only (fast)
"""
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(REPO, "build", "_gamedata_cache")
ICON_DIR = os.path.join(REPO, "app", "Relic.App", "ui", "assets", "icons")
OUT_JSON = os.path.join(REPO, "config", "gamedata.generated.json")

RAW = "https://gitlab.com/Dimbreath/AnimeGameData/-/raw/{commit}/{path}"
ICON_URL = "https://enka.network/ui/{icon}.png"

# Ordered oldest -> newest; an item's "since" is the FIRST version it appears in.
VERSIONS = [("1.6", "72c9112a7c5e"), ("2.8", "d56ed231c451")]

NEEDED = [
    "ExcelBinOutput/WeaponExcelConfigData.json",
    "ExcelBinOutput/ReliquaryExcelConfigData.json",
    "ExcelBinOutput/ReliquarySetExcelConfigData.json",
    "ExcelBinOutput/EquipAffixExcelConfigData.json",
    "ExcelBinOutput/AvatarExcelConfigData.json",
    "TextMap/TextMapEN.json",
]

WEAPON_TYPE = {
    "WEAPON_SWORD_ONE_HAND": "Sword",
    "WEAPON_CLAYMORE": "Claymore",
    "WEAPON_POLE": "Polearm",
    "WEAPON_CATALYST": "Catalyst",
    "WEAPON_BOW": "Bow",
}
SLOT = {
    "EQUIP_BRACER": "flower",
    "EQUIP_NECKLACE": "plume",
    "EQUIP_SHOES": "sands",
    "EQUIP_RING": "goblet",
    "EQUIP_DRESS": "circlet",
}
SLOT_ORDER = ["flower", "plume", "sands", "goblet", "circlet"]

# The low-tier family (setId < 14000) also carries the 3-star-max sets (Adventurer, Lucky Dog,
# Traveling Doctor). Their rank-4 rows exist in the tables but never drop in game at that rarity,
# so the 4-star picker lists only the sets a player actually knows as 4-star.
SETS_3STAR_MAX = {10010, 10011, 10013}

# Quest-only souvenirs the icon heuristic cannot catch (they own their icon): 11420 is "Prized
# Isshin Blade", the shattered story form of Kagotsurube Isshin (11416, which IS offered) — no
# passive, never a player weapon, would just read as a wrong entry in the picker.
WEAPONS_QUEST_ONLY = {11420}


def get(d, *names, default=None):
    """Field lookup tolerant of the dumps' casing drift (1.6-era PascalCase vs newer camelCase)."""
    for n in names:
        if n in d:
            return d[n]
        alt = n[0].lower() + n[1:]
        if alt in d:
            return d[alt]
    return default


def fetch(version, commit, path):
    local = os.path.join(CACHE, version, os.path.basename(path))
    if not os.path.exists(local):
        os.makedirs(os.path.dirname(local), exist_ok=True)
        url = RAW.format(commit=commit, path=path)
        print(f"  [{version}] downloading {os.path.basename(path)} ...", flush=True)
        urllib.request.urlretrieve(url, local)
    with open(local, encoding="utf-8") as fh:
        return json.load(fh)


def build_version(version, commit):
    """Everything that exists in this version's data, keyed for later 'since' resolution."""
    data = {os.path.basename(p): fetch(version, commit, p) for p in NEEDED}
    tm = data["TextMapEN.json"]

    def name_of(h):
        return tm.get(str(h), "").strip()

    # ── weapons: 4-star and 5-star ──────────────────────────────────────────
    # The tables also carry unreleased beta weapons ("One Side", "Deicide", "Mirror Breaker", the
    # duplicate "Primordial Jade *" line). They give themselves away by reusing a generic low-rarity
    # placeholder sprite (UI_EquipIcon_Sword_Blunt, ..._Bow_Hunters): a weapon that actually shipped
    # owns its icon outright, so an icon shared by two or more entries means placeholder art.
    icon_users = {}
    for w in data["WeaponExcelConfigData.json"]:
        ic = get(w, "Icon")
        if ic:
            icon_users[ic] = icon_users.get(ic, 0) + 1

    weapons = {}
    for w in data["WeaponExcelConfigData.json"]:
        rank = get(w, "RankLevel")
        if rank not in (4, 5):
            continue
        wid = get(w, "Id")
        nm = name_of(get(w, "NameTextMapHash"))
        icon = get(w, "Icon")
        if not wid or wid in WEAPONS_QUEST_ONLY or not nm or not icon or icon_users.get(icon, 0) > 1:
            continue
        weapons[wid] = {
            "id": wid,
            "name": nm,
            "rarity": rank,
            "type": WEAPON_TYPE.get(get(w, "WeaponType"), "Sword"),
            "icon": icon,
        }

    # ── artifacts: sets at the rarity they really drop, one canonical piece per slot ──
    # 5-star sets are the setId >= 14000 family; the classic 4-star sets (Sojourner, Berserker,
    # Instructor, ...) are the setId < 14000 family read at rank 4 — their tables DO define 5-star
    # rows, but those never drop in game at that rarity, and vice-versa the 14000+ sets' rank-4
    # variants would only duplicate every 5-star set, so each family is read at its native rank.
    pieces = [
        r for r in data["ReliquaryExcelConfigData.json"]
        if (get(r, "RankLevel") == 5 and (get(r, "SetId") or 0) >= 14000)
        or (get(r, "RankLevel") == 4 and 0 < (get(r, "SetId") or 0) < 14000
            and get(r, "SetId") not in SETS_3STAR_MAX)
    ]
    set_affix = {
        get(s, "SetId"): get(s, "EquipAffixId")
        for s in data["ReliquarySetExcelConfigData.json"]
        if get(s, "SetId")
    }
    affix_name = {}
    for a in data["EquipAffixExcelConfigData.json"]:
        aid = get(a, "Id")
        if aid not in affix_name:
            nm = name_of(get(a, "NameTextMapHash"))
            if nm:
                affix_name[aid] = nm

    by_set = {}
    for p in pieces:
        sid = get(p, "SetId")
        slot = SLOT.get(get(p, "EquipType"))
        if not sid or not slot:
            continue
        # Several ids share a (set, slot) -- they differ only in the initial sub-stat count (the
        # trailing digit) and sub-stat pool. Pick the HIGHEST id deterministically: that is the
        # variant that spawns with the most starting sub-stats, and it matches the ids the curated
        # config/gamedata.json already ships (e.g. Blizzard Strayer flower = 71544, not 23454).
        cur = by_set.setdefault(sid, {}).get(slot)
        pid = get(p, "Id")
        if cur is None or pid > cur["id"]:
            by_set[sid][slot] = {"slot": slot, "id": pid, "icon": get(p, "Icon")}

    artifacts = {}
    for sid, slots in by_set.items():
        if len(slots) < 5:
            continue  # incomplete set in this build -- skip rather than send a broken set
        nm = affix_name.get(set_affix.get(sid), "")
        if not nm:
            continue
        artifacts[sid] = {
            "setId": sid,
            "name": nm,
            "rarity": 5 if sid >= 14000 else 4,
            "pieces": [slots[s] for s in SLOT_ORDER],
        }

    # ── avatars: playable roster ────────────────────────────────────────────
    avatars = {}
    for a in data["AvatarExcelConfigData.json"]:
        aid = get(a, "Id")
        if not aid or not (10000002 <= aid <= 10000100):
            continue
        nm = name_of(get(a, "NameTextMapHash"))
        if not nm or nm.lower() in ("", "test"):
            continue
        quality = get(a, "QualityType") or ""
        avatars[aid] = {
            "id": aid,
            "name": nm,
            "rarity": 5 if "ORANGE" in str(quality) else 4,
            "weapon": WEAPON_TYPE.get(get(a, "WeaponType"), ""),
        }

    return {"weapons": weapons, "artifacts": artifacts, "avatars": avatars}


def merge_versions(snapshots):
    """Fold per-version snapshots into flat lists tagged with the version each item first appeared in."""
    out = {"avatars": {}, "weapons": {}, "artifactSets": {}}
    for version, snap in snapshots:
        for wid, w in snap["weapons"].items():
            if wid not in out["weapons"]:
                out["weapons"][wid] = dict(w, since=version)
        for sid, s in snap["artifacts"].items():
            if sid not in out["artifactSets"]:
                out["artifactSets"][sid] = dict(s, since=version)
        for aid, a in snap["avatars"].items():
            if aid not in out["avatars"]:
                out["avatars"][aid] = dict(a, since=version)
    return out


def download_icons(names):
    os.makedirs(ICON_DIR, exist_ok=True)
    try:
        from PIL import Image
    except ImportError:
        print("!! Pillow is missing -- skipping the icons (pip install pillow)")
        return 0

    def one(icon):
        dest = os.path.join(ICON_DIR, f"{icon}.webp")
        if os.path.exists(dest):
            return True
        tmp = dest + ".png"
        try:
            req = urllib.request.Request(
                ICON_URL.format(icon=icon), headers={"User-Agent": "Relic-launcher/1.0"}
            )
            with urllib.request.urlopen(req, timeout=40) as r, open(tmp, "wb") as fh:
                fh.write(r.read())
            im = Image.open(tmp).convert("RGBA")
            im.thumbnail((128, 128), Image.LANCZOS)
            # Flatten onto nothing: keep alpha, WebP handles it and the UI draws its own frame.
            im.save(dest, "WEBP", quality=88, method=6)
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            return False
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(one, sorted(names)))
    return sum(1 for r in results if r)


def main():
    want_icons = "--no-icons" not in sys.argv
    print("== Relic catalogue: authoritative data from ExcelBinOutput ==")
    snapshots = [(v, build_version(v, c)) for v, c in VERSIONS]
    for v, s in snapshots:
        print(f"  {v}: {len(s['avatars'])} characters, {len(s['weapons'])} 4-5* weapons, {len(s['artifacts'])} 4-5* sets")

    merged = merge_versions(snapshots)
    catalog = {
        "_comment": (
            "GENERATED by build/make_catalog.py from the game's real ExcelBinOutput. "
            "Do not edit by hand -- re-run the script. 'since' = the first supported version "
            "the item appears in; the UI shows only what is available for the selected version."
        ),
        "versionOrder": [v for v, _ in VERSIONS],
        "avatars": sorted(merged["avatars"].values(), key=lambda x: x["id"]),
        "weapons": sorted(merged["weapons"].values(), key=lambda x: (x["type"], x["id"])),
        "artifactSets": sorted(merged["artifactSets"].values(), key=lambda x: x["name"]),
    }

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, ensure_ascii=False, indent=2)
    print(f"\n-> {OUT_JSON}")
    print(f"   {len(catalog['avatars'])} characters, {len(catalog['weapons'])} 4-5* weapons, "
          f"{len(catalog['artifactSets'])} 4-5* sets")

    if want_icons:
        icons = {w["icon"] for w in catalog["weapons"] if w.get("icon")}
        for s in catalog["artifactSets"]:
            icons |= {p["icon"] for p in s["pieces"] if p.get("icon")}
        print(f"\n== icons: {len(icons)} to download ==")
        ok = download_icons(icons)
        have = {f[:-5] for f in os.listdir(ICON_DIR) if f.endswith(".webp")}
        total = sum(
            os.path.getsize(os.path.join(ICON_DIR, f)) for f in os.listdir(ICON_DIR)
        ) / 1_048_576
        print(f"   {ok}/{len(icons)} downloaded -> {ICON_DIR}  ({total:.1f} MB)")

        # Anything with no art is beta content that never shipped (e.g. the "Glacier and Snowfield"
        # set, which exists in the data but has no icon on any mirror). Drop it rather than let the
        # UI render a broken tile for an item the player can never legitimately see.
        dropped_w = [w["name"] for w in catalog["weapons"] if w.get("icon") not in have]
        dropped_s = [s["name"] for s in catalog["artifactSets"]
                     if any(p.get("icon") not in have for p in s["pieces"])]
        catalog["weapons"] = [w for w in catalog["weapons"] if w.get("icon") in have]
        catalog["artifactSets"] = [s for s in catalog["artifactSets"]
                                   if all(p.get("icon") in have for p in s["pieces"])]
        if dropped_w or dropped_s:
            print("   dropped (no art, unreleased content): "
                  + ", ".join(dropped_w + dropped_s))
        with open(OUT_JSON, "w", encoding="utf-8") as fh:
            json.dump(catalog, fh, ensure_ascii=False, indent=2)
        print(f"   final catalogue: {len(catalog['weapons'])} 4-5* weapons, "
              f"{len(catalog['artifactSets'])} 4-5* sets")


if __name__ == "__main__":
    main()

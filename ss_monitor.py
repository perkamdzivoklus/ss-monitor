"""
SS.LV Property Monitor
Uzrauga ss.lv dzīvokļu sludinājumus Rīgā un nosūta Telegram paziņojumus.
"""

import requests
from bs4 import BeautifulSoup
import os
import time
import re
import json
from datetime import datetime
from collections import defaultdict

# ─── KONFIGURĀCIJA ───────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MAX_PRICE          = 200_000   # € — maksimālā cena
DISCOUNT_THRESHOLD = 0.20      # 20% zem vidējā = minimums iekļaušanai

# Latio/ARCO ceturkšņa bāze — €/m² sērijveida/bez remonta dzīvokļiem
# ATJAUNOT katru ceturksni pēc jaunākā pārskata!
# Pēdējais atjaunojums: 2025 Q1
ARCO_BASE = {
    "centrs":          1900,
    "agenskalns":      1600,
    "purvciems":       1150,
    "imanta":          1100,
    "ilguciems":        950,
    "kengarags":        950,
    "pardaugava":      1200,
    "zolitude":         900,
    "mezciems":        1100,
    "plavnieki":       1000,
    "ziepniekkalns":   1000,
    "jugla":           1000,
    "shampeteris":     1100,
    "teika":           1300,
    "bolderaja":        800,
    "daugavgriva":      750,
    "vecmilgravis":     800,
    "riga":            1100,   # fallback
}

# Rajonu atpazīšana no sludinājuma virsraksta
DISTRICT_KEYWORDS = {
    "centrs":        ["centrs", "центр", "centre", "vecriga", "vēcrīga"],
    "agenskalns":    ["āgenskalns", "агенскалнс", "agenskalns"],
    "purvciems":     ["purvciems", "пурвциемс"],
    "imanta":        ["imanta", "иманта"],
    "ilguciems":     ["iļģuciems", "илгуциемс", "ilguciems"],
    "kengarags":     ["ķengarags", "кенгарагс", "kengarags"],
    "pardaugava":    ["pārdaugava", "pardaugava"],
    "zolitude":      ["zolitūde", "золитуде", "zolitude"],
    "mezciems":      ["mežciems", "межциемс", "mezciems"],
    "plavnieki":     ["plāvnieki", "плявниеки", "plavnieki"],
    "ziepniekkalns": ["ziepniekkalns", "зиепниеккалнс"],
    "jugla":         ["jugla", "югла"],
    "shampeteris":   ["šampēteris", "шампетерис", "shampeteris"],
    "teika":         ["teika", "тейка"],
    "bolderaja":     ["bolderāja", "болдерая", "bolderaja"],
    "daugavgriva":   ["daugavgrīva", "даугавгрива"],
    "vecmilgravis":  ["vecmīlgrāvis", "вецмилгравис"],
}

# Motivēta pārdevēja signāli
URGENCY_KEYWORDS = [
    "steidzami", "steidzams", "срочно", "steidzīgi",
    "mantojums", "наследство", "mantojuma",
    "šķiršanās", "развод", "šķiršanas",
    "apsvērsim piedāvājumus", "рассмотрим предложения",
    "bez remonta", "без ремонта",
    "renovācijas nepieciešama", "требует ремонта",
    "sliktā stāvoklī", "в плохом состоянии",
    "pirms remonta", "до ремонта",
    "saimnieks pārdod", "хозяин продает",
    "bez aģenta", "без агента",
    "parāds", "долг", "hipotēka", "ипотека",
]

# Aģentu atpazīšana
AGENT_KEYWORDS = [
    "re/max", "remax", "latio", "ober-haus", "arco",
    "brokeru", "aģentūra", "агентство", "rigas meža",
    "nira", "vara", "nekustamo īpašumu aģent",
    "realtor", "estate agent",
]

SS_BASE_URL = "https://www.ss.lv/lv/real-estate/flats/riga/sell/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "lv,en;q=0.9",
}


# ─── SCRAPING ─────────────────────────────────────────────────────────────────

def fetch_all_listings() -> list[dict]:
    """Iegūst visus ss.lv sludinājumus Rīgā."""
    listings = []
    page = 1

    while True:
        url = SS_BASE_URL if page == 1 else f"{SS_BASE_URL}page{page}.html"
        print(f"  Lapa {page}: {url}")

        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  Kļūda lapa {page}: {e}")
            break

        soup = BeautifulSoup(resp.content, "html.parser")
        rows = soup.select("tr[id^='tr_']")

        if not rows:
            print(f"  Beigas pie lapas {page}")
            break

        for row in rows:
            listing = parse_row(row)
            if listing:
                listings.append(listing)

        # Pārbauda vai ir nākamā lapa
        if not soup.select_one("a.navi[rel='next']"):
            break

        page += 1
        time.sleep(0.8)  # Pieklājīgs intervals

    return listings


def parse_row(row) -> dict | None:
    """Parsē vienu sludinājuma rindu."""
    try:
        link_elem = row.select_one("a.am")
        if not link_elem:
            return None

        title = link_elem.text.strip()
        url   = "https://www.ss.lv" + link_elem.get("href", "")

        cells = row.find_all("td")
        if len(cells) < 5:
            return None

        # ss.lv kolumnas: virsraksts | istabas | platība | stāvs | sērija | cena
        rooms_text  = cells[1].text.strip() if len(cells) > 1 else ""
        area_text   = cells[2].text.strip() if len(cells) > 2 else ""
        floor_text  = cells[3].text.strip() if len(cells) > 3 else ""
        series_text = cells[4].text.strip() if len(cells) > 4 else ""
        price_text  = cells[-1].text.strip()

        # Platība
        area_match = re.search(r"(\d+(?:[.,]\d+)?)", area_text)
        if not area_match:
            return None
        area = float(area_match.group(1).replace(",", "."))
        if area < 12:
            return None

        # Cena
        price_clean = re.sub(r"[^\d]", "", price_text)
        if not price_clean:
            return None
        price = int(price_clean)
        if price < 1000:
            return None

        # Istabu skaits
        rooms_match = re.search(r"(\d+)", rooms_text)
        rooms = int(rooms_match.group(1)) if rooms_match else None

        price_per_m2 = price / area

        return {
            "title":        title,
            "url":          url,
            "price":        price,
            "area":         area,
            "rooms":        rooms,
            "floor":        floor_text,
            "series":       series_text,
            "price_per_m2": price_per_m2,
        }

    except Exception:
        return None


# ─── CENU LOĢIKA ─────────────────────────────────────────────────────────────

def detect_district(title: str) -> str:
    """Nosaka rajonu no virsraksta."""
    t = title.lower()
    for district, keywords in DISTRICT_KEYWORDS.items():
        if any(k in t for k in keywords):
            return district
    return "riga"


def dynamic_baseline(listings: list[dict]) -> dict[str, float]:
    """Aprēķina dinamisku mediānu €/m² pa rajoniem no ss.lv datiem."""
    by_district: dict[str, list[float]] = defaultdict(list)

    for l in listings:
        d = detect_district(l["title"])
        by_district[d].append(l["price_per_m2"])

    result = {}
    for district, prices in by_district.items():
        if len(prices) >= 8:
            s = sorted(prices)
            mid = len(s) // 2
            result[district] = s[mid]  # mediāna
        else:
            result[district] = ARCO_BASE.get(district, ARCO_BASE["riga"])

    return result


def blended_baseline(dynamic: dict, arco: dict, w_dynamic=0.6) -> dict:
    """Apvieno dinamisko (60%) un ARCO/Latio bāzi (40%)."""
    all_districts = set(list(dynamic.keys()) + list(arco.keys()))
    result = {}
    for d in all_districts:
        dyn_val  = dynamic.get(d)
        arco_val = arco.get(d, arco["riga"])
        if dyn_val:
            result[d] = dyn_val * w_dynamic + arco_val * (1 - w_dynamic)
        else:
            result[d] = arco_val
    return result


# ─── ANALĪZE ─────────────────────────────────────────────────────────────────

def detect_urgency(text: str) -> list[str]:
    t = text.lower()
    return [kw for kw in URGENCY_KEYWORDS if kw in t]


def detect_agent(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in AGENT_KEYWORDS)


def score_listing(listing: dict, baseline: dict) -> tuple[str | None, float]:
    """
    Novērtē sludinājumu.
    Atgriež (score, discount) vai (None, discount) ja necaur slieksnim.
    """
    district   = detect_district(listing["title"])
    base_price = baseline.get(district, baseline.get("riga", 1100))
    discount   = (base_price - listing["price_per_m2"]) / base_price

    signals = listing.get("urgency_signals", [])
    n_sig   = len(signals)

    if discount < DISCOUNT_THRESHOLD:
        return None, discount

    # 🔴 Ļoti labs: 30%+ atlaide VAI 20%+ un 2+ signāli
    if discount >= 0.30 or (discount >= 0.20 and n_sig >= 2):
        return "🔴", discount

    # 🟡 Labs: 25%+ VAI 20%+ un 1 signāls
    if discount >= 0.25 or (discount >= 0.20 and n_sig >= 1):
        return "🟡", discount

    # ⚪️ Pieņemami: 20%+
    return "⚪️", discount


# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def format_message(good: list[dict]) -> str:
    date_str = datetime.now().strftime("%d.%m.%Y")

    if not good:
        return (
            f"📊 *SS.LV monitors — {date_str}*\n\n"
            "Šodien nav atrasts neviens sludinājums, kas atbilstu kritērijiem.\n"
            f"_(atlaide ≥ {int(DISCOUNT_THRESHOLD*100)}%, cena ≤ {MAX_PRICE:,}€)_"
        )

    # Kārtojam: 🔴 → 🟡 → ⚪️, tad pēc atlaides
    order = {"🔴": 0, "🟡": 1, "⚪️": 2}
    good.sort(key=lambda x: (order[x["score"]], -x["discount"]))

    msg  = f"🏠 *SS.LV monitors — {date_str}*\n"
    msg += f"Atrasti *{len(good)}* sludinājumi _(atlaide ≥ {int(DISCOUNT_THRESHOLD*100)}%)_\n\n"

    for l in good[:25]:  # Max 25 paziņojumā
        signals  = l.get("urgency_signals", [])
        is_agent = l.get("is_agent", False)

        msg += f"{l['score']} *{l['title']}*\n"
        msg += f"💰 {l['price']:,}€"

        if l.get("rooms"):
            msg += f" | {l['rooms']}-ist."

        msg += f" | {l['area']}m² | *{l['price_per_m2']:.0f}€/m²*\n"
        msg += f"📉 Atlaide: *-{l['discount']*100:.0f}%* no rajona vidējā\n"

        if l.get("floor"):
            msg += f"🏢 Stāvs: {l['floor']}\n"

        if signals:
            sig_display = [s for s in signals[:3]]
            msg += f"🚨 _{', '.join(sig_display)}_\n"

        if is_agent:
            msg += "👔 _Aģenta sludinājums_\n"

        msg += f"🔗 [Atvērt sludinājumu]({l['url']})\n\n"

    return msg


def send_telegram(text: str) -> bool:
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=data, timeout=15)
        if not resp.ok:
            print(f"Telegram kļūda: {resp.status_code} — {resp.text}")
        return resp.ok
    except requests.RequestException as e:
        print(f"Telegram savienojuma kļūda: {e}")
        return False


# ─── GALVENĀ FUNKCIJA ─────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"SS.LV monitors — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print(f"{'='*50}\n")

    # 1. Iegūst visus sludinājumus
    print("📥 Iegūst sludinājumus no ss.lv...")
    all_listings = fetch_all_listings()
    print(f"✓ Iegūti {len(all_listings)} sludinājumi\n")

    if not all_listings:
        print("❌ Nav sludinājumu — pārtrauc.")
        return

    # 2. Filtrē pēc budžeta
    within_budget = [l for l in all_listings if l["price"] <= MAX_PRICE]
    print(f"💰 Pēc budžeta filtra (≤{MAX_PRICE:,}€): {len(within_budget)} sludinājumi")

    # 3. Aprēķina bāzlīnijas
    print("\n📊 Aprēķina cenu bāzlīnijas...")
    dyn    = dynamic_baseline(all_listings)
    merged = blended_baseline(dyn, ARCO_BASE)

    # Izdrukā bāzlīnijas atkļūdošanai
    print("  Rajonu vidējie (sajaukts):")
    for d, v in sorted(merged.items()):
        print(f"    {d:20s}: {v:.0f} €/m²")

    # 4. Novērtē katru sludinājumu
    print(f"\n🔍 Analizē sludinājumus (slieksnis: -{int(DISCOUNT_THRESHOLD*100)}%)...")
    good_listings = []

    for listing in within_budget:
        listing["urgency_signals"] = detect_urgency(listing["title"])
        listing["is_agent"]        = detect_agent(listing["title"])
        score, discount            = score_listing(listing, merged)

        if score:
            listing["score"]    = score
            listing["discount"] = discount
            good_listings.append(listing)

    print(f"✓ Atbilstoši sludinājumi: {len(good_listings)}")
    print(f"  🔴 Ļoti labi: {sum(1 for l in good_listings if l['score'] == '🔴')}")
    print(f"  🟡 Labi:      {sum(1 for l in good_listings if l['score'] == '🟡')}")
    print(f"  ⚪️  Pieņemami: {sum(1 for l in good_listings if l['score'] == '⚪️')}")

    # 5. Nosūta Telegram
    print("\n📤 Nosūta Telegram paziņojumu...")
    message = format_message(good_listings)

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        ok = send_telegram(message)
        print(f"{'✓ Nosūtīts!' if ok else '❌ Sūtīšana neizdevās'}")
    else:
        print("⚠️  Telegram mainīgie nav iestatīti — drukā uz ekrāna:\n")
        print(message)

    print("\nGatavs.")


if __name__ == "__main__":
    main()

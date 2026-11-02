#!/usr/bin/env python3
"""Bot sesji na strimek.tv (live + VOD) - GitHub Actions edition."""
import asyncio
import os
import random
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

DEFAULT_TARGETS = [
    "https://strimek.tv/pl/",
    "https://strimek.tv/pl/vod/",
    "https://strimek.tv/pl/#tvn-turbo",
    "https://strimek.tv/pl/vod/#!series/eksperyment-odsiadka-sezon-2",
    "https://strimek.tv/pl/vod/#s/eksperyment-odsiadka-sezon-2",
    "https://strimek.tv/pl/vod/#!series/eksperyment-odsiadka",
    "https://strimek.tv/pl/vod/#!series/csi-czysta-strona-internetu",
]

_raw_targets = os.environ.get("TARGET_URL", "").replace(",", "\n")
TARGETS = [t.strip() for t in _raw_targets.splitlines() if t.strip()] or DEFAULT_TARGETS

VISITS = max(1, int(os.environ.get("VISITS", "3")))
CLICK_RATE = float(os.environ.get("CLICK_RATE", "0.75"))
SWITCH_RATE = float(os.environ.get("SWITCH_RATE", "0.55"))
MAX_SWITCHES = int(os.environ.get("MAX_SWITCHES", "3"))
CACHEBUST = os.environ.get("CACHEBUST", "1") == "1"
HEADLESS = os.environ.get("HEADLESS", "0") == "1"

ART = Path("artifacts")
ART.mkdir(exist_ok=True)

PROXIES = [
    x.strip()
    for x in os.environ.get("PROXY_LIST", "").replace(",", "\n").splitlines()
    if x.strip()
]

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
]

VIEWPORTS = [(1366, 768), (1440, 900), (1536, 864), (1920, 1080), (1600, 900)]

REFERRERS = [
    "https://www.google.com/",
    "https://www.google.pl/",
    "https://www.bing.com/",
    "https://www.facebook.com/",
    None,
]

AD_PATTERNS = [
    "al5sm.com", "n6wxm.com", "quge5.com", "monetag", "libtl.com",
    "vignette", "googlesyndication", "doubleclick", "effectivegatecpm",
]

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['pl-PL','pl','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || { runtime: {} };
const _q = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => p.name === 'notifications'
  ? Promise.resolve({state: Notification.permission}) : _q(p);
"""


def log(msg):
    print(msg, flush=True)


def with_cachebust(url):
    """Wstawia ?v= PRZED fragmentem #, żeby nie zepsuć routingu SPA."""
    if not CACHEBUST:
        return url
    base, _, frag = url.partition("#")
    sep = "&" if "?" in base else "?"
    out = f"{base}{sep}v={random.randint(100000, 999999)}"
    return f"{out}#{frag}" if frag else out


def is_vod(url):
    return "/vod/" in urlparse(url).path


async def human_scroll(page):
    for _ in range(random.randint(2, 6)):
        await page.mouse.wheel(0, random.randint(160, 560))
        await page.wait_for_timeout(random.randint(500, 1800))
        if random.random() < 0.3:
            await page.mouse.wheel(0, -random.randint(100, 300))
            await page.wait_for_timeout(random.randint(300, 900))


async def jiggle_mouse(page):
    vp = page.viewport_size
    for _ in range(random.randint(3, 8)):
        await page.mouse.move(
            random.randint(0, vp["width"]),
            random.randint(0, vp["height"]),
            steps=random.randint(6, 20),
        )
        await page.wait_for_timeout(random.randint(150, 700))


async def playback_pos(page):
    try:
        return await page.evaluate(
            "() => { const v = document.querySelector('#playerBox video');"
            " return v ? Math.round(v.currentTime) : -1; }"
        )
    except Exception:
        return -2


async def try_unmute(page):
    try:
        btn = await page.query_selector(".sp-unmute")
        if btn and await btn.is_visible():
            await btn.click(timeout=4000)
            log("  odciszono odtwarzacz")
            return True
    except Exception:
        pass
    return False


async def switch_channel(page):
    """Tryb LIVE: przełączenie kanału w panelu bocznym."""
    try:
        items = await page.query_selector_all("#list .ev")
        if len(items) < 2:
            return False
        el = random.choice(items)
        await el.scroll_into_view_if_needed(timeout=5000)
        await el.click(timeout=6000)
        await page.wait_for_timeout(random.randint(2000, 5000))
        title = await page.text_content("#evTitle")
        log(f"  przełączono kanał -> {title!r}")
        return True
    except Exception as e:
        log(f"  przełączenie nieudane: {type(e).__name__}")
        return False


async def open_vod_item(page):
    """Tryb VOD: klik w kartę serialu / odcinek."""
    try:
        for sel in ("#rseries .rcard", "#replayGrid .rcard:not(.rser)", "#replayGrid .rcard"):
            items = await page.query_selector_all(sel)
            if not items:
                continue
            el = random.choice(items[:12])
            await el.scroll_into_view_if_needed(timeout=5000)
            await el.click(timeout=6000)
            await page.wait_for_timeout(random.randint(3000, 6000))
            log(f"  otwarto kartę VOD ({sel})")
            return True
    except Exception as e:
        log(f"  karta VOD nieudana: {type(e).__name__}")
    return False


async def try_click(page):
    """Klik w player lub element strony - odpala vignette/popunder."""
    for sel in ("#playerBox", ".actions .btn", ".title-row", ".below", "#replayGrid .rcard"):
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click(timeout=4000)
                log(f"  klik w {sel}")
                return
        except Exception:
            continue
    try:
        vp = page.viewport_size
        await page.mouse.click(
            random.randint(int(vp["width"] * 0.3), int(vp["width"] * 0.7)),
            random.randint(int(vp["height"] * 0.3), int(vp["height"] * 0.75)),
        )
        log("  klik w tło")
    except Exception:
        pass


async def hold_extra(pg, idx, n):
    try:
        await pg.wait_for_load_state("domcontentloaded", timeout=20000)
        await pg.wait_for_timeout(random.randint(8000, 16000))
        await pg.screenshot(path=str(ART / f"popunder-{idx}-{n}.png"))
    except Exception:
        pass
    finally:
        try:
            await pg.close()
        except Exception:
            pass


async def visit(browser, idx):
    target = random.choice(TARGETS)
    vod = is_vod(target)

    ua = random.choice(UAS)
    w, h = random.choice(VIEWPORTS)
    proxy = random.choice(PROXIES) if PROXIES else None
    ref = random.choice(REFERRERS)

    ctx = await browser.new_context(
        user_agent=ua,
        viewport={"width": w, "height": h},
        screen={"width": w, "height": h},
        locale="pl-PL",
        timezone_id="Europe/Warsaw",
        color_scheme=random.choice(["light", "dark"]),
        proxy={"server": proxy} if proxy else None,
        extra_http_headers={"Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"},
    )
    await ctx.add_init_script(STEALTH)

    page = await ctx.new_page()
    ad_hits, extra, steps = set(), 0, 0

    def on_page(_pg):
        nonlocal extra
        extra += 1
        asyncio.create_task(hold_extra(_pg, idx, extra))

    ctx.on("page", on_page)
    page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
    page.on(
        "response",
        lambda r: ad_hits.add(r.url.split("?")[0])
        if any(p in r.url.lower() for p in AD_PATTERNS)
        else None,
    )

    url = with_cachebust(target)
    log(f"[{idx}] -> {url} | tryb={'vod' if vod else 'live'} | ref={ref} | proxy={'tak' if proxy else 'nie'}")
    await page.goto(url, wait_until="domcontentloaded", timeout=45000, referer=ref)

    selector = "#replayGrid .rcard, #rseries .rcard" if vod else "#playerBox video, #list .ev"
    try:
        await page.wait_for_selector(selector, timeout=20000)
    except Exception:
        log(f"[{idx}] oczekiwany element nie pojawił się w 20s")

    await page.wait_for_timeout(random.randint(3000, 6000))
    await try_unmute(page)
    await jiggle_mouse(page)

    pos = await playback_pos(page)
    log(f"[{idx}] currentTime wideo: {pos}s")

    await page.wait_for_timeout(random.randint(10000, 25000))
    await human_scroll(page)

    if random.random() < CLICK_RATE:
        await try_click(page)
        await page.wait_for_timeout(random.randint(4000, 9000))

    # głębsze wejścia
    while steps < MAX_SWITCHES and random.random() < SWITCH_RATE:
        ok = await open_vod_item(page) if vod else await switch_channel(page)
        if not ok:
            break
        steps += 1
        await try_unmute(page)
        await page.wait_for_timeout(random.randint(8000, 20000))
        if random.random() < 0.4:
            await try_click(page)
            await page.wait_for_timeout(random.randint(3000, 7000))

    await human_scroll(page)
    await page.wait_for_timeout(random.randint(5000, 12000))

    slots = {}
    for sel in ("#navAd", "#sideAd"):
        try:
            el = await page.query_selector(sel)
            slots[sel] = bool(el and (await el.inner_html()).strip()) if el else False
        except Exception:
            slots[sel] = False
    ad_frames = [f for f in page.frames if any(p in (f.url or "").lower() for p in AD_PATTERNS)]

    log(f"[{idx}] hosty reklamowe: {sorted(ad_hits)[:8] or 'BRAK'}")
    log(f"[{idx}] ramki reklamowe w DOM: {len(ad_frames)} | sloty: {slots}")
    log(f"[{idx}] popundery: {extra} | kroki: {steps}")

    try:
        await page.screenshot(path=str(ART / f"visit-{idx}.png"))
    except Exception:
        pass

    await page.wait_for_timeout(1500)
    await ctx.close()


async def main():
    log(f"cele ({len(TARGETS)}): {TARGETS}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        for i in range(VISITS):
            try:
                await visit(browser, i)
            except Exception as e:
                log(f"[{i}] błąd: {type(e).__name__}: {e}")
            if i < VISITS - 1:
                d = random.uniform(30, 120)
                log(f"przerwa {d:.0f}s")
                await asyncio.sleep(d)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
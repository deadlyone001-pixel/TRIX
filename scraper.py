"""
scraper.py — Chapter scraping logic for various manga sites.

Supported detection methods:
  1. Kuaikan: NUXT SSR data decoding
  2. Bilibili Manga: Page hash change detection + OG metadata
  3. MangaDex: Official public API
  4. Generic: HTML heuristics + page hash
"""


import logging
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import json

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TIMEOUT = 18  # seconds


class ChapterInfo:
    """Holds information about a single chapter."""
    def __init__(self, number: float, title: str, url: str = ""):
        self.number = number
        self.title = title
        self.url = url

    def __repr__(self):
        return f"ChapterInfo(number={self.number}, title={self.title!r})"

    def to_dict(self):
        return {"number": self.number, "title": self.title, "url": self.url}

    @classmethod
    def from_dict(cls, d):
        return cls(d["number"], d["title"], d.get("url", ""))


class MangaInfo:
    """Holds the scraped state of a manga series."""
    def __init__(self, title: str, cover_url: str, latest_chapter: "ChapterInfo | None"):
        self.title = title
        self.cover_url = cover_url
        self.latest_chapter = latest_chapter

    def __repr__(self):
        return f"MangaInfo(title={self.title!r}, latest={self.latest_chapter})"


# ─────────────────────────────────────────────────────────────────────────────
#  NUXT data decoder (shared utility for Nuxt.js SSR sites)
# ─────────────────────────────────────────────────────────────────────────────

def _split_js_args(args_str: str) -> list[str]:
    """Split JS function arguments respecting string literals and nesting."""
    args, current, depth, in_str, esc = [], [], 0, False, False
    sc = ""
    for c in args_str:
        if esc:
            current.append(c)
            esc = False
            continue
        if in_str:
            if c == "\\":
                esc = True
            elif c == sc:
                in_str = False
            current.append(c)
        else:
            if c in ('"', "'"):
                in_str = True
                sc = c
                current.append(c)
            elif c in ("(", "[", "{"):
                depth += 1
                current.append(c)
            elif c in (")", "]", "}"):
                depth -= 1
                current.append(c)
            elif c == "," and depth == 0:
                args.append("".join(current).strip())
                current = []
            else:
                current.append(c)
    if current:
        args.append("".join(current).strip())
    return args


def _decode_nuxt_script(script_text: str) -> dict[str, object]:
    """
    Decode a Nuxt.js compressed __NUXT__ script block.
    Returns a variable-name → Python-value mapping.
    """
    params_m = re.match(r"window\.__NUXT__=\(function\(([^)]+)\)", script_text)
    if not params_m:
        return {}
    params = [p.strip() for p in params_m.group(1).split(",")]

    last_brace_paren = script_text.rfind("}(")
    args_end = script_text.rfind("))")
    if last_brace_paren == -1 or args_end == -1:
        return {}

    args_str = script_text[last_brace_paren + 2 : args_end]
    args_raw = _split_js_args(args_str)

    var_map: dict[str, object] = {}
    for p, a in zip(params, args_raw):
        val = a.strip()
        if val == "true":
            var_map[p] = True
        elif val == "false":
            var_map[p] = False
        elif val in ("null", "undefined", "void 0"):
            var_map[p] = None
        elif (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            inner = val[1:-1]
            inner = re.sub(
                r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), inner
            )
            var_map[p] = inner
        else:
            try:
                var_map[p] = int(val)
            except ValueError:
                try:
                    var_map[p] = float(val)
                except ValueError:
                    var_map[p] = val

    return var_map


def _parse_chapter_number(text: str) -> float | None:
    # 1. Match standard formats
    m = re.search(r"(?:第\s*)?(\d+(?:\.\d+)?)\s*(?:话|章|回|集|季)", text)
    if m:
        return float(m.group(1))
        
    # 2. Match Chinese numerals with 第...话
    cn = {"零":0,"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10,"百":100,"千":1000,"万":10000}
    
    def parse_cn(s):
        total, cur = 0, 0
        for ch in s:
            v = cn.get(ch)
            if v is None: continue
            if v >= 10:
                if cur == 0: cur = 1
                total += cur * v
                cur = 0
            else:
                cur += v
        return float(total + cur) if (total + cur) else None

    m_cn = re.search(r"第\s*([一二三四五六七八九十百千万零]+)\s*(?:话|章|回|集)", text)
    if m_cn:
        return parse_cn(m_cn.group(1))
        
    # 3. Match leading Arabic numbers with separators
    m_lead = re.match(r"^\s*(?:\[|【|\()?(\d+(?:\.\d+)?)(?:\s|-|\.|:|：|_|】|\]|）|\)|$)", text)
    if m_lead:
        return float(m_lead.group(1))
        
    # 4. Match leading Chinese numerals with separators
    m_cn_lead = re.match(r"^\s*(?:\[|【|\()?([一二三四五六七八九十百千万零]+)(?:\s|-|\.|:|：|_|】|\]|）|\)|$)", text)
    if m_cn_lead:
        return parse_cn(m_cn_lead.group(1))
        
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  Site-specific scrapers
# ─────────────────────────────────────────────────────────────────────────────

def _scrape_kuaikan(url: str, session: requests.Session) -> MangaInfo:
    """
    Parse kuaikanmanhua.com topic pages using Nuxt SSR data decoding.
    Fallback: HTML heuristics.
    """
    resp = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Manga title from og:title
    og_title = soup.find("meta", property="og:title")
    manga_title = (
        og_title["content"] if og_title and og_title.get("content") else "Unknown"
    )
    # Clean up site suffix
    manga_title = manga_title.split("｜")[0].split("|")[0].strip()

    # Cover image from og:image
    og_image = soup.find("meta", property="og:image")
    cover_url = og_image["content"] if og_image and og_image.get("content") else ""

    # Try NUXT SSR decoding
    scripts = soup.find_all("script")
    nuxt_script = None
    for s in scripts:
        txt = s.get_text()
        if "window.__NUXT__" in txt and len(txt) > 1000:
            nuxt_script = txt
            break

    if nuxt_script:
        try:
            var_map = _decode_nuxt_script(nuxt_script)
            body_start = nuxt_script.index("{", nuxt_script.index(")"))
            body_end = nuxt_script.rfind("}(")
            body = nuxt_script[body_start:body_end]

            # Extract main cover image from topicInfo
            # Example: topicInfo:{id:F,title:g,cover_image_url:G,...}
            cover_match = re.search(r"topicInfo:\{[^}]*cover_image_url:(\w+)", body)
            if cover_match:
                img_var = cover_match.group(1)
                real_cover = var_map.get(img_var)
                if isinstance(real_cover, str) and real_cover.startswith("http"):
                    cover_url = real_cover

            # Find comic entries: {id:VAR, title:VAR, cover_image_url:VAR}
            comic_pattern = re.compile(
                r"\{id:(\w+),title:(\w+),cover_image_url:(\w+)"
            )
            entries = comic_pattern.findall(body)

            if entries:
                # Use list ORDER as canonical (Kuaikan: oldest first → newest last)
                # Each entry in NUXT has unique id; last entry = latest chapter
                valid_entries = []
                for e in entries:
                    ch_id = var_map.get(e[0])
                    ch_title = var_map.get(e[1])
                    if isinstance(ch_title, str) and isinstance(ch_id, (int, float)):
                        valid_entries.append((ch_id, ch_title))

                if valid_entries:
                    latest_id, latest_title = valid_entries[-1]
                    # Extract display chapter number from Chinese title
                    m_num = _parse_chapter_number(latest_title)
                    if m_num is not None:
                        latest_num = m_num
                    else:
                        # Scan backwards to find the last valid chapter number
                        last_num = 0.0
                        for i in range(len(valid_entries) - 2, -1, -1):
                            prev_title = valid_entries[i][1]
                            m_prev = _parse_chapter_number(prev_title)
                            if m_prev is not None:
                                last_num = m_prev
                                break
                        if last_num > 0:
                            # Add a fractional amount so it registers as a new chapter
                            latest_num = last_num + 0.01
                        else:
                            latest_num = float(len(valid_entries))
                    ch_url = f"https://www.kuaikanmanhua.com/web/comic/{latest_id}/"
                    logger.info(
                        f"Kuaikan NUXT decode: {manga_title} — "
                        f"Ch.{latest_num}: {latest_title} (total {len(valid_entries)})"
                    )
                    return MangaInfo(
                        title=manga_title,
                        cover_url=cover_url,
                        latest_chapter=ChapterInfo(latest_num, latest_title, ch_url),
                    )
        except Exception as e:
            logger.warning(f"NUXT decode failed for {url}: {e}")

    # Fallback: look for chapter links in HTML
    logger.warning(f"Kuaikan NUXT decode failed, using HTML fallback for {url}")
    return _scrape_generic(url, session, pre_fetched_soup=soup, pre_title=manga_title)


def _scrape_bilibili_manga(url: str, session: requests.Session) -> MangaInfo:
    """
    Parse manga.bilibili.com using the MOBILE page SSR JSON.

    The mobile detail page (/m/detail/mc{id}) embeds full comic data in an
    inline <script> tag as server-rendered JSON — no authentication needed.

    Data path: data.seasonData.{title, last_ord, last_short_title, ep_list}
    """
    # Extract comic ID from URL (supports /detail/mc35917 and /m/detail/mc35917)
    m = re.search(r"/(?:detail|m/detail)/mc(\d+)", url)
    if not m:
        raise ValueError(f"Cannot extract Bilibili comic ID from URL: {url}")
    comic_id = m.group(1)

    # Use mobile UA to get the SSR page with embedded JSON
    mobile_url = f"https://manga.bilibili.com/m/detail/mc{comic_id}"
    mobile_headers = {
        **HEADERS,
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/16.6 Mobile/15E148 Safari/604.1"
        ),
    }

    resp = session.get(mobile_url, headers=mobile_headers, timeout=TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    # Extract title from <title> tag (mobile page has it)
    title_tag = soup.find("title")
    raw_title = title_tag.get_text(strip=True) if title_tag else ""
    # Title format: "《漫画名》_正版汉化版_哔哩哔哩漫画"
    # Clean up
    manga_title = re.sub(r"[《》]", "", raw_title)
    manga_title = re.split(r"[_｜|]", manga_title)[0].strip()
    if not manga_title or "哔哩哔哩" in manga_title:
        manga_title = f"Bilibili Manga mc{comic_id}"

    # Find the inline JSON script (the big one with server data)
    inline_json = None
    for script in soup.find_all("script"):
        if not script.get("src"):
            txt = script.get_text(strip=True)
            if len(txt) > 5000 and '"seasonData"' in txt:
                inline_json = txt
                break

    if inline_json:
        try:
            data = json.loads(inline_json)
            season = data.get("data", {}).get("seasonData", {})

            if season:
                manga_title = season.get("title") or manga_title
                cover = season.get("vertical_cover") or season.get("horizontal_cover") or ""

                last_ord = season.get("last_ord", 0)
                last_short = season.get("last_short_title") or ""

                ep_list = season.get("ep_list", [])

                # ── pick the best episode ─────────────────────────────────────
                # Some comics have last_ord=0 (e.g. locked/preview), so we
                # walk ep_list and prefer the entry whose ord matches last_ord,
                # then fall back to highest ord, then highest list-index.
                latest_ep = None
                if ep_list:
                    if last_ord:
                        for ep in ep_list:
                            if ep.get("ord") == last_ord:
                                latest_ep = ep
                                break
                    if latest_ep is None:
                        # highest ord wins; 0-ord entries sorted last
                        latest_ep = max(ep_list, key=lambda e: e.get("ord", 0))
                    # If ALL ords are 0 (some series store numbers only in
                    # short_title like "第92章"), use the first item in the
                    # list which Bilibili orders newest→oldest.
                    if latest_ep.get("ord", 0) == 0 and ep_list:
                        latest_ep = ep_list[0]

                def _parse_ch_num(text: str) -> float:
                    """Extract a chapter number from arbitrary Chinese/English text."""
                    # Convert Chinese numerals for common cases handled inline
                    cn_map = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,
                              "八":8,"九":9,"十":10,"百":100,"千":1000}
                    # Arabic digits first
                    m_ar = re.search(r"(\d+(?:\.\d+)?)", text)
                    if m_ar:
                        return float(m_ar.group(1))
                    # Try naive Chinese numeral conversion (handles 九十二 → 92)
                    total, cur = 0, 0
                    for ch in text:
                        v = cn_map.get(ch)
                        if v is None:
                            continue
                        if v >= 10:
                            cur = max(cur, 1) * v
                            if v == 1000:
                                total += cur; cur = 0
                        else:
                            cur += v
                    total += cur
                    return float(total) if total else 0.0

                if latest_ep:
                    raw_ord  = latest_ep.get("ord", 0)
                    short    = latest_ep.get("short_title") or last_short or ""
                    title_pt = latest_ep.get("title") or ""
                    ch_title = f"{short}{title_pt}".strip() or f"Chapter {int(raw_ord)}"
                    ch_url   = f"https://manga.bilibili.com/mc{comic_id}/{latest_ep.get('id', '')}"

                    # Determine the best numeric chapter number
                    if raw_ord and raw_ord > 0:
                        ch_num = float(raw_ord)
                    else:
                        ch_num = _parse_ch_num(short) or _parse_ch_num(title_pt)
                        if not ch_num:
                            ch_num = float(len(ep_list))
                else:
                    ch_num   = float(last_ord) if last_ord else float(len(ep_list))
                    ch_title = last_short or f"Chapter {int(ch_num)}"
                    ch_url   = url

                logger.info(
                    f"Bilibili manga (mobile SSR): {manga_title} — "
                    f"Ch.{ch_num}: {ch_title}"
                )
                return MangaInfo(
                    title=manga_title,
                    cover_url=cover,
                    latest_chapter=ChapterInfo(ch_num, ch_title, ch_url),
                )

        except Exception as e:
            logger.warning(f"Bilibili mobile SSR parse failed: {e}")

    # Fallback: extract chapter number from the page title itself
    # Title sometimes contains "第X话" or "共X话"
    title_text = raw_title
    ch_patterns = [
        r"第\s*(\d+(?:\.\d+)?)\s*话",
        r"共\s*(\d+)\s*(?:话|章)",
        r"更新至第\s*(\d+)\s*话",
    ]
    for pat in ch_patterns:
        m2 = re.search(pat, title_text)
        if m2:
            ch_num = float(m2.group(1))
            ch_title = f"第{int(ch_num)}话"
            logger.info(f"Bilibili fallback title parse: Ch.{ch_num}")
            return MangaInfo(
                title=manga_title,
                cover_url="",
                latest_chapter=ChapterInfo(ch_num, ch_title, url),
            )

    logger.warning(f"Bilibili: could not extract chapter for {url}")
    return MangaInfo(title=manga_title, cover_url="", latest_chapter=None)



def _scrape_mangadex(url: str, session: requests.Session) -> MangaInfo:
    """Parse MangaDex using the official public API."""
    # Extract manga UUID from URL: /title/UUID or /manga/UUID
    m = re.search(r"/(?:title|manga)/([0-9a-f-]{36})", url)
    if not m:
        raise ValueError(f"Cannot extract MangaDex manga ID from URL: {url}")

    manga_id = m.group(1)
    api_base = "https://api.mangadex.org"

    # Get manga info
    info_resp = session.get(
        f"{api_base}/manga/{manga_id}",
        headers={**HEADERS, "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    info_resp.raise_for_status()
    info_data = info_resp.json().get("data", {})
    attrs = info_data.get("attributes", {})
    title_map = attrs.get("title", {})
    manga_title = (
        title_map.get("en")
        or title_map.get("ja-ro")
        or next(iter(title_map.values()), "Unknown")
    )

    # Get latest chapter
    feed_resp = session.get(
        f"{api_base}/manga/{manga_id}/feed",
        params={
            "limit": 1,
            "order[chapter]": "desc",
            "translatedLanguage[]": ["en"],
        },
        headers={**HEADERS, "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    feed_resp.raise_for_status()
    feed_data = feed_resp.json()
    chapters = feed_data.get("data", [])

    if not chapters:
        return MangaInfo(title=manga_title, cover_url="", latest_chapter=None)

    ch_attrs = chapters[0].get("attributes", {})
    ch_num_str = ch_attrs.get("chapter") or "0"
    ch_num = float(ch_num_str) if ch_num_str else 0.0
    ch_title = ch_attrs.get("title") or f"Chapter {ch_num}"
    ch_id = chapters[0].get("id", "")
    ch_url = f"https://mangadex.org/chapter/{ch_id}"

    return MangaInfo(
        title=manga_title,
        cover_url="",
        latest_chapter=ChapterInfo(ch_num, ch_title, ch_url),
    )


def _scrape_generic(
    url: str,
    session: requests.Session,
    pre_fetched_soup: "BeautifulSoup | None" = None,
    pre_title: str = "",
) -> MangaInfo:
    """Generic heuristic scraper for unknown sites."""
    if pre_fetched_soup is None:
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    else:
        soup = pre_fetched_soup

    # Title
    og_title = soup.find("meta", property="og:title")
    title_tag = soup.find("h1") or soup.find("title")
    manga_title = pre_title or (
        og_title["content"]
        if og_title and og_title.get("content")
        else (title_tag.get_text(strip=True) if title_tag else "Unknown")
    )

    # Cover image
    og_image = soup.find("meta", property="og:image")
    cover = og_image["content"] if og_image and og_image.get("content") else ""

    # Find chapter links heuristically
    chapter_patterns = [
        r"(?:chapter|ch(?:ap)?\.?)\s*[-–]?\s*(\d+(?:\.\d+)?)",
        r"(?:episode|ep\.?)\s*[-–]?\s*(\d+(?:\.\d+)?)",
        r"第\s*(\d+(?:\.\d+)?)\s*(?:話|话|章|回)",
    ]
    combined = re.compile("|".join(chapter_patterns), re.IGNORECASE)

    best_num = -1.0
    latest = None
    parsed_base = urlparse(url)

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        m = combined.search(text) or combined.search(href)
        if m:
            num_str = next((g for g in m.groups() if g is not None), None)
            if num_str:
                try:
                    num = float(num_str)
                    if num > best_num:
                        best_num = num
                        ch_title = text if text else f"Chapter {num}"
                        ch_url = href
                        if ch_url.startswith("/"):
                            ch_url = f"{parsed_base.scheme}://{parsed_base.netloc}{ch_url}"
                        elif not ch_url.startswith("http"):
                            ch_url = url + "/" + ch_url
                        latest = ChapterInfo(num, ch_title, ch_url)
                except ValueError:
                    pass

    return MangaInfo(title=manga_title, cover_url=cover, latest_chapter=latest)


def _scrape_ac_qq(url: str, session: requests.Session) -> MangaInfo:
    """Scrape Tencent Animation & Comics (ac.qq.com) with proxy fallback"""
    import urllib.parse
    
    html = ""
    try:
        r = session.get(url, timeout=10)
        r.raise_for_status()
        html = r.text
    except requests.exceptions.RequestException as e:
        logger.warning(f"Direct connection to ac.qq.com failed ({type(e).__name__}). Using proxy fallback...")
        proxy_url = f"https://api.allorigins.win/raw?url={urllib.parse.quote(url)}"
        r_proxy = session.get(proxy_url, timeout=15)
        r_proxy.raise_for_status()
        html = r_proxy.text

    soup = BeautifulSoup(html, "lxml")

    # Title
    manga_title = "Unknown"
    title_el = soup.select_one(".works-intro-title strong")
    if title_el:
        manga_title = title_el.get_text(strip=True)

    # Cover
    cover_url = ""
    cover_el = soup.select_one(".works-cover img")
    if cover_el:
        cover_url = cover_el.get("src", "")

    # Chapters
    chapters = []
    seen_urls = set()
    for a in soup.select(".works-chapter-item a"):
        ch_title = (a.get("title") or a.get_text(strip=True)).strip()
        ch_href  = a.get("href", "")
        ch_url   = "https://ac.qq.com" + ch_href if ch_href.startswith("/") else ch_href

        if ch_url in seen_urls:
            continue
        seen_urls.add(ch_url)

        # Strip leading manga-title prefix
        if manga_title and ch_title.startswith(manga_title):
            ch_title = ch_title[len(manga_title):].lstrip("：: ").strip()

        num = _parse_chapter_number(ch_title)
        if num is None:
            num = 0.0
        chapters.append(ChapterInfo(num, ch_title, ch_url))

    if not chapters:
        logger.warning(f"No chapters found for Tencent AC URL: {url}")
        return MangaInfo(title=manga_title, latest_chapter=None, cover_url=cover_url)

    # Sort numerically; if all nums are 0, fallback to their index
    if any(c.number > 0 for c in chapters):
        chapters.sort(key=lambda c: c.number)
    else:
        # No chapters had valid numbers (e.g. they are just titled "Prologue")
        for i, c in enumerate(chapters):
            c.number = float(i + 1)
            
    latest = chapters[-1]

    logger.info(
        f"Tencent AC: {manga_title} — "
        f"Ch.{latest.number:.0f}: {latest.title} "
        f"(total {len(chapters)})"
    )

    return MangaInfo(title=manga_title, latest_chapter=latest, cover_url=cover_url)


# ─────────────────────────────────────────────────────────────────────────────
#  Korean platform scrapers
# ─────────────────────────────────────────────────────────────────────────────

KR_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.google.com/",
}


def _scrape_naver_webtoon(url: str, session: requests.Session) -> MangaInfo:
    """
    Parse comic.naver.com/webtoon using the official JSON article-list API.
    URL format: https://comic.naver.com/webtoon/list?titleId=769209
    """
    m = re.search(r'titleId=(\d+)', url)
    if not m:
        raise ValueError(f"Cannot extract titleId from: {url}")
    title_id = m.group(1)

    api = f"https://comic.naver.com/api/article/list?titleId={title_id}&page=1&sort=DESC"
    resp = session.get(api, headers=KR_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    articles = data.get("articleList", [])
    if not articles:
        # fallback: try to get title from the HTML page
        page = session.get(url, headers=KR_HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(page.text, "lxml")
        og = soup.find("meta", property="og:title")
        title = og["content"].split(" : ")[0].strip() if og else "Unknown"
        return MangaInfo(title=title, cover_url="", latest_chapter=None)

    latest = articles[0]
    # volumeNo is the episode sequence number; subtitle is like "159화"
    ch_num   = float(latest.get("volumeNo", 0))
    subtitle = latest.get("subtitle", f"{int(ch_num)}화")
    # Parse Arabic numeral from subtitle (e.g. "159화" → 159)
    m_num = re.search(r'(\d+(?:\.\d+)?)', subtitle)
    if m_num:
        ch_num = float(m_num.group(1))
    ch_url = f"https://comic.naver.com/webtoon/detail?titleId={title_id}&no={int(ch_num)}"

    # Get manga title from HTML (API doesn't include it)
    title = "Unknown"
    try:
        page = session.get(url, headers=KR_HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(page.text, "lxml")
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].split(" : ")[0].strip()
    except Exception:
        pass

    logger.info(f"Naver Webtoon: {title} — Ch.{ch_num}: {subtitle}")
    return MangaInfo(title=title, cover_url="", latest_chapter=ChapterInfo(ch_num, subtitle, ch_url))


def _scrape_naver_series(url: str, session: requests.Session) -> MangaInfo:
    """
    Parse series.naver.com/comic using the detail page HTML.
    URL format: https://series.naver.com/comic/detail.series?productNo=6030941
    Latest episode number is extracted from the highest ³µ numeral on the page.
    """
    resp = session.get(url, headers=KR_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Title from og:title (strip [독점] suffixes)
    og = soup.find("meta", property="og:title")
    manga_title = og["content"].strip() if og and og.get("content") else "Unknown"
    manga_title = re.sub(r'\[.*?\]', '', manga_title).strip()

    # All 화 numbers on the page — the maximum is the latest chapter
    ep_nums = re.findall(r'(\d+)화', resp.text)
    if not ep_nums:
        return MangaInfo(title=manga_title, cover_url="", latest_chapter=None)

    ch_num   = float(max(int(n) for n in ep_nums))
    ch_title = f"{int(ch_num)}화"
    m = re.search(r'productNo=(\d+)', url)
    product_no = m.group(1) if m else ""
    ch_url = f"https://series.naver.com/comic/viewer.series?productNo={product_no}"

    logger.info(f"Naver Series: {manga_title} — Ch.{ch_num}")
    return MangaInfo(title=manga_title, cover_url="", latest_chapter=ChapterInfo(ch_num, ch_title, ch_url))


def _scrape_kakao_page(url: str, session: requests.Session) -> MangaInfo:
    """
    Parse page.kakao.com/content using the embedded __NEXT_DATA__ JSON.

    The SSR page includes a React Query dehydrated state with:
      - content.title
      - content.freeSlideCount  (total free episodes = episode number)
      - content.lastSlideAddedDate (when last episode was uploaded)
    URL format: https://page.kakao.com/content/61856924
    """
    resp = session.get(
        url,
        headers={**KR_HEADERS, "Referer": "https://page.kakao.com/"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Title fallback from og:title
    og_title = soup.find("meta", property="og:title")
    manga_title = og_title["content"].strip() if og_title and og_title.get("content") else "Unknown"
    manga_title = re.sub(r'\s*[-|]\s*(웹툰|카카오페이지).*$', '', manga_title).strip()

    # Parse __NEXT_DATA__ embedded JSON for episode data
    nd_tag = soup.find("script", id="__NEXT_DATA__")
    if nd_tag:
        try:
            import json as _json
            nd = _json.loads(nd_tag.get_text())
            # Traverse dehydrated React Query state
            queries = (
                nd.get("props", {})
                  .get("pageProps", {})
                  .get("initialProps", {})
                  .get("dehydratedState", {})
                  .get("queries", [])
            )
            content = None
            for q in queries:
                data = q.get("state", {}).get("data", {})
                overview = data.get("contentHomeOverview", {})
                if overview:
                    content = overview.get("content", {})
                    if not content:
                        content = data
                    break

            if content:
                # freeSlideCount = number of free episodes (reliable episode count)
                free_count = content.get("freeSlideCount", 0)
                # Also try totalSlideCount if available
                total_count = content.get("totalSlideCount") or content.get("slideCount") or free_count
                ch_num = float(total_count or free_count)

                # Use manga title from the JSON (cleaner than og:title)
                manga_title = content.get("title") or manga_title

                if ch_num > 0:
                    m_id = re.search(r'/content/(\d+)', url)
                    content_id = m_id.group(1) if m_id else ""
                    ch_title = f"{int(ch_num)}화"
                    ch_url = f"https://page.kakao.com/content/{content_id}"
                    logger.info(f"Kakao Page (__NEXT_DATA__): {manga_title} — Ch.{ch_num}")
                    return MangaInfo(
                        title=manga_title,
                        cover_url="",
                        latest_chapter=ChapterInfo(ch_num, ch_title, ch_url),
                    )
        except Exception as e:
            logger.warning(f"Kakao __NEXT_DATA__ parse failed: {e}")

    # Final fallback: regex max 화 number from HTML
    all_nums = [int(n) for n in re.findall(r'(\d+)화', resp.text)]
    if all_nums:
        ch_num = float(max(all_nums))
        ch_title = f"{int(ch_num)}화"
        m_id = re.search(r'/content/(\d+)', url)
        content_id = m_id.group(1) if m_id else ""
        ch_url = f"https://page.kakao.com/content/{content_id}"
        logger.info(f"Kakao Page (fallback): {manga_title} — Ch.{ch_num}")
        return MangaInfo(title=manga_title, cover_url="", latest_chapter=ChapterInfo(ch_num, ch_title, ch_url))

    return MangaInfo(title=manga_title, cover_url="", latest_chapter=None)




# ─────────────────────────────────────────────────────────────────────────────
#  Public interface
# ─────────────────────────────────────────────────────────────────────────────

def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def scrape(url: str, session: "requests.Session | None" = None) -> MangaInfo:
    """Dispatch to the appropriate scraper based on domain."""
    if session is None:
        session = get_session()

    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    logger.info(f"Scraping: {url} (domain={domain})")

    try:
        if "kuaikanmanhua.com" in url:
            return _scrape_kuaikan(url, session)
        elif "bilibili.com" in url:
            return _scrape_bilibili_manga(url, session)
        elif "mangadex.org" in url:
            return _scrape_mangadex(url, session)
        elif "ac.qq.com" in url:
            return _scrape_ac_qq(url, session)
        elif "comic.naver.com" in url:
            return _scrape_naver_webtoon(url, session)
        elif "series.naver.com" in url:
            return _scrape_naver_series(url, session)
        elif "page.kakao.com" in url:
            return _scrape_kakao_page(url, session)

        return _scrape_generic(url, session)

    except requests.exceptions.Timeout:
        raise ConnectionError(f"Request timed out for {url}")
    except requests.exceptions.ConnectionError as e:
        raise ConnectionError(f"Cannot connect to {url}: {e}")
    except requests.exceptions.HTTPError as e:
        raise ConnectionError(
            f"HTTP error {e.response.status_code} for {url}"
        )

#!/usr/bin/env python3
"""
Atomic Digital Product-Fit Advisor v2
================================
Scrape any family office or investment fund's public website, analyze their
investment style with DeepSeek, and get
a pre-meeting brief recommending which Atomic Digital product (Market Neutral
Fund / Bitcoin Market Neutral Fund / SMA) to pitch — plus talking points,
objection handling, and data gaps to confirm in the meeting.

INSTALL:
    pip install openai requests beautifulsoup4 lxml python-dotenv fpdf2

    Create a file named .env in this same folder (never commit/share it) with:
        DEEPSEEK_API_KEY=your-key-here
        DEEPSEEK_MODEL=deepseek-v4-flash
    Get a key at https://platform.deepseek.com. This is loaded automatically
    on startup — no need to `export` these in your shell every time (which
    only applies to that one terminal session anyway). Plain env vars still
    work too if you'd rather set them that way.

    Optional, for JS-heavy prospect sites (React/Vue/Next.js homepages that
    render empty when fetched with plain requests):
    pip install selenium
    Also requires a matching Chrome/Chromedriver on PATH (or use
    webdriver-manager to handle that automatically):
    pip install webdriver-manager
    Selenium is used only as an automatic fallback when a page's own site
    comes back suspiciously empty — it renders that same official page in a
    headless browser so the content can be parsed, nothing more.

SINGLE PROSPECT:
    python advisor.py --name "Landmark Family Office" --website https://landmarkfo.com/en/

BATCH (CSV with columns: name, website, linkedin):
    python advisor.py --batch prospects.csv --outdir reports/

SEARCH BY NAME ONLY (auto-finds website):
    python advisor.py --name "Landmark Family Office"

OUTPUT JSON:
    python advisor.py --name "Landmark Family Office" --website https://landmarkfo.com/en/ --json
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: pip install openai requests beautifulsoup4 lxml")
    sys.exit(1)

# Load DEEPSEEK_* variables from a .env file next to this script, if one
# exists — so you don't have to `export` them in the exact terminal session
# you launch this from every time. A plain `export` in one terminal window
# does NOT carry over to a different terminal, VS Code run, or GUI launch;
# a .env file does, since this reads it directly off disk.
# pip install python-dotenv
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass  # .env support is optional — plain `export`-ed env vars still work fine

# Selenium is optional — only needed for JS-rendered sites where a plain
# requests.get() comes back nearly empty (React/Vue/Next.js homepages, etc).
# Install with: pip install selenium webdriver-manager
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

import atexit

try:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False

_selenium_driver = None  # lazily created, reused across pages, closed at exit


def _get_selenium_driver():
    """Lazily start one headless Chrome instance for the whole run."""
    global _selenium_driver
    if _selenium_driver is not None:
        return _selenium_driver

    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument(f"user-agent={HEADERS['User-Agent']}")
    # Don't load images — faster, and we only need text/DOM.
    opts.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})

    _selenium_driver = webdriver.Chrome(options=opts)
    _selenium_driver.set_page_load_timeout(20)
    atexit.register(_close_selenium_driver)
    return _selenium_driver


def _close_selenium_driver():
    global _selenium_driver
    if _selenium_driver is not None:
        try:
            _selenium_driver.quit()
        except Exception:
            pass
        _selenium_driver = None

# ──────────────────────────────────────────────────────────────────────────
# ATOMIC DIGITAL PRODUCT KNOWLEDGE — loaded from product_knowledge.json so there's
# a single source of truth (edit the JSON file, not this script, when terms
# change). Falls back to a script-relative path if run from another cwd.
# ──────────────────────────────────────────────────────────────────────────

_PK_PATH = Path(__file__).resolve().parent / "product_knowledge.json"

try:
    with open(_PK_PATH, "r") as _f:
        PRODUCT_KNOWLEDGE = json.load(_f)
except FileNotFoundError:
    print(f"ERROR: product_knowledge.json not found at {_PK_PATH}")
    sys.exit(1)
except json.JSONDecodeError as e:
    print(f"ERROR: product_knowledge.json is not valid JSON: {e}")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────
# 1. SCRAPING
# ──────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def scrape_multiple_websites(urls: list, max_pages: int = 5, max_chars_per_page: int = 12000, allow_selenium: bool = True) -> dict:
    """
    Scrape several websites (e.g. the main site plus a specific team/strategy
    page hosted elsewhere) and merge the results into one combined dict with
    the same shape scrape_website() returns, so analyze_prospect() doesn't
    need to know or care whether it came from one site or several.
    """
    combined = {
        "urls": urls,
        "pages_scraped": [],
        "all_text": "",
        "headings": [],
        "team_info": "",
        "investment_keywords_found": [],
        "errors": [],
    }

    all_text_parts = []
    team_parts = []

    for url in urls:
        url = (url or "").strip()
        if not url:
            continue
        print(f"  Scraping website: {url}")
        result = scrape_website(url, max_pages=max_pages, max_chars_per_page=max_chars_per_page, allow_selenium=allow_selenium)

        if result.get("error"):
            print(f"    (failed to scrape {url}: {result['error']})")
            combined["errors"].append({"url": url, "error": result["error"]})
            continue

        pages = len(result.get("pages_scraped", []))
        chars = len(result.get("all_text", ""))
        print(f"  Scraped {pages} pages, {chars} chars from {url}")

        combined["pages_scraped"].extend(result.get("pages_scraped", []))
        combined["headings"].extend(result.get("headings", []))
        combined["investment_keywords_found"].extend(result.get("investment_keywords_found", []))
        if result.get("all_text"):
            all_text_parts.append(result["all_text"])
        if result.get("team_info"):
            team_parts.append(result["team_info"])

    # Slightly higher cap than a single site, since content from several
    # distinct sites is less redundant than several pages of the same site.
    combined["all_text"] = "\n\n".join(all_text_parts)[:80000]
    combined["team_info"] = "\n".join(team_parts)[:15000] if team_parts else ""

    if not combined["pages_scraped"] and combined["errors"]:
        combined["error"] = "; ".join(f"{e['url']}: {e['error']}" for e in combined["errors"])

    return combined


def scrape_website(url: str, max_pages: int = 5, max_chars_per_page: int = 12000, allow_selenium: bool = True) -> dict:
    """
    Scrape a family office website — homepage + linked subpages (About, Team,
    Strategy, Portfolio, Investments, Services). Returns structured content.
    Automatically falls back to a headless-browser render (Selenium) for
    pages that come back empty from a plain HTTP fetch (JS-rendered sites).
    """
    result = {
        "url": url,
        "pages_scraped": [],
        "all_text": "",
        "headings": [],
        "team_info": "",
        "investment_keywords_found": [],
    }

    # Scrape homepage first
    homepage = _scrape_single_page(url, max_chars_per_page, allow_selenium=allow_selenium)
    if homepage.get("error"):
        result["error"] = homepage["error"]
        return result

    result["pages_scraped"].append(homepage)
    result["headings"].extend(homepage.get("headings", []))
    result["investment_keywords_found"].extend(homepage.get("investment_links", []))

    # Find and scrape important subpages
    priority_patterns = [
        r'about', r'team', r'strategy', r'portfolio', r'investment',
        r'services', r'approach', r'philosophy', r'fund', r'asset',
        r'crypto', r'digital', r'contact'
    ]

    visited = {url}
    subpage_urls = []

    for link in homepage.get("all_links", []):
        href = link.get("href", "")
        full_url = urljoin(url, href)
        if full_url in visited:
            continue
        if urlparse(full_url).netloc != urlparse(url).netloc:
            continue
        if not any(re.search(pat, href.lower()) for pat in priority_patterns):
            continue
        subpage_urls.append(full_url)
        visited.add(full_url)

    # Scrape up to max_pages subpages
    for suburl in subpage_urls[:max_pages - 1]:
        time.sleep(0.5)  # Be polite
        page = _scrape_single_page(suburl, max_chars_per_page, allow_selenium=allow_selenium)
        if not page.get("error"):
            result["pages_scraped"].append(page)
            result["headings"].extend(page.get("headings", []))

    # Combine all text
    all_text_parts = []
    for page in result["pages_scraped"]:
        if page.get("content"):
            all_text_parts.append(f"--- {page['url']} ---\n{page['content']}")
    result["all_text"] = "\n\n".join(all_text_parts)[:50000]  # Cap total

    # Extract team info specifically (names, titles, backgrounds)
    team_parts = []
    for page in result["pages_scraped"]:
        for h in page.get("headings", []):
            if any(kw in h.lower() for kw in ["team", "leadership", "partner", "founder", "ceo", "cio", "management"]):
                # Grab surrounding context
                content = page.get("content", "")
                idx = content.find(h)
                if idx >= 0:
                    team_parts.append(content[idx:idx + 2000])
    result["team_info"] = "\n".join(team_parts)[:10000] if team_parts else ""

    return result


# Below this char count, a "successful" requests.get() is treated as
# suspicious — most likely a JS-rendered shell (React/Vue/Next.js root div
# with no server-rendered content) rather than a genuinely thin page.
JS_RENDER_SUSPECT_THRESHOLD = 400


def _parse_html(html: str, url: str, max_chars: int) -> dict:
    """Shared parsing logic for both the requests path and the Selenium path."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "nav", "footer", "noscript", "svg", "iframe"]):
        tag.decompose()

    title = soup.find("title")
    title_text = title.get_text(strip=True) if title else ""

    meta_desc = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta:
        meta_desc = meta.get("content", "")

    headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])][:30]

    all_links = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        all_links.append({"text": text, "href": a["href"]})

    investment_links = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if any(kw in text for kw in [
            "invest", "portfolio", "strategy", "fund", "crypto",
            "digital asset", "venture", "asset", "allocation", "approach",
            "services", "team", "about", "philosophy"
        ]):
            investment_links.append({"text": a.get_text(strip=True), "href": a["href"]})

    body_text = soup.get_text(separator="\n", strip=True)
    body_text = re.sub(r'\n{3,}', '\n\n', body_text)
    content = body_text[:max_chars]

    return {
        "url": url,
        "title": title_text,
        "meta_description": meta_desc,
        "headings": headings,
        "all_links": all_links[:100],
        "investment_links": investment_links[:30],
        "content": content,
    }


def _scrape_with_selenium(url: str, max_chars: int) -> dict:
    """Render a JS-heavy page with headless Chrome, then reuse the same parser."""
    if not HAS_SELENIUM:
        return {"url": url, "error": "selenium not installed", "content": "", "headings": [], "all_links": [], "investment_links": []}

    try:
        driver = _get_selenium_driver()
        driver.get(url)
        # Wait for the page to actually populate rather than fixed-sleeping.
        WebDriverWait(driver, 15).until(
            lambda d: len(d.find_element(By.TAG_NAME, "body").text.strip()) > 200
        )
        time.sleep(1)  # let any late-loading widgets/fonts settle
        html = driver.page_source
    except Exception as e:
        return {"url": url, "error": f"selenium render failed: {e}", "content": "", "headings": [], "all_links": [], "investment_links": []}

    parsed = _parse_html(html, url, max_chars)
    parsed["rendered_with"] = "selenium"
    return parsed


def _scrape_single_page(url: str, max_chars: int, allow_selenium: bool = True) -> dict:
    """
    Scrape a single page and extract structured content.
    Tries a plain requests.get() first (fast, polite). If that fails outright,
    or comes back suspiciously thin (likely a JS-rendered shell), falls back
    to rendering the same official page with headless Chrome.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        parsed = _parse_html(resp.text, url, max_chars)
        parsed["rendered_with"] = "requests"

        content_len = len(parsed.get("content", ""))
        if content_len >= JS_RENDER_SUSPECT_THRESHOLD or not allow_selenium or not HAS_SELENIUM:
            return parsed

        # Thin content — likely JS-rendered. Try Selenium as a fallback.
        print(f"    (thin content from requests — {content_len} chars — retrying with Selenium)")
        rendered = _scrape_with_selenium(url, max_chars)
        if rendered.get("content") and len(rendered["content"]) > content_len:
            return rendered
        return parsed  # Selenium didn't help — keep the original

    except Exception as e:
        if allow_selenium and HAS_SELENIUM:
            print(f"    (requests failed [{e}] — retrying with Selenium)")
            return _scrape_with_selenium(url, max_chars)
        return {"url": url, "error": str(e), "content": "", "headings": [], "all_links": [], "investment_links": []}


def google_search(name: str, num_results: int = 8) -> list:
    """Search Google for a family office and return results."""
    from urllib.parse import quote_plus
    query = quote_plus(f'"{name}" family office investment fund')
    url = f"https://www.google.com/search?q={query}&num={num_results}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if text and len(text) > 20 and "google.com" not in href and href.startswith("http"):
            results.append({"url": href, "title": text})

    return results[:num_results]


def find_website_from_name(name: str) -> str | None:
    """Try to find a family office's website from their name via search."""
    results = google_search(name)
    for r in results:
        url = r["url"]
        if "linkedin.com" not in url and "google.com" not in url and "facebook.com" not in url:
            return url
    return None


# ──────────────────────────────────────────────────────────────────────────
# 2. DEEPSEEK ANALYSIS
# ──────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert investment analyst at Atomic Digital, a market-neutral digital asset hedge fund with a 2-year track record, a 27% average annual return, and a 4.0+ Sharpe ratio since inception. Your job is to research prospective investors (family offices, investment funds, wealth managers) from their public web presence and recommend which Atomic Digital product to pitch them. You always respond with a single valid JSON object and nothing else."""

ANALYSIS_INSTRUCTIONS = """Analyze the scraped public information about this prospect and determine which Atomic Digital product best fits their needs.

## ATOMIC DIGITAL PRODUCTS

{product_knowledge}

## YOUR TASK

Based ONLY on the scraped public information provided:

1. **Infer the prospect's investment profile** — their apparent AUM range, geographic focus, asset classes they invest in, investment style, crypto/digital asset experience, and any signals about their preferred denomination (USD vs. BTC), liquidity needs, transparency requirements, and desire for customization.

2. **Score each product** (0-100) on fit, with specific evidence-based fit factors and concerns.

3. **Recommend the primary product** to pitch, plus a secondary option if close.

4. **Provide talking points** for the first meeting — specific, actionable, referencing what you found about THEM (not generic sales pitches).

5. **Anticipate objections** they're likely to raise based on their profile.

6. **Identify data gaps** — what you couldn't determine from public info that should be confirmed in the meeting.

## SCORING GUIDELINES

- **Fund / Market Neutral Fund (70+ baseline)**: fits investors who want USD-denominated exposure to a market-neutral, multi-strategy fund, comfortable with monthly liquidity, turnkey fund structure, $100K+ ticket, traditional/TradFi allocators new to crypto or wanting a standardized product
- **Bitcoin Market Neutral Fund / btc_fund (65+ baseline)**: fits crypto-native investors who hold BTC as their base currency and want returns/exposure to stay BTC-denominated rather than converting to USD, comfortable with monthly liquidity, $100K+ ticket
- **SMA (70+ baseline)**: fits institutional/large private clients wanting a bespoke, customized mandate and terms, sophisticated allocators with their own custody/compliance infrastructure, larger ticket sizes, investors who want a dedicated account rather than a pooled vehicle

All three offerings share monthly liquidity — if the prospect clearly needs more frequent liquidity than that, flag it as a concern across all three rather than favoring one.

Adjust scores based on evidence. If the prospect emphasizes holding BTC as a reserve/treasury asset or is clearly crypto-native, lean btc_fund. If they emphasize "bespoke," "customized," or "institutional mandate," lean SMA. If they're TradFi-only, new to crypto, or emphasize USD reporting, lean the flagship Fund.

## PROSPECT INFORMATION

**Name:** {prospect_name}
**Website URL:** {website_url}
**LinkedIn URL:** {linkedin_url}

### SCRAPED WEBSITE CONTENT (homepage + subpages):
{website_content}

### TEAM / LEADERSHIP INFORMATION:
{team_info}

### WEB SEARCH RESULTS:
{search_results}

## OUTPUT — return ONLY valid JSON, no other text:

```json
{
  "prospect_summary": {
    "name": "string",
    "apparent_aum_range": "string or 'Unknown — not publicly available'",
    "geographic_focus": "string",
    "asset_classes": ["list"],
    "investment_style": "string (active/passive/quantitative/opportunistic/multi-asset/etc.)",
    "crypto_experience": "string (none/curious/experienced/native)",
    "denomination_signal": "string — does the prospect appear to prefer USD or BTC-denominated exposure?",
    "liquidity_signal": "string — what their portfolio suggests about liquidity needs",
    "customization_signal": "string — do they emphasize bespoke/standardized solutions?",
    "key_signals": ["specific, evidence-based signals extracted from the scraped content — cite actual details"]
  },
  "product_scores": {
    "fund": {
      "score": 0-100,
      "fit_factors": ["specific reasons with evidence"],
      "concerns": ["specific mismatches"]
    },
    "btc_fund": {
      "score": 0-100,
      "fit_factors": ["specific reasons with evidence"],
      "concerns": ["specific mismatches"]
    },
    "sma": {
      "score": 0-100,
      "fit_factors": ["specific reasons with evidence"],
      "concerns": ["specific mismatches"]
    }
  },
  "recommendation": {
    "primary": "fund | btc_fund | sma",
    "primary_score": 0-100,
    "secondary": "fund | btc_fund | sma or null",
    "secondary_score": 0-100,
    "rationale": "2-3 sentences explaining why this is the top pick, referencing specific evidence",
    "suggested_approach": "string — how to position the pitch (e.g. 'Start with X as entry point, then graduate to Y')",
    "talking_points": ["Specific talking point referencing what you found about THEM", "..."],
    "objection_handling": ["'Their likely objection' → 'How to address it'", "..."],
    "data_gaps": ["What wasn't available publicly — confirm in meeting", "..."]
  }
}
```

## CRITICAL RULES
- Cite specific evidence from the scraped content (e.g. "Their website mentions X, which suggests Y")
- If information is insufficient, say so — don't fabricate signals
- Be honest about data gaps rather than guessing
- Talking points should reference the prospect's actual team, strategy, or positioning
- Objection handling should be specific to THIS prospect's likely concerns, not generic"""


def _get_deepseek_client() -> OpenAI:
    """
    Build a client for DeepSeek's official API (OpenAI-compatible).
    Docs: https://api-docs.deepseek.com
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")

    if not api_key:
        raise RuntimeError(
            "Set DEEPSEEK_API_KEY (env var or .env file) — get a key at "
            "https://platform.deepseek.com (see the INSTALL section at the top of this file)."
        )

    return OpenAI(base_url="https://api.deepseek.com", api_key=api_key)


def analyze_prospect(
    prospect_name: str,
    website_url: str,
    linkedin_url: str,
    scraped: dict,
    search_results: list,
    model: str = None,
) -> dict:
    """
    Run DeepSeek analysis on scraped prospect data.
    `model` defaults to the DEEPSEEK_MODEL env var, or "deepseek-v4-flash"
    (the cheap/fast default) if that isn't set either. Use "deepseek-v4-pro"
    for deeper reasoning on higher-stakes prospects.
    """

    client = _get_deepseek_client()
    deployment = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

    # Format website content
    if scraped.get("error"):
        website_content = f"Unable to scrape website: {scraped['error']}"
    else:
        pages_text = scraped.get("all_text", "")
        if not pages_text:
            website_content = "No content extracted from website."
        else:
            website_content = pages_text

    team_info = scraped.get("team_info", "No team information extracted.")

    search_text = json.dumps(search_results[:8], indent=2) if search_results else "No search results."

    pk_str = json.dumps(PRODUCT_KNOWLEDGE, indent=2)

    user_message = ANALYSIS_INSTRUCTIONS         .replace("{product_knowledge}", pk_str)         .replace("{prospect_name}", prospect_name)         .replace("{website_url}", website_url or "Not provided")         .replace("{linkedin_url}", linkedin_url or "Not provided")         .replace("{website_content}", website_content)         .replace("{team_info}", team_info)         .replace("{search_results}", search_text)

    print(f"  Analyzing with DeepSeek ({deployment})...")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    # DeepSeek's API supports OpenAI-style JSON mode — try it first, fall
    # back to a plain call + regex extraction if the specific deployment
    # doesn't support the parameter.
    try:
        response = client.chat.completions.create(
            model=deployment,
            max_tokens=8000,
            response_format={"type": "json_object"},
            messages=messages,
        )
    except Exception as e:
        print(f"    (response_format=json_object rejected [{e}] — retrying without it)")
        response = client.chat.completions.create(
            model=deployment,
            max_tokens=8000,
            messages=messages,
        )

    raw = response.choices[0].message.content

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"raw_output": raw, "error": "JSON parse failed"}


# ──────────────────────────────────────────────────────────────────────────
# 3. REPORT FORMATTING
# ──────────────────────────────────────────────────────────────────────────

def format_report(name: str, analysis: dict) -> str:
    """Format analysis into a clean pre-meeting brief."""
    if "error" in analysis and "raw_output" in analysis:
        return f"ERROR: {analysis['error']}\n\nRaw:\n{analysis['raw_output']}"

    ps = analysis.get("prospect_summary", {})
    scores = analysis.get("product_scores", {})
    rec = analysis.get("recommendation", {})

    lines = []
    lines.append("=" * 72)
    lines.append("  ATOMIC DIGITAL — PRE-MEETING PRODUCT FIT BRIEF")
    lines.append(f"  Prospect: {name}")
    lines.append(f"  Generated: {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 72)
    lines.append("")

    # Prospect profile
    lines.append("PROSPECT PROFILE (inferred from public sources)")
    lines.append("-" * 50)
    lines.append(f"  AUM:          {ps.get('apparent_aum_range', 'Unknown')}")
    lines.append(f"  Geography:    {ps.get('geographic_focus', 'Unknown')}")
    lines.append(f"  Asset classes: {', '.join(ps.get('asset_classes', ['Unknown']))}")
    lines.append(f"  Style:        {ps.get('investment_style', 'Unknown')}")
    lines.append(f"  Crypto exp:   {ps.get('crypto_experience', 'Unknown')}")
    lines.append(f"  Custody:      {ps.get('custody_signal', 'Unknown')}")
    lines.append(f"  Liquidity:    {ps.get('liquidity_signal', 'Unknown')}")
    lines.append(f"  Customization: {ps.get('customization_signal', 'Unknown')}")
    lines.append("")

    lines.append("  Key signals:")
    for s in ps.get("key_signals", []):
        lines.append(f"    • {s}")
    lines.append("")

    # Scores
    lines.append("PRODUCT FIT SCORES")
    lines.append("-" * 50)
    names = {
        "fund": "MARKET NEUTRAL FUND (USD)",
        "btc_fund": "BITCOIN MARKET NEUTRAL FUND (BTC)",
        "sma": "SEPARATELY MANAGED ACCOUNT (SMA)",
    }
    for key in ["fund", "btc_fund", "sma"]:
        p = scores.get(key, {})
        score = p.get("score", 0)
        bar_len = score // 5
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(f"  {names[key]}")
        lines.append(f"  [{bar}] {score}/100")
        lines.append("  Fit factors:")
        for f in p.get("fit_factors", []):
            lines.append(f"    + {f}")
        for c in p.get("concerns", []):
            lines.append(f"    - {c}")
        lines.append("")

    # Recommendation
    lines.append("=" * 50)
    primary_name = names.get(rec.get("primary", ""), rec.get("primary", ""))
    lines.append(f"  RECOMMENDED: {primary_name}")
    lines.append(f"  Score: {rec.get('primary_score', 0)}/100")
    lines.append("=" * 50)
    lines.append("")
    lines.append(f"  Rationale: {rec.get('rationale', '')}")
    lines.append("")
    lines.append(f"  Suggested approach: {rec.get('suggested_approach', '')}")
    lines.append("")

    sec = rec.get("secondary")
    if sec:
        sec_name = names.get(sec, sec)
        lines.append(f"  Secondary: {sec_name} ({rec.get('secondary_score', 0)}/100)")
        lines.append("")

    # Talking points
    lines.append("MEETING TALKING POINTS")
    lines.append("-" * 50)
    for i, tp in enumerate(rec.get("talking_points", []), 1):
        lines.append(f"  {i}. {tp}")
    lines.append("")

    # Objections
    if rec.get("objection_handling"):
        lines.append("ANTICIPATED OBJECTIONS")
        lines.append("-" * 50)
        for obj in rec["objection_handling"]:
            lines.append(f"  • {obj}")
        lines.append("")

    # Data gaps
    if rec.get("data_gaps"):
        lines.append("CONFIRM IN MEETING (data gaps)")
        lines.append("-" * 50)
        for gap in rec["data_gaps"]:
            lines.append(f"  ? {gap}")
        lines.append("")

    lines.append("-" * 72)
    lines.append("  Generated by Atomic Digital Product-Fit Advisor")
    lines.append("  Pre-meeting prep aid — not investment advice.")
    lines.append("-" * 72)

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# 3b. PDF REPORT (structured, downloadable)
# ──────────────────────────────────────────────────────────────────────────

PRODUCT_LABELS = {
    "fund": "Market Neutral Fund (USD)",
    "btc_fund": "Bitcoin Market Neutral Fund (BTC)",
    "sma": "Separately Managed Account (SMA)",
}

# fpdf2's core fonts (Helvetica etc.) only support Latin-1. Rather than
# bundling a Unicode TTF font just to render a few bullets and arrows, swap
# the handful of characters the analysis text actually uses for plain-ASCII
# equivalents — keeps this dependency-free.
_PDF_CHAR_MAP = {
    "\u2022": "-", "\u2192": "->", "\u2013": "-", "\u2014": "--",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u2713": "v", "\u2717": "x",
}


def _pdf_safe(text) -> str:
    if text is None:
        return ""
    text = str(text)
    for bad, good in _PDF_CHAR_MAP.items():
        text = text.replace(bad, good)
    # Final safety net: drop anything else outside Latin-1 (e.g. stray emoji)
    # rather than letting fpdf2 raise on it.
    return text.encode("latin-1", errors="ignore").decode("latin-1")


def generate_pdf_report(name: str, analysis: dict, output_path: str) -> None:
    """
    Render the same analysis dict format_report() uses into a structured,
    downloadable PDF — with an actual drawn score bar per product rather
    than ASCII block characters (which the PDF's core font can't render).
    """
    if not HAS_FPDF:
        raise RuntimeError("PDF export needs fpdf2 — install with: pip install fpdf2")

    if "error" in analysis and "raw_output" in analysis:
        raise RuntimeError(f"Can't export a malformed analysis to PDF: {analysis['error']}")

    ps = analysis.get("prospect_summary", {})
    scores = analysis.get("product_scores", {})
    rec = analysis.get("recommendation", {})

    ACCENT_RGB = (184, 121, 31)
    GOOD_RGB = (46, 139, 87)
    BAD_RGB = (192, 57, 43)
    FAINT_RGB = (120, 120, 120)
    TRACK_RGB = (230, 230, 230)

    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()

    # NOTE ON LAYOUT: fpdf2's multi_cell(), when new_x/new_y aren't passed
    # explicitly, leaves the cursor at the right edge of its own text box —
    # NOT back at the left margin like you'd expect from a "paragraph" call.
    # Every helper below passes new_x=XPos.LMARGIN, new_y=YPos.NEXT
    # explicitly so each block reliably starts the next line at the left
    # margin, regardless of fpdf2's own defaults.

    def h1(text, size=16, gap=4):
        pdf.set_font("Helvetica", "B", size)
        pdf.set_text_color(20, 20, 20)
        pdf.multi_cell(0, size * 0.6, _pdf_safe(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(gap)

    def h2(text, gap=3):
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(*ACCENT_RGB)
        pdf.multi_cell(0, 7, _pdf_safe(text.upper()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_draw_color(220, 220, 220)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(gap)

    def body(text, size=10, color=(30, 30, 30), gap=2):
        pdf.set_font("Helvetica", "", size)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, 5.5, _pdf_safe(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(gap)

    def line(text, size=9.5, style="", color=(30, 30, 30)):
        pdf.set_font("Helvetica", style, size)
        pdf.set_text_color(*color)
        pdf.cell(0, 6, _pdf_safe(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def label_value(label, value):
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(90, 90, 90)
        pdf.cell(38, 6, _pdf_safe(label))
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(0, 6, _pdf_safe(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def bullet_list(items, prefix="-", color=(30, 30, 30)):
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*color)
        for item in items:
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(0, 5.5, f"{prefix} {_pdf_safe(item)}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)

    # ---- Title ----
    h1("ATOMIC DIGITAL", size=18, gap=1)
    line("Pre-Meeting Product Fit Brief", color=(90, 90, 90))
    line(name, size=13, style="B", color=(20, 20, 20))
    line(f"Generated: {time.strftime('%Y-%m-%d %H:%M')}", size=9, color=FAINT_RGB)
    pdf.ln(4)

    # ---- Prospect profile ----
    h2("Prospect Profile")
    label_value("AUM:", ps.get("apparent_aum_range", "Unknown"))
    label_value("Geography:", ps.get("geographic_focus", "Unknown"))
    label_value("Asset classes:", ", ".join(ps.get("asset_classes", ["Unknown"])))
    label_value("Style:", ps.get("investment_style", "Unknown"))
    label_value("Crypto exp:", ps.get("crypto_experience", "Unknown"))
    label_value("Denomination:", ps.get("denomination_signal", "Unknown"))
    label_value("Liquidity:", ps.get("liquidity_signal", "Unknown"))
    label_value("Customization:", ps.get("customization_signal", "Unknown"))
    pdf.ln(2)

    if ps.get("key_signals"):
        line("Key signals:", style="B", color=(60, 60, 60))
        bullet_list(ps["key_signals"])
    pdf.ln(2)

    # ---- Product fit scores (with a real drawn bar) ----
    h2("Product Fit Scores")
    for key in ["fund", "btc_fund", "sma"]:
        p = scores.get(key, {})
        score = max(0, min(100, int(p.get("score", 0) or 0)))
        label = PRODUCT_LABELS.get(key, key)

        pdf.set_font("Helvetica", "B", 10.5)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(120, 6, _pdf_safe(label))
        pdf.set_font("Helvetica", "B", 10.5)
        pdf.set_text_color(*ACCENT_RGB)
        pdf.cell(0, 6, f"{score}/100", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        bar_y = pdf.get_y()
        bar_w = pdf.w - pdf.l_margin - pdf.r_margin
        bar_h = 3.2
        pdf.set_fill_color(*TRACK_RGB)
        pdf.rect(pdf.l_margin, bar_y, bar_w, bar_h, style="F")
        pdf.set_fill_color(*ACCENT_RGB)
        pdf.rect(pdf.l_margin, bar_y, bar_w * (score / 100), bar_h, style="F")
        pdf.ln(bar_h + 2)

        bullet_list(p.get("fit_factors", []), prefix="+", color=GOOD_RGB)
        bullet_list(p.get("concerns", []), prefix="-", color=BAD_RGB)
        pdf.ln(2)

    # ---- Recommendation ----
    primary_label = PRODUCT_LABELS.get(rec.get("primary", ""), rec.get("primary", ""))
    h2("Recommendation")
    line(f"{primary_label} -- {rec.get('primary_score', 0)}/100", size=12, style="B", color=ACCENT_RGB)
    pdf.ln(1)
    body(rec.get("rationale", ""))
    if rec.get("suggested_approach"):
        line("Suggested approach:", style="B", color=(60, 60, 60))
        body(rec["suggested_approach"])
    if rec.get("secondary"):
        sec_label = PRODUCT_LABELS.get(rec["secondary"], rec["secondary"])
        line(f"Secondary option: {sec_label} ({rec.get('secondary_score', 0)}/100)", color=FAINT_RGB)
    pdf.ln(3)

    # ---- Talking points / objections / data gaps ----
    if rec.get("talking_points"):
        h2("Meeting Talking Points")
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(30, 30, 30)
        for i, tp in enumerate(rec["talking_points"], 1):
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(0, 5.5, _pdf_safe(f"{i}. {tp}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)

    if rec.get("objection_handling"):
        h2("Anticipated Objections")
        bullet_list(rec["objection_handling"])
        pdf.ln(1)

    if rec.get("data_gaps"):
        h2("Confirm In Meeting")
        bullet_list(rec["data_gaps"], prefix="?")

    # ---- Footer disclaimer ----
    pdf.ln(4)
    pdf.set_draw_color(220, 220, 220)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*FAINT_RGB)
    pdf.multi_cell(0, 4.5, "Generated by Atomic Digital Product-Fit Advisor. Pre-meeting prep aid -- not investment advice.")

    pdf.output(str(output_path))


# ──────────────────────────────────────────────────────────────────────────
# 4. ORCHESTRATION
# ──────────────────────────────────────────────────────────────────────────

def run_prospect(name: str, website=None, linkedin: str = None, allow_selenium: bool = True,
                  model: str = None) -> dict:
    """
    Full pipeline: scrape → search → analyze → return analysis dict.
    `website` accepts a single URL string, a list of URLs (scraped and
    merged together), or None (falls back to searching for one by name).
    """

    print(f"\n{'─' * 50}")
    print(f"  Processing: {name}")
    print(f"{'─' * 50}")

    if isinstance(website, str):
        websites = [website]
    else:
        websites = list(website) if website else []
    websites = [w.strip() for w in websites if w and w.strip()]

    search_results = []

    # Find a website if none provided
    if not websites:
        print("  No website provided — searching...")
        found = find_website_from_name(name)
        if found:
            websites = [found]
            print(f"  Found: {found}")
        else:
            print("  No website found — using search results only.")
            search_results = google_search(name)

    # Scrape website(s)
    scraped = {"all_text": "", "team_info": ""}
    if websites:
        scraped = scrape_multiple_websites(websites, allow_selenium=allow_selenium)

        # Also run a search for additional context
        if not search_results:
            search_results = google_search(name)

    # Analyze
    website_url_for_prompt = ", ".join(websites) if websites else ""
    analysis = analyze_prospect(
        prospect_name=name,
        website_url=website_url_for_prompt,
        linkedin_url=linkedin or "",
        scraped=scraped,
        search_results=search_results,
        model=model,
    )

    return analysis


def main():
    parser = argparse.ArgumentParser(
        description="Atomic Digital Product-Fit Advisor — scrape a family office/fund website and get a product recommendation."
    )
    parser.add_argument("--name", help="Name of the family office / fund / prospect")
    parser.add_argument("--website", help="Website URL(s) to scrape — separate multiple with a comma or pipe")
    parser.add_argument("--linkedin", help="LinkedIn company page URL (for reference)")
    parser.add_argument("--batch", help="CSV file with columns: name, website, linkedin")
    parser.add_argument("--outdir", default="reports", help="Output directory for batch reports")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of formatted report")
    parser.add_argument("--output", "-o", help="Save single report to file")
    parser.add_argument("--no-selenium", action="store_true",
                         help="Disable the headless-browser fallback for JS-heavy sites (requests-only)")
    parser.add_argument("--model", default=None,
                         help="DeepSeek model to use (default: reads DEEPSEEK_MODEL env var, or "
                              "'deepseek-v4-flash'. Use 'deepseek-v4-pro' for deeper reasoning.)")
    args = parser.parse_args()

    allow_selenium = not args.no_selenium
    if allow_selenium and not HAS_SELENIUM:
        print("  (note: selenium not installed — JS-heavy sites may scrape thin. `pip install selenium` to enable fallback.)")

    # Batch mode
    if args.batch:
        import csv
        outdir = Path(args.outdir)
        outdir.mkdir(exist_ok=True)

        with open(args.batch) as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("name", "").strip()
                # Support multiple URLs in one CSV cell, separated by "|" or ",".
                website_raw = row.get("website", "").strip()
                website = [w.strip() for w in re.split(r'[|,]', website_raw) if w.strip()] or None
                linkedin = row.get("linkedin", "").strip() or None

                if not name:
                    continue

                try:
                    analysis = run_prospect(name, website, linkedin, allow_selenium=allow_selenium, model=args.model)

                    if args.json:
                        output = json.dumps(analysis, indent=2)
                    else:
                        output = format_report(name, analysis)

                    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', name)
                    outpath = outdir / f"{safe_name}.txt"
                    outpath.write_text(output)
                    print(f"  → Saved: {outpath}")
                except Exception as e:
                    print(f"  ✗ FAILED on '{name}': {e}  — continuing with next prospect.")

        _close_selenium_driver()
        print(f"\n✓ Batch complete. Reports in {outdir}/")
        return

    # Single prospect
    if not args.name:
        parser.error("--name is required (or use --batch for multiple prospects)")

    try:
        websites = [w.strip() for w in re.split(r'[|,]', args.website)] if args.website else None
        analysis = run_prospect(args.name, websites, args.linkedin, allow_selenium=allow_selenium, model=args.model)
    finally:
        _close_selenium_driver()

    if args.json:
        output = json.dumps(analysis, indent=2)
    else:
        output = format_report(args.name, analysis)

    if args.output:
        Path(args.output).write_text(output)
        print(f"\n✓ Report saved to: {args.output}")
    else:
        print("\n" + output)


if __name__ == "__main__":
    main()

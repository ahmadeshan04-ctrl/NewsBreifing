#!/usr/bin/env python3

import calendar
import email as email_lib
import html as html_lib
import imaplib
import os
import re
import smtplib
import feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# How recent an article has to be to reach the prompt. Kept tight for markets
# (yesterday's index move is not today's news) and looser for the slower-moving
# sector/deal beats.
MARKET_MAX_AGE_DAYS = 3
SECTOR_MAX_AGE_DAYS = 10
RESTRUCTURING_MAX_AGE_DAYS = 21

# If a feed's newest item is older than this, treat the whole feed as dead and
# skip it — this is what stops an abandoned feed (e.g. the old WSJ RSS, frozen
# since Jan 2025) from silently pinning a section to the same headlines forever.
STALE_FEED_DAYS = 14

# Broad market / macro coverage. WSJ's own RSS feeds (feeds.a.dj.com/*) were
# abandoned in early 2025 and only ever return January 2025 headlines, so they
# were removed; these are live and carry real article summaries to ground the
# briefing.
MARKET_FEEDS = [
    ("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("NPR Economy",  "https://feeds.npr.org/1017/rss.xml"),
    ("BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml"),
]

FINANCE_FEEDS = [
    ("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Finance",  "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("CNBC Business", "https://www.cnbc.com/id/10000115/device/rss/rss.html"),
]

HEALTHCARE_FEEDS = [
    ("STAT News",         "https://www.statnews.com/feed/"),
    ("Fierce Healthcare", "https://www.fiercehealthcare.com/rss.xml"),
    ("Healthcare Dive",   "https://www.healthcaredive.com/feeds/news/"),
]

RESTRUCTURING_FIRMS = ["EY-Parthenon", "Alvarez & Marsal", "FTI Consulting", "AlixPartners"]

# Google News RSS is the source for per-firm coverage: it's reliably updated,
# every entry carries a real publish date, and `when:Nd` scopes by recency.
# Its entry "summary" is only a link, so the title is all the model gets — the
# prompt is told not to infer deal specifics that aren't in the headline.
RESTRUCTURING_FIRM_MATCH_TERMS = {
    "EY-Parthenon": ["ey-parthenon", "ey parthenon"],
    "Alvarez & Marsal": ["alvarez & marsal", "alvarez and marsal", "alvarez marsal", "a&m"],
    "FTI Consulting": ["fti consulting", "fti "],
    "AlixPartners": ["alixpartners", "alix partners"],
}

# A firm's name in a headline isn't enough — "Careers at EY", "Welcome Back
# Spotlight", award announcements etc. all match. Require a substantive term too,
# and drop the obvious non-news.
_RESTRUCTURING_TOPIC_TERMS = [
    "restructur", "bankrupt", "chapter 11", "chapter 7", "insolven", "turnaround",
    "distress", "creditor", "liquidat", "administration", "receivership",
    "advis", "mandate", "retained", "engaged", "acqui", "merger", "deal",
    "hire", "hires", "appoint", "names ", "joins", "poach",
]
_RESTRUCTURING_EXCLUDE_TERMS = [
    "careers at", "welcome back", "spotlight", "day in the life",
    "best places to work", "wins award", "award for", "obituary",
    # Algorithmic 13F / brokerage-filing and sell-side-rating spam that mentions
    # a firm only as a ticker.
    "buys new stake", "sells shares", "acquires shares", "acquires a new",
    "shares purchased", "shares sold", "stake in", "position in", "holdings in",
    "price target", "average rating", "equities analyst", "analysts' ratings",
    "13f", "$fcn", "market cap", "short interest", "p/e ratio",
]


def _restructuring_feed_url(firm: str) -> str:
    query = (
        f'"{firm}" (restructuring OR bankruptcy OR turnaround OR "Chapter 11" '
        f'OR creditors OR distressed OR advisory OR mandate OR hire) '
        f'when:{RESTRUCTURING_MAX_AGE_DAYS}d'
    )
    return (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )


def _is_relevant_to_firm(article: dict, firm: str) -> bool:
    haystack = (article["title"] + " " + article["description"]).lower()
    if not any(term in haystack for term in RESTRUCTURING_FIRM_MATCH_TERMS[firm]):
        return False
    if any(term in haystack for term in _RESTRUCTURING_EXCLUDE_TERMS):
        return False
    return any(term in haystack for term in _RESTRUCTURING_TOPIC_TERMS)


def fetch_restructuring_articles(firm: str, limit: int = 6) -> list[dict]:
    """Fetch this firm's recent restructuring/deal headlines from Google News."""
    feed = [(firm, _restructuring_feed_url(firm))]
    articles = fetch_from_feeds(
        feed, limit_per_feed=limit * 4, max_age_days=RESTRUCTURING_MAX_AGE_DAYS,
        stale_check=False,
    )
    articles = [a for a in articles if _is_relevant_to_firm(a, firm)]
    return articles[:limit]


_FEED_REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBriefingBot/1.0)"}


def _strip_html(text: str) -> str:
    """Strip tags/scripts from an HTML fragment and unescape entities."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _entry_datetime(entry) -> datetime | None:
    """UTC datetime for a feed entry, or None if it carries no usable date."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def fetch_from_feeds(
    feeds: list[tuple[str, str]],
    limit_per_feed: int = 6,
    max_age_days: int | None = None,
    stale_check: bool = True,
) -> list[dict]:
    """Fetch and normalize entries from a list of (name, url) feeds.

    Entries older than `max_age_days` are dropped. With `stale_check`, a feed
    whose newest entry is older than STALE_FEED_DAYS is skipped entirely and
    reported — the guard against abandoned feeds quietly freezing a section.
    (Turn it off for search feeds, where a genuinely quiet topic looks the same
    as a dead feed.)
    """
    now = datetime.now(timezone.utc)
    articles = []
    for source_name, url in feeds:
        try:
            feed = feedparser.parse(url, request_headers=_FEED_REQUEST_HEADERS)
            dated = [(_entry_datetime(e), e) for e in feed.entries]
            newest = max((d for d, _ in dated if d), default=None)
            if stale_check and newest and (now - newest).days > STALE_FEED_DAYS:
                print(
                    f"  Warning: {source_name} looks stale — newest item is "
                    f"{(now - newest).days} days old ({url}); skipping feed."
                )
                continue

            kept = 0
            for entry_dt, entry in dated:
                if kept >= limit_per_feed:
                    break
                if max_age_days is not None and entry_dt and (now - entry_dt).days > max_age_days:
                    continue
                title = _strip_html(entry.get("title", ""))
                if not title:
                    continue
                summary = _strip_html(entry.get("summary") or entry.get("description") or "")
                # Some feeds (Google News especially) just echo the headline as
                # the summary — that adds nothing and inflates the prompt.
                norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
                if norm(summary)[:60] == norm(title)[:60]:
                    summary = ""
                articles.append({
                    "source": source_name,
                    "title": title,
                    "description": summary,
                    "date": entry_dt.strftime("%b %d") if entry_dt else "",
                })
                kept += 1
        except Exception as e:
            print(f"  Warning: could not fetch {source_name} ({url}): {e}")
    return articles


def _dedupe_articles(*pools: list[dict]) -> None:
    """Drop articles whose title already appeared in an earlier pool, in place —
    so the same story isn't fed to (and written up in) two sections."""
    seen: set[str] = set()
    for pool in pools:
        kept = []
        for article in pool:
            key = re.sub(r"[^a-z0-9]+", " ", article["title"].lower()).strip()
            key = " ".join(key.split()[:12])
            if key and key in seen:
                continue
            seen.add(key)
            kept.append(article)
        pool[:] = kept


def fetch_wsj_newsletter(gmail_address: str, gmail_app_password: str, lookback_days: int = 3) -> str:
    """Fetch the most recent WSJ 10-Point newsletter email via IMAP.

    Assumes the newsletter either arrives at, or is forwarded to, this same
    Gmail inbox. Returns the extracted body text, or "" if nothing was found
    (e.g. weekends, when WSJ doesn't send it, or forwarding isn't set up yet).
    """
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(gmail_address, gmail_app_password)
        imap.select("INBOX")

        since_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        status, data = imap.search(None, f'(SINCE "{since_date}" SUBJECT "10-Point")')
        if status != "OK" or not data[0]:
            imap.logout()
            return ""

        latest_id = data[0].split()[-1]
        # BODY.PEEK avoids marking the email as read in your inbox
        status, msg_data = imap.fetch(latest_id, "(BODY.PEEK[])")
        imap.logout()
        if status != "OK" or not msg_data or not msg_data[0]:
            return ""

        msg = email_lib.message_from_bytes(msg_data[0][1])
        return _extract_email_text(msg)
    except Exception as e:
        print(f"  Warning: could not fetch WSJ 10-Point newsletter: {e}")
        return ""


def _extract_email_text(msg: email_lib.message.Message) -> str:
    """Pull readable text out of an email message, preferring text/plain."""
    body_plain, body_html = None, None

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and body_plain is None:
                payload = part.get_payload(decode=True)
                if payload:
                    body_plain = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ctype == "text/html" and body_html is None:
                payload = part.get_payload(decode=True)
                if payload:
                    body_html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                body_html = text
            else:
                body_plain = text

    text = body_plain or (_strip_html(body_html) if body_html else "")
    return text.strip()[:6000]


def format_articles_for_prompt(articles: list[dict]) -> str:
    if not articles:
        return "(no headlines available)"
    lines = []
    for i, article in enumerate(articles, 1):
        date = f", {article['date']}" if article.get("date") else ""
        lines.append(f"{i}. [{article['source']}{date}] {article['title']}")
        if article.get("description"):
            # Trim very long summaries
            desc = article["description"][:400]
            lines.append(f"   {desc}")
    return "\n".join(lines)


def format_restructuring_for_prompt(articles_by_firm: dict[str, list[dict]]) -> str:
    blocks = []
    for firm, articles in articles_by_firm.items():
        blocks.append(f"=== {firm} ===")
        blocks.append(
            format_articles_for_prompt(articles)
            if articles
            else "(no relevant headlines in the past few weeks)"
        )
    return "\n".join(blocks)


def generate_briefing(
    client: Anthropic,
    market_articles: list[dict],
    wsj_newsletter_text: str,
    finance_articles: list[dict],
    healthcare_articles: list[dict],
    restructuring_by_firm: dict[str, list[dict]],
) -> str:
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    market_text = format_articles_for_prompt(market_articles)
    finance_text = format_articles_for_prompt(finance_articles)
    healthcare_text = format_articles_for_prompt(healthcare_articles)
    restructuring_text = format_restructuring_for_prompt(restructuring_by_firm)

    newsletter_section = (
        wsj_newsletter_text
        if wsj_newsletter_text
        else "(No 10-Point email found today — skip this section with a single sentence noting it wasn't available.)"
    )

    prompt = f"""Today is {today}. You are writing a sharp, professional morning briefing email for a financially-aware reader.

Every headline below is tagged with its source and publish date, e.g. [CNBC Markets, Aug 28]. Work only from the material provided here.

GROUND RULES — follow these exactly:
- Use only facts that appear in the headlines and summaries below. Do NOT add company names, ticker symbols, dollar figures, percentages, counterparties, dates, or deal terms that are not explicitly present in the source text. If a detail a reader would want is not in the source, say it wasn't disclosed rather than filling it in.
- Respect the dates. Today is {today}. Never describe something dated days ago as if it happened "today" or "this morning" — say "on Aug 28" or "this week". If the freshest item in a section is several days old, open that section by saying it's a quiet news day for that beat.
- If a section's material is missing, thin, or off-topic, cover fewer stories — or say there's nothing material to report. Padding a section with generic commentary is worse than a short section.
- No outside knowledge, no forecasts of specific numbers, no invented quotes.

MARKET / MACRO HEADLINES:
{market_text}

WSJ 10-POINT NEWSLETTER CONTENT (forwarded email):
{newsletter_section}

GENERAL FINANCE HEADLINES:
{finance_text}

HEALTHCARE SECTOR HEADLINES:
{healthcare_text}

RESTRUCTURING / DEAL HEADLINES for four consulting firms (Google News; headline only, no summary text — do not infer specifics beyond what the headline says):
{restructuring_text}

Write a morning briefing with exactly five sections, in this order. For sections 1, 3, and 4, pick the 3–5 most significant stories and write 2–3 sentences each: what happened (with the date), why it matters, what to watch.

Section 1 — MARKETS & MACRO: Index moves, rates, commodities, the Fed and other central banks, economic data.
Section 2 — WSJ 10-POINT: Summarize the newsletter content above story by story (skip anything already covered in Section 1). If no newsletter text was provided, note that in one sentence and move on.
Section 3 — FINANCE: Broader finance and business news — earnings, M&A, corporate strategy, macro trends not already covered above.
Section 4 — HEALTHCARE: Biotech, pharma, hospital systems, payers, regulation.
Section 5 — RESTRUCTURING CONSULTING: One <h3> subsection per firm — EY-Parthenon, Alvarez & Marsal, FTI Consulting, AlixPartners. Under each, summarize only what its headlines actually say (a named engagement, a hire, an acquisition, a results note) in 1–3 sentences, with the date. If a firm has no relevant headline, write exactly one sentence: "No notable update in the past few weeks." Do not describe a deal, client, or figure that isn't in that firm's headlines.

Format strictly as HTML (no <html>/<head>/<body> tags):
- <h2> for section headers
- <h3 style="margin-bottom:4px"> for each story headline or firm name (punchy, under 10 words)
- <p style="margin-top:4px"> for the analysis paragraph
- Wrap key figures/numbers that came from the source in <strong>

Open with a single <p><em>one-sentence overview of the overall tone of today's news</em></p> before the sections."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in message.content if block.type == "text")


def build_email_html(briefing_content: str, recipient_name: str = "") -> str:
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    greeting = f"Good morning{', ' + recipient_name if recipient_name else ''}."

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Georgia,'Times New Roman',serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:24px 0;">
    <tr><td align="center">
      <table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">

        <!-- Header -->
        <tr><td style="background-color:#0d1b2a;border-radius:8px 8px 0 0;padding:28px 36px;">
          <p style="margin:0;font-family:Georgia,serif;font-size:11px;letter-spacing:3px;color:#778da9;text-transform:uppercase;">Daily Briefing</p>
          <h1 style="margin:6px 0 0;font-family:Georgia,serif;font-size:26px;font-weight:700;color:#e0e1dd;letter-spacing:0.5px;">The Morning Report</h1>
          <p style="margin:8px 0 0;font-size:13px;color:#778da9;">{today}</p>
        </td></tr>

        <!-- Greeting bar -->
        <tr><td style="background-color:#1b2838;padding:14px 36px;border-left:1px solid #1e3a5f;border-right:1px solid #1e3a5f;">
          <p style="margin:0;font-size:14px;font-style:italic;color:#a8b2bf;">{greeting} Here's what's moving markets, finance, healthcare, and restructuring today.</p>
        </td></tr>

        <!-- Body -->
        <tr><td style="background-color:#ffffff;padding:32px 36px;border:1px solid #d1d5db;border-top:none;">
          <div style="font-size:15px;line-height:1.75;color:#1f2937;">
            {briefing_content}
          </div>
        </td></tr>

        <!-- Footer -->
        <tr><td style="background-color:#f8f9fa;border:1px solid #d1d5db;border-top:none;border-radius:0 0 8px 8px;padding:16px 36px;text-align:center;">
          <p style="margin:0;font-size:11px;color:#9ca3af;letter-spacing:0.3px;">
            Automated Morning Briefing &middot; Powered by Claude AI &middot; {today}
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_email(html: str, subject: str, sender: str, password: str, recipient: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Morning Briefing <{sender}>"
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())


def main() -> None:
    missing = [v for v in ("ANTHROPIC_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD") if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Missing required environment variables / GitHub secrets: {', '.join(missing)}")

    def clean_email(val: str) -> str:
        return re.sub(r'\s+', '', val)

    anthropic_key = os.environ["ANTHROPIC_API_KEY"].strip()
    gmail_address = clean_email(os.environ["GMAIL_ADDRESS"])
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"].strip()
    recipient_email = clean_email(os.environ.get("RECIPIENT_EMAIL", gmail_address))
    recipient_name = os.environ.get("RECIPIENT_NAME", "").strip()

    print("Fetching market / macro news...")
    market_articles = fetch_from_feeds(MARKET_FEEDS, max_age_days=MARKET_MAX_AGE_DAYS)

    print("Fetching WSJ 10-Point newsletter from email...")
    wsj_newsletter_text = fetch_wsj_newsletter(gmail_address, gmail_app_password)
    print("  Found WSJ 10-Point email." if wsj_newsletter_text else "  No WSJ 10-Point email found for today.")

    print("Fetching general finance news...")
    finance_articles = fetch_from_feeds(FINANCE_FEEDS, max_age_days=MARKET_MAX_AGE_DAYS)

    print("Fetching healthcare sector news...")
    healthcare_articles = fetch_from_feeds(HEALTHCARE_FEEDS, max_age_days=SECTOR_MAX_AGE_DAYS)

    print("Fetching restructuring consulting news, per firm...")
    restructuring_by_firm = {firm: fetch_restructuring_articles(firm) for firm in RESTRUCTURING_FIRMS}

    # Keep the same story from showing up in two sections.
    _dedupe_articles(market_articles, finance_articles, healthcare_articles,
                     *restructuring_by_firm.values())

    for firm, articles in restructuring_by_firm.items():
        print(f"    {firm}: {len(articles)} headlines")

    print(
        f"  Market: {len(market_articles)}, Finance: {len(finance_articles)}, "
        f"Healthcare: {len(healthcare_articles)}, "
        f"Restructuring: {sum(len(a) for a in restructuring_by_firm.values())}"
    )

    if not any(
        [market_articles, finance_articles, healthcare_articles, wsj_newsletter_text]
        + list(restructuring_by_firm.values())
    ):
        raise RuntimeError("No articles fetched — all RSS feeds failed. Check network access.")

    print("Generating briefing with Claude...")
    client = Anthropic(api_key=anthropic_key)
    briefing_content = generate_briefing(
        client, market_articles, wsj_newsletter_text, finance_articles, healthcare_articles, restructuring_by_firm
    )

    today_short = datetime.now(timezone.utc).strftime("%A, %B %d")
    subject = f"Morning Briefing — {today_short}"
    html_email = build_email_html(briefing_content, recipient_name)

    print(f"Sending email to {recipient_email}...")
    send_email(html_email, subject, gmail_address, gmail_app_password, recipient_email)

    print("Done — morning briefing sent.")


if __name__ == "__main__":
    main()

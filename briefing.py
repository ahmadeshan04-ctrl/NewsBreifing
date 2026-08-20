#!/usr/bin/env python3

import email as email_lib
import html as html_lib
import imaplib
import os
import re
import smtplib
import feedparser
from datetime import datetime, timedelta
from urllib.parse import quote
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

WSJ_FEEDS = [
    ("WSJ Markets",   "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
]

FINANCE_FEEDS = [
    ("BBC Business",  "http://feeds.bbci.co.uk/news/business/rss.xml"),
    ("MarketWatch",   "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("CNBC Finance",  "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]

HEALTHCARE_FEEDS = [
    ("STAT News",         "https://www.statnews.com/feed/"),
    ("Fierce Healthcare", "https://www.fiercehealthcare.com/rss.xml"),
    ("Healthcare Dive",   "https://www.healthcaredive.com/feeds/news/"),
]

# The restructuring-consulting firms don't publish their own deal RSS feeds.
# Google News' RSS entries link to a Google-hosted redirect page with no real
# content, so this uses Bing News search instead — its RSS descriptions
# contain actual deal detail (advisors, parties, deal terms), which is what
# lets the briefing say something substantive about each firm's active deals.
RESTRUCTURING_FIRMS = ["EY-Parthenon", "Alvarez & Marsal", "FTI Consulting", "AlixPartners"]

# Bing's news search sometimes pads a quoted-phrase query with loosely related
# "trending" stories that don't actually mention the firm — this is the
# post-filter that catches those before they reach the prompt.
RESTRUCTURING_FIRM_MATCH_TERMS = {
    "EY-Parthenon": ["ey-parthenon", "ey parthenon"],
    "Alvarez & Marsal": ["alvarez & marsal", "alvarez and marsal", "alvarez"],
    "FTI Consulting": ["fti consulting"],
    "AlixPartners": ["alixpartners", "alix partners"],
}

# Bing's `qft=interval="N"` scopes results by recency: 8 = past week, 9 = past
# month. Try the narrower window first so the briefing stays current; widen
# to a month only if a firm had too little coverage this week.
def _restructuring_feed_url(firm: str, interval: int) -> str:
    query = f'"{firm}" (restructuring OR bankruptcy OR turnaround OR "Chapter 11" OR advisory OR deal)'
    return (
        "https://www.bing.com/news/search?q="
        + quote(query)
        + f'&format=rss&qft=interval%3d%22{interval}%22'
    )


def _is_relevant_to_firm(article: dict, firm: str) -> bool:
    haystack = (article["title"] + " " + article["description"]).lower()
    return any(term in haystack for term in RESTRUCTURING_FIRM_MATCH_TERMS[firm])


def fetch_restructuring_articles(firm: str, limit: int = 6) -> list[dict]:
    """Fetch this firm's recent restructuring headlines, widening the lookback
    window from a week to a month if the narrower window comes up too thin."""
    for interval in (8, 9):
        feed = [(firm, _restructuring_feed_url(firm, interval))]
        articles = [a for a in fetch_from_feeds(feed, limit_per_feed=limit * 2) if _is_relevant_to_firm(a, firm)]
        if articles:
            return articles[:limit]
    return []


_FEED_REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBriefingBot/1.0)"}


def _strip_html(text: str) -> str:
    """Strip tags/scripts from an HTML fragment and unescape entities."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def fetch_from_feeds(feeds: list[tuple[str, str]], limit_per_feed: int = 6) -> list[dict]:
    articles = []
    for source_name, url in feeds:
        try:
            feed = feedparser.parse(url, request_headers=_FEED_REQUEST_HEADERS)
            for entry in feed.entries[:limit_per_feed]:
                title = _strip_html(entry.get("title", ""))
                if not title:
                    continue
                summary = _strip_html(entry.get("summary") or entry.get("description") or "")
                articles.append({"source": source_name, "title": title, "description": summary})
        except Exception as e:
            print(f"  Warning: could not fetch {source_name} ({url}): {e}")
    return articles


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

        since_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
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
    lines = []
    for i, article in enumerate(articles, 1):
        lines.append(f"{i}. [{article['source']}] {article['title']}")
        if article.get("description"):
            # Trim very long summaries
            desc = article["description"][:300]
            lines.append(f"   {desc}")
    return "\n".join(lines)


def format_restructuring_for_prompt(articles_by_firm: dict[str, list[dict]]) -> str:
    blocks = []
    for firm, articles in articles_by_firm.items():
        blocks.append(f"=== {firm} ===")
        blocks.append(format_articles_for_prompt(articles) if articles else "(no recent headlines found)")
    return "\n".join(blocks)


def generate_briefing(
    client: Anthropic,
    wsj_articles: list[dict],
    wsj_newsletter_text: str,
    finance_articles: list[dict],
    healthcare_articles: list[dict],
    restructuring_by_firm: dict[str, list[dict]],
) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    wsj_text = format_articles_for_prompt(wsj_articles)
    finance_text = format_articles_for_prompt(finance_articles)
    healthcare_text = format_articles_for_prompt(healthcare_articles)
    restructuring_text = format_restructuring_for_prompt(restructuring_by_firm)

    newsletter_section = (
        wsj_newsletter_text
        if wsj_newsletter_text
        else "(No 10-Point email found today — skip this section with a single sentence noting it wasn't available.)"
    )

    prompt = f"""Today is {today}. You are writing a sharp, professional morning briefing email for a financially-aware reader.

WSJ MARKET HEADLINES (from RSS):
{wsj_text}

WSJ 10-POINT NEWSLETTER CONTENT (forwarded email):
{newsletter_section}

GENERAL FINANCE HEADLINES:
{finance_text}

HEALTHCARE SECTOR HEADLINES:
{healthcare_text}

RESTRUCTURING CONSULTING HEADLINES, grouped by firm:
{restructuring_text}

Write a morning briefing with exactly five sections, in this order. For sections 1, 3, and 4, pick the 3–5 most significant stories and write 2–3 sentences per story: what happened, why it matters, and what to watch. If a section's headlines are thin or off-topic, cover fewer stories rather than padding — do not invent stories.

Throughout, prioritize concrete, specific facts over vague characterizations: name the companies, counterparties, dollar figures, deal structures, and dates whenever the source material provides them. The reader wants enough specificity to form their own opinion on a deal, not just a summary that something happened — if a number or name is available, use it; if it isn't, say what's unknown rather than glossing over it.

Section 1 — WSJ MARKETS: Major market moves reported by the Wall Street Journal — indices, rates, commodities, Fed/central bank signals.
Section 2 — WSJ 10-POINT: Summarize the newsletter content above in the same story-by-story style (skip stories already covered in Section 1).
Section 3 — FINANCE: Broader finance and business news of the day — earnings, M&A, corporate strategy, macro trends not already covered above.
Section 4 — HEALTHCARE: What's happening in the healthcare sector — biotech, pharma, hospital systems, payers, regulation.
Section 5 — RESTRUCTURING CONSULTING: For EACH of the four firms — EY-Parthenon, Alvarez & Marsal, FTI Consulting, AlixPartners — write its own <h3> subsection naming the firm, followed by 2–3 short paragraphs covering: (a) the specific active deals or engagements in the headlines — who the client/counterparty is, what kind of mandate (restructuring, bankruptcy, M&A advisory, acquisition, senior hire), and any deal size or terms mentioned; (b) what's new or has changed versus what a reader would already know; and (c) why it's a meaningful data point about that firm's positioning (e.g. sector focus, deal flow momentum, competitive standing). If a firm has no usable headlines, write one sentence noting there's no notable update today rather than inventing a deal.

Format strictly as HTML (no <html>/<head>/<body> tags). Use this structure:
- <h2> for section headers
- <h3 style="margin-bottom:4px"> for each story headline or firm name (keep it punchy, under 10 words)
- <p style="margin-top:4px"> for the analysis paragraph
- Wrap any key figures/numbers in <strong>

Open with a single <p><em>one-sentence overview of the overall tone of today's news</em></p> before the sections."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4500,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def build_email_html(briefing_content: str, recipient_name: str = "") -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
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

    print("Fetching WSJ market news...")
    wsj_articles = fetch_from_feeds(WSJ_FEEDS)

    print("Fetching WSJ 10-Point newsletter from email...")
    wsj_newsletter_text = fetch_wsj_newsletter(gmail_address, gmail_app_password)
    print("  Found WSJ 10-Point email." if wsj_newsletter_text else "  No WSJ 10-Point email found for today.")

    print("Fetching general finance news...")
    finance_articles = fetch_from_feeds(FINANCE_FEEDS)

    print("Fetching healthcare sector news...")
    healthcare_articles = fetch_from_feeds(HEALTHCARE_FEEDS)

    print("Fetching restructuring consulting news, per firm...")
    restructuring_by_firm = {firm: fetch_restructuring_articles(firm) for firm in RESTRUCTURING_FIRMS}
    for firm, articles in restructuring_by_firm.items():
        print(f"    {firm}: {len(articles)} headlines")

    print(
        f"  WSJ: {len(wsj_articles)}, Finance: {len(finance_articles)}, "
        f"Healthcare: {len(healthcare_articles)}, "
        f"Restructuring: {sum(len(a) for a in restructuring_by_firm.values())}"
    )

    if not any(
        [wsj_articles, finance_articles, healthcare_articles, wsj_newsletter_text]
        + list(restructuring_by_firm.values())
    ):
        raise RuntimeError("No articles fetched — all RSS feeds failed. Check network access.")

    print("Generating briefing with Claude...")
    client = Anthropic(api_key=anthropic_key)
    briefing_content = generate_briefing(
        client, wsj_articles, wsj_newsletter_text, finance_articles, healthcare_articles, restructuring_by_firm
    )

    today_short = datetime.now().strftime("%A, %B %d")
    subject = f"Morning Briefing — {today_short}"
    html_email = build_email_html(briefing_content, recipient_name)

    print(f"Sending email to {recipient_email}...")
    send_email(html_email, subject, gmail_address, gmail_app_password, recipient_email)

    print("Done — morning briefing sent.")


if __name__ == "__main__":
    main()

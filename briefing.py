#!/usr/bin/env python3

import os
import re
import smtplib
import feedparser
from datetime import datetime
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

# The restructuring-consulting firms (EY-Parthenon, Alvarez & Marsal, FTI
# Consulting, AlixPartners) don't publish their own deal RSS feeds, so this
# uses a Google News search feed scoped to those firm names instead.
RESTRUCTURING_QUERY = (
    '"Alvarez & Marsal" OR "AlixPartners" OR "FTI Consulting" OR "EY-Parthenon" restructuring'
)
RESTRUCTURING_FEEDS = [
    (
        "Restructuring Deals",
        "https://news.google.com/rss/search?q="
        + quote(RESTRUCTURING_QUERY)
        + "&hl=en-US&gl=US&ceid=US:en",
    ),
]


def fetch_from_feeds(feeds: list[tuple[str, str]], limit_per_feed: int = 6) -> list[dict]:
    articles = []
    for source_name, url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit_per_feed]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                summary = (entry.get("summary") or entry.get("description") or "").strip()
                # feedparser sometimes wraps summary in HTML tags — strip them crudely
                summary = summary.replace("<p>", "").replace("</p>", "").strip()
                articles.append({"source": source_name, "title": title, "description": summary})
        except Exception as e:
            print(f"  Warning: could not fetch {source_name} ({url}): {e}")
    return articles


def format_articles_for_prompt(articles: list[dict]) -> str:
    lines = []
    for i, article in enumerate(articles, 1):
        lines.append(f"{i}. [{article['source']}] {article['title']}")
        if article.get("description"):
            # Trim very long summaries
            desc = article["description"][:300]
            lines.append(f"   {desc}")
    return "\n".join(lines)


def generate_briefing(
    client: Anthropic,
    wsj_articles: list[dict],
    finance_articles: list[dict],
    healthcare_articles: list[dict],
    restructuring_articles: list[dict],
) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    wsj_text = format_articles_for_prompt(wsj_articles)
    finance_text = format_articles_for_prompt(finance_articles)
    healthcare_text = format_articles_for_prompt(healthcare_articles)
    restructuring_text = format_articles_for_prompt(restructuring_articles)

    prompt = f"""Today is {today}. You are writing a sharp, professional morning briefing email for a financially-aware reader.

WSJ MARKET HEADLINES:
{wsj_text}

GENERAL FINANCE HEADLINES:
{finance_text}

HEALTHCARE SECTOR HEADLINES:
{healthcare_text}

RESTRUCTURING CONSULTING HEADLINES (mentions of EY-Parthenon, Alvarez & Marsal, FTI Consulting, AlixPartners):
{restructuring_text}

Write a morning briefing with exactly four sections. For each section pick the 3–5 most significant stories and write 2–3 sentences per story: what happened, why it matters, and what to watch. If a section's headlines are thin or off-topic, cover fewer stories rather than padding — do not invent stories.

Section 1 — WSJ MARKETS: Major market moves reported by the Wall Street Journal — indices, rates, commodities, Fed/central bank signals.
Section 2 — FINANCE: Broader finance and business news of the day — earnings, M&A, corporate strategy, macro trends not already covered in Section 1.
Section 3 — HEALTHCARE: What's happening in the healthcare sector — biotech, pharma, hospital systems, payers, regulation.
Section 4 — RESTRUCTURING CONSULTING: Key deals, engagements, and moves at restructuring/turnaround advisory firms, especially EY-Parthenon, Alvarez & Marsal, FTI Consulting, and AlixPartners. If none of these firms appear by name, summarize the most relevant restructuring/turnaround deal news instead.

Format strictly as HTML (no <html>/<head>/<body> tags). Use this structure:
- <h2> for section headers
- <h3 style="margin-bottom:4px"> for each story headline (keep it punchy, under 10 words)
- <p style="margin-top:4px"> for the analysis paragraph
- Wrap any key figures/numbers in <strong>

Open with a single <p><em>one-sentence overview of the overall tone of today's news</em></p> before the sections."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3500,
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

    print("Fetching general finance news...")
    finance_articles = fetch_from_feeds(FINANCE_FEEDS)

    print("Fetching healthcare sector news...")
    healthcare_articles = fetch_from_feeds(HEALTHCARE_FEEDS)

    print("Fetching restructuring consulting news...")
    restructuring_articles = fetch_from_feeds(RESTRUCTURING_FEEDS)

    print(
        f"  WSJ: {len(wsj_articles)}, Finance: {len(finance_articles)}, "
        f"Healthcare: {len(healthcare_articles)}, Restructuring: {len(restructuring_articles)}"
    )

    if not any([wsj_articles, finance_articles, healthcare_articles, restructuring_articles]):
        raise RuntimeError("No articles fetched — all RSS feeds failed. Check network access.")

    print("Generating briefing with Claude...")
    client = Anthropic(api_key=anthropic_key)
    briefing_content = generate_briefing(
        client, wsj_articles, finance_articles, healthcare_articles, restructuring_articles
    )

    today_short = datetime.now().strftime("%A, %B %d")
    subject = f"Morning Briefing — {today_short}"
    html_email = build_email_html(briefing_content, recipient_name)

    print(f"Sending email to {recipient_email}...")
    send_email(html_email, subject, gmail_address, gmail_app_password, recipient_email)

    print("Done — morning briefing sent.")


if __name__ == "__main__":
    main()

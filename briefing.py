#!/usr/bin/env python3

import os
import smtplib
import requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()


def fetch_news(api_key: str, category: str, query: str | None = None, page_size: int = 12) -> list[dict]:
    params = {
        "language": "en",
        "pageSize": page_size,
        "apiKey": api_key,
    }
    if query:
        url = "https://newsapi.org/v2/everything"
        params["q"] = query
        params["sortBy"] = "publishedAt"
    else:
        url = "https://newsapi.org/v2/top-headlines"
        params["category"] = category
        params["country"] = "us"

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    articles = response.json().get("articles", [])
    # Filter out removed articles
    return [a for a in articles if a.get("title") and a["title"] != "[Removed]"]


def format_articles_for_prompt(articles: list[dict]) -> str:
    lines = []
    for i, article in enumerate(articles, 1):
        title = article.get("title", "").strip()
        desc = (article.get("description") or "").strip()
        source = article.get("source", {}).get("name", "Unknown")
        lines.append(f"{i}. [{source}] {title}")
        if desc:
            lines.append(f"   {desc}")
    return "\n".join(lines)


def generate_briefing(client: Anthropic, market_articles: list[dict], political_articles: list[dict]) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    market_text = format_articles_for_prompt(market_articles)
    political_text = format_articles_for_prompt(political_articles)

    prompt = f"""Today is {today}. You are writing a sharp, professional morning briefing email for a financially-aware reader.

MARKET & BUSINESS HEADLINES:
{market_text}

POLITICAL & WORLD HEADLINES:
{political_text}

Write a morning briefing with exactly two sections. For each section pick the 3–5 most significant stories and write 2–3 sentences per story: what happened, why it matters, and what to watch.

Section 1 — MARKETS: Focus on macro moves, earnings, commodity prices (oil, gold), Fed/central bank signals, major IPOs or M&A, geopolitical impacts on markets.
Section 2 — POLITICS: Cover the most consequential domestic or international political developments and their real-world implications.

Format strictly as HTML (no <html>/<head>/<body> tags). Use this structure:
- <h2> for section headers
- <h3 style="margin-bottom:4px"> for each story headline (keep it punchy, under 10 words)
- <p style="margin-top:4px"> for the analysis paragraph
- Wrap any key figures/numbers in <strong>

Open with a single <p><em>one-sentence overview of the overall tone of today's news</em></p> before the sections."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
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
          <p style="margin:0;font-size:14px;font-style:italic;color:#a8b2bf;">{greeting} Here's what's moving the world today.</p>
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
            Automated Morning Briefing &middot; Powered by Claude AI &amp; NewsAPI &middot; {today}
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
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    newsapi_key = os.environ["NEWSAPI_KEY"]
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient_email = os.environ.get("RECIPIENT_EMAIL", gmail_address)
    recipient_name = os.environ.get("RECIPIENT_NAME", "")

    print("Fetching market & business news...")
    market_articles = fetch_news(newsapi_key, category="business")

    print("Fetching political & world news...")
    # Mix top-headlines general + targeted query for broader political coverage
    political_articles = fetch_news(newsapi_key, category="general")

    print(f"  Market articles: {len(market_articles)}, Political articles: {len(political_articles)}")

    if not market_articles and not political_articles:
        raise RuntimeError("No articles fetched — check your NEWSAPI_KEY and quota.")

    print("Generating briefing with Claude...")
    client = Anthropic(api_key=anthropic_key)
    briefing_content = generate_briefing(client, market_articles, political_articles)

    today_short = datetime.now().strftime("%A, %B %d")
    subject = f"Morning Briefing — {today_short}"
    html_email = build_email_html(briefing_content, recipient_name)

    print(f"Sending email to {recipient_email}...")
    send_email(html_email, subject, gmail_address, gmail_app_password, recipient_email)

    print("Done — morning briefing sent.")


if __name__ == "__main__":
    main()

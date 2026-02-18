import os
import json
import hashlib
import smtplib
import imaplib
import email
import re
from email.mime.text import MIMEText
from datetime import datetime
 
import feedparser
import yaml
from bs4 import BeautifulSoup
 
# -----------------------------
# Basic helpers
# -----------------------------
def normalize(text):
    return (text or "").lower()
 
def contains_any(text, keywords):
    text = normalize(text)
    return any((k or "").lower() in text for k in keywords)
 
def load_sent_jobs(path="sent_jobs.json"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()
 
def save_sent_jobs(sent, path="sent_jobs.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(sent), f, indent=2, ensure_ascii=False)
 
def make_id(*parts):
    raw = "|".join([p or "" for p in parts])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
 
def extract_urls(text):
    # simple URL extraction
    return list(dict.fromkeys(re.findall(r"https?://[^\s<>\"]+", text or "")))
 
# -----------------------------
# RSS (optional / old behavior)
# -----------------------------
def fetch_rss(source):
    feed = feedparser.parse(source["url"])
    jobs = []
    for entry in feed.entries[:30]:
        summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ")
        jobs.append({
            "title": entry.get("title", ""),
            "url": entry.get("link", ""),
            "source": source.get("name", "RSS"),
            "text": summary
        })
    return jobs
 
# -----------------------------
# Gmail IMAP fetch (new)
# -----------------------------
def fetch_gmail_alert_emails(max_emails=50):
    """
    Reads the last N emails from Gmail INBOX using IMAP.
    Requires env vars:
      IMAP_USER, IMAP_PASS
    """
    imap_user = os.environ.get("IMAP_USER")
    imap_pass = os.environ.get("IMAP_PASS")
    if not imap_user or not imap_pass:
        return []
 
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login(imap_user, imap_pass)
    mail.select("INBOX")
 
    status, data = mail.search(None, "ALL")
    if status != "OK":
        mail.logout()
        return []
 
    ids = data[0].split()
    ids = ids[-max_emails:]  # last N
 
    results = []
    for eid in reversed(ids):
        status, msg_data = mail.fetch(eid, "(RFC822)")
        if status != "OK":
            continue
 
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
 
        subject = msg.get("Subject", "")
        from_ = msg.get("From", "")
        date_ = msg.get("Date", "")
 
        body_texts = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition", "")).lower()
                if ctype in ("text/plain", "text/html") and "attachment" not in disp:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_texts.append(payload.decode(charset, errors="ignore"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body_texts.append(payload.decode(charset, errors="ignore"))
 
        body = "\n\n".join(body_texts)
        results.append({
            "source": from_,
            "title": subject,
            "date": date_,
            "text": body,
            "urls": extract_urls(body)
        })
 
    mail.logout()
    return results
 
# -----------------------------
# SMTP send (unchanged)
# -----------------------------
def send_email(html, subject, to_email):
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
 
    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg["Subject"] = subject
 
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
 
def main():
    # Read config
    config = yaml.safe_load(open("jobsources.yaml", "r", encoding="utf-8"))
 
    keywords = config["filters"].get("keywords_any", [])
    excludes = config["filters"].get("exclude_any", [])
    locations = config["filters"].get("locations_any", [])
 
    # NEW: company markers come from jobsources.yaml (companies:)
    company_markers = []
    companies = config.get("companies", [])
    for c in companies:
        company_markers.extend(c.get("markers_any", []))
 
    # Fallback markers if companies not configured
    if not company_markers:
        company_markers = ["bmw", "bosch", "allianz", "siemens", "sap", "telekom", "henkel", "basf"]
 
    sent = load_sent_jobs()
    matches = []
 
    # 1) Try Gmail IMAP alert emails first (preferred)
    email_items = fetch_gmail_alert_emails(max_emails=60)
 
    for item in email_items:
        full_text = f"{item['source']} {item['title']} {item['text']} " + " ".join(item.get("urls", []))
 
        # must mention one of our companies
        if company_markers and not contains_any(full_text, company_markers):
            continue
 
        # exclude terms
        if excludes and contains_any(full_text, excludes):
            continue
 
        # include keywords
        if keywords and not contains_any(full_text, keywords):
            continue
 
        # location filter (optional)
        if locations and not contains_any(full_text, locations):
            continue
 
        jid = make_id(item["source"], item["title"], (item["urls"][0] if item["urls"] else ""))
        if jid in sent:
            continue
 
        sent.add(jid)
        matches.append({
            "source": item["source"],
            "title": item["title"],
            "url": item["urls"][0] if item["urls"] else "",
            "extra_urls": item["urls"][1:5] if item["urls"] else []
        })
 
    # 2) Optional fallback: RSS sources (if you still have them in jobsources.yaml)
    # This keeps your old behavior available, but it's not required.
    if not matches:
        for source in config.get("sources", []):
            if source.get("type") == "rss":
                for job in fetch_rss(source):
                    full_text = f"{job['title']} {job['text']} {job['url']}"
 
                    if excludes and contains_any(full_text, excludes):
                        continue
                    if keywords and not contains_any(full_text, keywords):
                        continue
                    if locations and not contains_any(full_text, locations):
                        continue
 
                    jid = make_id(job["title"], job["source"], job["url"])
                    if jid in sent:
                        continue
 
                    sent.add(jid)
                    matches.append({
                        "source": job["source"],
                        "title": job["title"],
                        "url": job["url"],
                        "extra_urls": []
                    })
 
    if not matches:
        print("No new matching jobs found (emails or RSS).")
        return
 
    # Build HTML table
    rows = ""
    for m in matches:
        main_link = f'<a href="{m["url"]}">Open</a>' if m["url"] else "No link"
        more_links = ""
        if m["extra_urls"]:
            more_links = "<br>" + "<br>".join([f'<a href="{u}">More</a>' for u in m["extra_urls"]])
 
        rows += f"<tr><td>{m['source']}</td><td>{m['title']}</td><td>{main_link}{more_links}</td></tr>"
 
    html = f"""
    <h2>Job Digest ({datetime.now().date()})</h2>
    <p>Matches found: <b>{len(matches)}</b></p>
    <table border="1" cellpadding="6">
      <tr><th>Source</th><th>Title</th><th>Link</th></tr>
      {rows}
    </table>
    """
 
    send_email(html, config["email"]["subject"], config["email"]["to"])
    save_sent_jobs(sent)
    print(f"Sent digest with {len(matches)} items.")
 
if __name__ == "__main__":
    main()

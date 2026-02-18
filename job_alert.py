import os
import re
import json
import hashlib
import smtplib
import imaplib
import email
from email.mime.text import MIMEText
from datetime import datetime, timezone
 
import yaml
 
# ---------------------------
# Helpers
# ---------------------------
def norm(s: str) -> str:
    return (s or "").strip().lower()
 
def contains_any(text: str, terms: list[str]) -> bool:
    t = norm(text)
    return any(norm(x) in t for x in (terms or []))
 
def sha(*parts: str) -> str:
    raw = "|".join([p or "" for p in parts])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
 
def load_sent(path="sent_jobs.json") -> set[str]:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return set(json.load(f))
            except Exception:
                return set()
    return set()
 
def save_sent(sent: set[str], path="sent_jobs.json") -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent)), f, ensure_ascii=False, indent=2)
 
def extract_urls(text: str) -> list[str]:
    # keep order, remove duplicates
    urls = re.findall(r"https?://[^\s<>\"]+", text or "")
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
 
def get_message_body(msg: email.message.Message) -> str:
    """
    Prefer text/plain; fall back to text/html.
    """
    plain_parts = []
    html_parts = []
 
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
 
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(charset, errors="ignore")
                except Exception:
                    decoded = payload.decode("utf-8", errors="ignore")
 
                if ctype == "text/plain":
                    plain_parts.append(decoded)
                else:
                    html_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="ignore")
            except Exception:
                decoded = payload.decode("utf-8", errors="ignore")
            plain_parts.append(decoded)
 
    if plain_parts:
        return "\n\n".join(plain_parts)
    return "\n\n".join(html_parts)
 
# ---------------------------
# IMAP (Gmail)
# ---------------------------
def fetch_gmail_messages(max_emails: int = 60) -> list[dict]:
    """
    Returns list of dicts: {from, subject, date, body, urls}
    Requires env:
      IMAP_USER, IMAP_PASS
    """
    user = os.environ.get("IMAP_USER", "")
    pw = os.environ.get("IMAP_PASS", "")
    if not user or not pw:
        print("❌ IMAP_USER / IMAP_PASS not set. Skipping IMAP fetch.")
        return []
 
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(user, pw)
        imap.select("INBOX")
 
        status, data = imap.search(None, "ALL")
        if status != "OK":
            print("❌ IMAP search failed:", status, data)
            imap.logout()
            return []
 
        ids = data[0].split()
        ids = ids[-max_emails:]
 
        results = []
        for eid in reversed(ids):
            status, msg_data = imap.fetch(eid, "(RFC822)")
            if status != "OK":
                continue
 
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
 
            subject = msg.get("Subject", "") or ""
            from_ = msg.get("From", "") or ""
            date_ = msg.get("Date", "") or ""
            body = get_message_body(msg)
            urls = extract_urls(body)
 
            results.append({
                "from": from_,
                "subject": subject,
                "date": date_,
                "body": body,
                "urls": urls,
            })
 
        imap.logout()
        return results
 
    except imaplib.IMAP4.error as e:
        print("❌ IMAP auth/connection error:", str(e))
        return []
    except Exception as e:
        print("❌ Unexpected IMAP error:", repr(e))
        return []
 
# ---------------------------
# SMTP send
# ---------------------------
def send_email(html: str, subject: str, to_email: str) -> None:
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
 
# ---------------------------
# Main
# ---------------------------
def main():
    print("✅ START job_alert.py", datetime.now(timezone.utc).isoformat())
 
    # Load config
    with open("jobsources.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
 
    email_cfg = config.get("email", {})
    filters = config.get("filters", {})
    companies = config.get("companies", [])
 
    keywords = filters.get("keywords_any", []) or []
    excludes = filters.get("exclude_any", []) or []
    locations = filters.get("locations_any", []) or []
    max_emails = int(filters.get("max_emails", 60))
 
    # Flatten company markers
    company_markers = []
    for c in companies:
        company_markers.extend(c.get("markers_any", []) or [])
 
    sent = load_sent("sent_jobs.json")
    matches = []
 
    # Fetch emails
    messages = fetch_gmail_messages(max_emails=max_emails)
 
    for m in messages:
        haystack = f"{m['from']} {m['subject']} {m['body']} " + " ".join(m.get("urls", []))
 
        # company filter (if provided)
        if company_markers and not contains_any(haystack, company_markers):
            continue
 
        # excludes
        if excludes and contains_any(haystack, excludes):
            continue
 
        # keywords
        if keywords and not contains_any(haystack, keywords):
            continue
 
        # optional location filter
        if locations and not contains_any(haystack, locations):
            continue
 
        # determine which company this likely belongs to (best-effort)
        company_name = "Unknown"
        careers_url = ""
        for c in companies:
            if contains_any(haystack, c.get("markers_any", [])):
                company_name = c.get("name", "Unknown")
                careers_url = c.get("careers_url", "")
                break
 
        # pick a main link
        main_url = m["urls"][0] if m["urls"] else careers_url
 
        jid = sha(m["from"], m["subject"], main_url or "")
        if jid in sent:
            continue
 
        sent.add(jid)
        matches.append({
            "company": company_name,
            "from": m["from"],
            "subject": m["subject"],
            "main_url": main_url or "",
            "careers_url": careers_url or "",
        })
 
    if not matches:
        print("ℹ️ No matching alert emails found. Nothing to send.")
        save_sent(sent, "sent_jobs.json")
        print("✅ END job_alert.py", datetime.now(timezone.utc).isoformat())
        return
 
    # Build HTML
    rows = ""
    for item in matches:
        link_html = "—"
        if item["main_url"]:
            link_html = f'<a href="{item["main_url"]}">Open</a>'
 
        careers_html = "—"
        if item["careers_url"]:
            careers_html = f'<a href="{item["careers_url"]}">Careers</a>'
 
        rows += (
            "<tr>"
            f"<td>{item['company']}</td>"
            f"<td>{item['subject']}</td>"
            f"<td>{link_html}</td>"
            f"<td>{careers_html}</td>"
            "</tr>"
        )
 
    html = f"""
    <h2>Job Alerts Digest ({datetime.now().date()})</h2>
    <p>Matches found: <b>{len(matches)}</b></p>
    <table border="1" cellpadding="6" cellspacing="0">
      <tr>
        <th>Company</th>
        <th>Subject</th>
        <th>Link</th>
        <th>Careers</th>
      </tr>
      {rows}
    </table>
    """
 
    to_email = email_cfg["to"]
    subject = email_cfg.get("subject", "Daily Job Alert Digest")
 
    send_email(html, subject, to_email)
    save_sent(sent, "sent_jobs.json")
 
    print(f"✅ Sent digest with {len(matches)} items.")
    print("✅ END job_alert.py", datetime.now(timezone.utc).isoformat())
 
if __name__ == "__main__":
    main()

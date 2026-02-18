import os
import json
import hashlib
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
import feedparser
import yaml
from bs4 import BeautifulSoup
 
def normalize(text):
    return (text or "").lower()
 
def load_sent_jobs():
    if os.path.exists("sent_jobs.json"):
        with open("sent_jobs.json", "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()
 
def save_sent_jobs(sent):
    with open("sent_jobs.json", "w", encoding="utf-8") as f:
        json.dump(list(sent), f, indent=2, ensure_ascii=False)
 
def job_id(title, source, url):
    raw = f"{title}|{source}|{url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
 
def contains_any(text, keywords):
    text = normalize(text)
    return any(k.lower() in text for k in keywords)
 
def fetch_rss(source):
    feed = feedparser.parse(source["url"])
    jobs = []
    for entry in feed.entries[:30]:
        summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text()
        jobs.append({
            "title": entry.get("title", ""),
            "url": entry.get("link", ""),
            "source": source["name"],
            "text": summary
        })
    return jobs
 
def send_email(html, subject, to_email):
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
 
    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg["Subject"] = subject
 
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
 
def main():
    config = yaml.safe_load(open("jobsources.yaml", "r", encoding="utf-8"))
 
    keywords = config["filters"]["keywords_any"]
    excludes = config["filters"]["exclude_any"]
    locations = config["filters"]["locations_any"]
 
    sent = load_sent_jobs()
    matches = []
 
    for source in config["sources"]:
        if source["type"] == "rss":
            jobs = fetch_rss(source)
        else:
            continue
 
        for job in jobs:
            full_text = f"{job['title']} {job['text']}"
            if contains_any(full_text, excludes):
                continue
            if not contains_any(full_text, keywords):
                continue
            if locations and not contains_any(full_text, locations):
                continue
 
            jid = job_id(job["title"], job["source"], job["url"])
            if jid in sent:
                continue
 
            sent.add(jid)
            matches.append(job)
 
    if not matches:
        print("No new jobs found.")
        return
 
    rows = ""
    for job in matches:
        rows += f"<tr><td>{job['source']}</td><td>{job['title']}</td><td>{job['url']}Open</a></td></tr>"
 
    html = f"""
    <h2>New Job Matches ({datetime.now().date()})</h2>
    <table border="1" cellpadding="6">
      <tr><th>Source</th><th>Title</th><th>Link</th></tr>
      {rows}
    </table>
    """
 
    send_email(html, config["email"]["subject"], config["email"]["to"])
    save_sent_jobs(sent)
 
if __name__ == "__main__":
    main()

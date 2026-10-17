"""Run discovery, signup attempts, and reporting with one command."""
import argparse
import csv
import html
import json
import subprocess
import sys
from pathlib import Path


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def make_report(folder):
    events = read_jsonl(folder / "signups.jsonl")
    attempts = {}
    skipped = {}
    for event in events:
        kind = event.get("event")
        if kind in {"attempt_started", "attempt_result"}:
            attempts[event["key"]] = event
        elif kind in {"form_skipped", "page_skipped"}:
            skipped[(event.get("page"), event.get("form_index"))] = event

    rows = []
    for event in attempts.values():
        rows.append({
            "service": event.get("service", ""), "page": event.get("page", ""),
            "status": event.get("result", "unknown/interrupted"),
            "information_sent": json.dumps(event.get("information", []), ensure_ascii=False),
            "submission_endpoint": event.get("endpoint", ""),
            "http_status": event.get("http_status", ""),
            "response_evidence_or_reason": event.get("evidence", "No final result recorded"),
            "time": event.get("time", ""),
        })
    for event in skipped.values():
        rows.append({
            "service": event.get("service", ""), "page": event.get("page", ""),
            "status": "not_attempted", "information_sent": "None",
            "submission_endpoint": "", "http_status": "",
            "response_evidence_or_reason": event.get("reason", ""),
            "time": event.get("time", ""),
        })
    processed = {(e.get("page"), e.get("form_index")) for e in events
                 if e.get("event") in {"attempt_started", "attempt_result", "form_skipped"}}
    # Include discovered forms not reached because of limits or deduplication.
    for event in read_jsonl(folder / "crawl.jsonl"):
        page = event.get("url", event.get("requested_url", ""))
        for index, form in enumerate(event.get("forms", [])):
            if (page, form.get("index", index)) not in processed:
                rows.append({
                    "service": event.get("title", ""), "page": page,
                    "status": "not_processed", "information_sent": "None",
                    "submission_endpoint": form.get("action", ""), "http_status": "",
                    "response_evidence_or_reason": "Not reached or skipped by per-site/deduplication limits; see log",
                    "time": event.get("fetched_at", ""),
                })
    # Include discovery failures and pages with no forms, too.
    for event in read_jsonl(folder / "crawl.jsonl"):
        if event.get("status") != "ok" or not event.get("forms"):
            rows.append({
                "service": event.get("title", ""),
                "page": event.get("url", event.get("requested_url", "")),
                "status": "discovery_skipped" if event.get("status") != "ok" else "no_forms_found",
                "information_sent": "None", "submission_endpoint": "", "http_status": "",
                "response_evidence_or_reason": event.get("error", "No static HTML forms found"),
                "time": event.get("fetched_at", ""),
            })
    fields = ["service", "page", "status", "information_sent", "submission_endpoint",
              "http_status", "response_evidence_or_reason", "time"]
    # Prevent spreadsheet formula execution from untrusted page titles/text.
    def csv_safe(value):
        text = str(value)
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
    with (folder / "signup_report.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: csv_safe(row[key]) for key in fields} for row in rows)

    total = len(attempts)
    likely = sum(event.get("result") == "likely_success" for event in attempts.values())
    pending = sum(event.get("result") == "confirmation_required" for event in attempts.values())
    estimate = f"{100 * likely / total:.1f}%" if total else "N/A"
    summary = (f"{total} attempted forms; {likely} likely successful; "
               f"{pending} need email confirmation. Estimated success: {estimate}.")
    cells = "".join("<tr>" + "".join(f"<td>{html.escape(str(row[key]))}</td>"
                                   for key in fields) + "</tr>" for row in rows)
    headers = "".join(f"<th>{html.escape(key.replace('_', ' '))}</th>" for key in fields)
    document = '''<!doctype html><meta charset="utf-8"><title>Signup report</title>
    <style>body{font:15px system-ui;margin:30px;color:#182331}table{border-collapse:collapse;width:100%}
    th,td{border:1px solid #ccc;padding:10px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
    th{background:#edf2f7}td{max-width:320px}tr:nth-child(even){background:#fafafa}</style>
    <h1>Signup collection report</h1>'''
    document += f"<p>{html.escape(summary)}</p>"
    document += ("<p>Success is estimated from response text, not verified in your inbox. "
                 "Attempts cover all runs retained in signups.jsonl. Discovery details cover the latest crawl. "
                 "Multiple rows may describe different forms on the same site. "
                 "Information is the attempted POST payload; hidden values are omitted. "
                 "Response evidence is a short extracted phrase, not the full response body.</p>")
    document += f"<table><thead><tr>{headers}</tr></thead><tbody>{cells}</tbody></table>"
    (folder / "signup_report.html").write_text(document, encoding="utf-8")
    print("\n" + summary, flush=True)
    print("Open signup_report.html in your browser, or signup_report.csv in a spreadsheet.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--max-submissions", type=int, default=10)
    args = parser.parse_args()
    folder = Path(__file__).resolve().parent
    for name in ("freebies_crawler.py", "signup_services.py"):
        if not (folder / name).exists():
            parser.error(f"Put {name} in the same folder as this launcher")
    try:
        print("STEP 1: Finding forms...", flush=True)
        subprocess.run([sys.executable, "freebies_crawler.py", "--max-pages", str(args.max_pages),
                        "--depth", str(args.depth)], cwd=folder, check=True)
        print("STEP 2: Submitting eligible forms...", flush=True)
        subprocess.run([sys.executable, "signup_services.py", "--email", args.email,
                        "--name", args.name, "--max-submissions", str(args.max_submissions),
                        "--submit"], cwd=folder, check=True)
    except subprocess.CalledProcessError as error:
        print(f"A step failed (exit {error.returncode}); reporting any available results.", flush=True)
        raise
    finally:
        make_report(folder)


if __name__ == "__main__":
    main()

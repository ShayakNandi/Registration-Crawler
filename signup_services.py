import argparse
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from freebies_crawler import PoliteClient


AGENT = "FreebieResearchBot"

SIGNUP = re.compile(
    r"newsletter|subscribe|sign\s*up|join.{0,25}(club|list)",
    re.I,
)

CAPTCHA = re.compile(
    r"recaptcha|hcaptcha|turnstile|captcha",
    re.I,
)

SUCCESS = re.compile(
    r"successfully subscribed|subscription (?:is )?confirmed|"
    r"thank you for subscribing|thanks for subscribing|"
    r"you(?:'re| are) subscribed",
    re.I,
)

PENDING = re.compile(
    r"check your (?:email|inbox)|"
    r"confirm your (?:email|subscription)|"
    r"confirmation (?:email|link) (?:has been )?sent",
    re.I,
)

FAILURE = re.compile(
    r"invalid email|email (?:address )?is invalid|"
    r"please complete the captcha|required field|"
    r"subscription failed|unable to subscribe",
    re.I,
)


def origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def read_log(path):
    if not Path(path).exists():
        return []

    rows = []

    for line in Path(path).read_text(
        encoding="utf-8"
    ).splitlines():
        if line.strip():
            rows.append(json.loads(line))

    return rows


def record(path, row):
    """Append a timestamped record without overwriting earlier runs."""
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        **row,
    }

    with open(path, "a", encoding="utf-8") as file:
        file.write(
            json.dumps(entry, ensure_ascii=False) + "\n"
        )
        file.flush()


def prepare(form, page_url, soup, email, name):
    """
    Build the form payload.

    Hidden fields are preserved for submission, but their values
    are omitted from the audit log because they may contain tokens.
    """
    description = (
        form.get_text(" ", strip=True)
        + " "
        + str(form.attrs)
    )

    if not SIGNUP.search(description):
        raise ValueError(
            "Not clearly a newsletter/signup form"
        )

    if CAPTCHA.search(str(form)):
        raise ValueError(
            "CAPTCHA requires browser completion"
        )

    if (
        form.find(["select", "textarea"])
        or form.find("fieldset", disabled=True)
    ):
        raise ValueError(
            "Complex controls require manual completion"
        )

    if (
        form.get("id")
        and soup.find(attrs={"form": form["id"]})
    ):
        raise ValueError(
            "Externally associated controls require manual completion"
        )

    if form.get("method", "get").lower() != "post":
        raise ValueError(
            "Only POST signup forms are supported"
        )

    encoding = form.get(
        "enctype",
        "application/x-www-form-urlencoded",
    ).lower()

    if encoding != "application/x-www-form-urlencoded":
        raise ValueError("Unsupported form encoding")

    base = soup.find("base", href=True)
    base_url = (
        urljoin(page_url, base["href"])
        if base else page_url
    )

    action = form.get("action", "").strip()
    endpoint = (
        urljoin(base_url, action)
        if action else page_url
    )

    parts = urlsplit(endpoint)

    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError(
            "Submission endpoint must be HTTPS"
        )

    data = []
    audit = []
    email_found = False

    for field in form.find_all("input"):
        if (
            field.has_attr("disabled")
            or not field.get("name")
        ):
            continue

        key = field["name"]
        kind = field.get("type", "text").lower()
        normalized = re.sub(
            r"[^a-z]", "", key.lower()
        )

        if kind in {"submit", "button", "reset"}:
            continue

        if kind == "hidden":
            value = field.get("value", "")

        elif kind in {
            "password", "file", "checkbox", "radio", "image"
        }:
            raise ValueError(
                f"Control '{key}' requires manual completion"
            )

        elif (
            kind == "email"
            or normalized in {
                "email",
                "emailaddress",
                "subscriberemail",
            }
        ):
            value = email
            email_found = True

        elif normalized in {
            "name", "fullname", "subscribername"
        }:
            value = name

        elif normalized in {"firstname", "fname"}:
            value = name.split()[0] if name else ""

        else:
            # Leave unknown fields empty instead of inventing data.
            value = ""

        if field.has_attr("required") and not value:
            raise ValueError(
                f"Missing required information: {key}"
            )

        data.append((key, value))

        audit.append({
            "field": key,
            "value": (
                "[hidden value omitted]"
                if kind == "hidden" else value
            ),
        })

    if not email_found:
        raise ValueError("No recognizable email field")

    buttons = form.select(
        'input[type="submit"], '
        'button:not([type]), '
        'button[type="submit"]'
    )

    if len(buttons) > 1:
        raise ValueError(
            "Multiple submit buttons require review"
        )

    if buttons:
        button = buttons[0]

        if (
            button.has_attr("formaction")
            or button.has_attr("formmethod")
        ):
            raise ValueError(
                "Submit button overrides form destination/method"
            )

        if (
            button.get("name")
            and not button.has_attr("disabled")
        ):
            value = button.get("value", "")
            data.append((button["name"], value))
            audit.append({
                "field": button["name"],
                "value": value,
            })

    return endpoint, data, audit


def policy(client, endpoint):
    """Support either earlier version of freebies_crawler.py."""
    getter = (
        getattr(client, "get_robots_policy", None)
        or client.policy
    )

    rules, delay = getter(endpoint)

    if not rules.can_fetch(AGENT, endpoint):
        raise ValueError(
            "Submission endpoint blocked/unavailable "
            "under robots policy"
        )

    return delay


def classify(status, response_text, original_text):
    """Estimate the result using HTTP status and new response text."""
    if not 200 <= status < 300:
        return "http_error", f"HTTP {status}"

    patterns = [
        (FAILURE, "possible_failure"),
        (PENDING, "confirmation_required"),
        (SUCCESS, "likely_success"),
    ]

    for pattern, result in patterns:
        for match in pattern.finditer(response_text):
            # Ignore success messages already present before submission.
            if match.group().lower() not in original_text.lower():
                return result, match.group()

    return (
        "unverified",
        "HTTP response alone does not confirm subscription",
    )


def run(args):
    client = PoliteClient(3)
    existing = read_log(args.log)

    # Remember attempts even if the previous run crashed or timed out.
    attempted = {
        row["key"]
        for row in existing
        if row.get("event") == "attempt_started"
    }

    used_sites = {
        row["site_key"]
        for row in existing
        if row.get("event") == "attempt_started"
    }

    outcomes = Counter()
    submissions = 0

    pages = list(dict.fromkeys(
        row["url"]
        for row in read_log(args.input)
        if row.get("status") == "ok"
        and row.get("forms")
    ))

    for page in pages:
        if submissions >= args.max_submissions:
            break

        try:
            # Reload the form to obtain fresh hidden fields and cookies.
            url, soup = client.get_html(page)
        except (requests.RequestException, ValueError) as error:
            record(args.log, {
                "event": "page_skipped",
                "page": page,
                "reason": str(error),
            })
            continue

        title = (
            soup.title.get_text(strip=True)
            if soup.title else urlsplit(url).hostname
        )

        original_text = soup.get_text(" ", strip=True)

        site_key = hashlib.sha256(
            (origin(url) + args.email.lower()).encode()
        ).hexdigest()

        if site_key in used_sites:
            continue

        for index, form in enumerate(soup.find_all("form")):
            base = {
                "page": url,
                "service": title,
                "form_index": index,
            }

            try:
                endpoint, data, audit = prepare(
                    form,
                    url,
                    soup,
                    args.email,
                    args.name,
                )

                delay = policy(client, endpoint)

            except ValueError as error:
                record(args.log, {
                    **base,
                    "event": "form_skipped",
                    "reason": str(error),
                })
                continue

            key = hashlib.sha256(
                (endpoint + args.email.lower()).encode()
            ).hexdigest()

            if key in attempted:
                continue

            base.update(
                key=key,
                site_key=site_key,
                endpoint=endpoint,
                information=audit,
                method="POST",
            )

            if not args.submit:
                print(
                    f"PREVIEW: {title} -> {endpoint}\n"
                    f"  {audit}"
                )

                record(args.log, {
                    **base,
                    "event": "preview",
                })
                continue

            # Log before sending: a lost response can still mean success.
            record(args.log, {
                **base,
                "event": "attempt_started",
            })

            attempted.add(key)
            used_sites.add(site_key)
            submissions += 1

            result = "unknown"
            evidence = ""
            http_status = None

            try:
                time.sleep(delay)

                with client.session.post(
                    endpoint,
                    data=data,
                    timeout=20,
                    allow_redirects=False,
                    headers={
                        "Referer": url,
                        "Origin": origin(url),
                    },
                    stream=True,
                ) as response:
                    http_status = response.status_code
                    body = bytearray()

                    for chunk in response.iter_content(65536):
                        body.extend(chunk)

                        if len(body) > 2_000_000:
                            raise ValueError(
                                "Response too large; "
                                "submission outcome unknown"
                            )

                    after = BeautifulSoup(
                        bytes(body), "html.parser"
                    ).get_text(" ", strip=True)

                    if (
                        http_status in {301, 302, 303}
                        and response.headers.get("Location")
                    ):
                        landing = urljoin(
                            endpoint,
                            response.headers["Location"],
                        )

                        if urlsplit(landing).scheme != "https":
                            raise ValueError(
                                "Non-HTTPS redirect; outcome unknown"
                            )

                        # Follow with GET, without repeating the POST.
                        time.sleep(delay)
                        _, landing_soup = client.get_html(landing)

                        after = landing_soup.get_text(
                            " ", strip=True
                        )

                        result, evidence = classify(
                            200, after, original_text
                        )

                    elif http_status in {307, 308}:
                        result = "unverified"
                        evidence = "POST redirect not replayed"

                    else:
                        result, evidence = classify(
                            http_status, after, original_text
                        )

            except (requests.RequestException, ValueError) as error:
                evidence = str(error)

            record(args.log, {
                **base,
                "event": "attempt_result",
                "result": result,
                "http_status": http_status,
                "evidence": evidence,
            })

            outcomes[result] += 1
            print(f"{title}: {result} ({evidence})")

            # One attempted signup per source origin/email.
            break

    summary = {
        "submissions_this_run": submissions,
        "outcomes": dict(outcomes),
        "estimated_success_percent": (
            round(
                100 * outcomes["likely_success"] / submissions,
                1,
            )
            if submissions else None
        ),
        "note": (
            "Heuristic success estimate; "
            "inbox confirmation is not checked."
        ),
    }

    record(args.log, {
        "event": "summary",
        **summary,
    })

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Submit simple newsletter forms from crawl.jsonl."
    )

    parser.add_argument("--input", default="crawl.jsonl")
    parser.add_argument("--log", default="signups.jsonl")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--max-submissions", type=int, default=10)
    parser.add_argument("--submit", action="store_true")

    args = parser.parse_args()

    if (
        args.max_submissions < 1
        or not re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+",
            args.email,
        )
    ):
        parser.error(
            "Supply a valid email and max-submissions >= 1"
        )

    run(args)
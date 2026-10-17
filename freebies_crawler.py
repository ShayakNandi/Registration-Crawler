import argparse
import json
import re
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


START_URL = "https://www.llbean.com/catalog/request?storeId=1&catalogId=1&langId=-1"
USER_AGENT = "FreebieResearchBot"

SIGNUP_WORDS = re.compile(
    r"newsletter|subscribe|sign.?up|e.?club|join",
    re.IGNORECASE,
)

FILE_EXTENSIONS = re.compile(
    r"\.(pdf|zip|exe|jpg|jpeg|png|gif|svg|mp4|css|js)$",
    re.IGNORECASE,
)


def clean_url(base_url, href):
    """Turn relative links into complete HTTP/HTTPS URLs."""
    url = urldefrag(urljoin(base_url, href))[0]
    parts = urlsplit(url)

    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None

    if parts.username or parts.password:
        return None

    if FILE_EXTENSIONS.search(parts.path):
        return None

    return url


def get_origin(url):
    """Example: https://example.com/news -> https://example.com"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class PoliteClient:
    def __init__(self, delay=3):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"{USER_AGENT}/1.0"

        self.delay = delay
        self.robots_cache = {}
        self.last_request = {}

    def request(self, url, delay):
        """Make one GET request, with pacing and a response-size limit."""
        origin = get_origin(url)

        elapsed = time.monotonic() - self.last_request.get(origin, 0)
        if elapsed < delay:
            time.sleep(delay - elapsed)

        try:
            # Handle redirects ourselves so their URLs get robots checks.
            with self.session.get(
                url,
                timeout=20,
                allow_redirects=False,
                stream=True,
            ) as response:
                body = bytearray()

                for chunk in response.iter_content(chunk_size=65536):
                    body.extend(chunk)

                    if len(body) > 2_000_000:
                        raise ValueError("Response exceeds the 2 MB limit.")

                return (
                    response.status_code,
                    dict(response.headers),
                    bytes(body),
                )
        finally:
            self.last_request[origin] = time.monotonic()

    def get_robots_policy(self, url):
        """Load and cache robots.txt separately for each origin."""
        origin = get_origin(url)

        if origin in self.robots_cache:
            return self.robots_cache[origin]

        robots_url = f"{origin}/robots.txt"
        parser = RobotFileParser(robots_url)

        # Conservatively block if robots.txt cannot be retrieved.
        rules = ["User-agent: *", "Disallow: /"]

        try:
            status, headers, body = self.request(
                robots_url,
                self.delay,
            )

            if status in {404, 410}:
                # No robots.txt file exists.
                rules = ["User-agent: *", "Disallow:"]

            elif status == 200:
                text = body.decode("utf-8", errors="replace")

                # Avoid treating an HTML error page as robots.txt.
                if "<html" not in text[:1000].lower():
                    rules = text.splitlines()

            # Other statuses, including redirected robots.txt,
            # remain blocked under this conservative policy.

        except (requests.RequestException, ValueError) as error:
            print(f"Could not retrieve {robots_url}: {error}")

        parser.parse(rules)

        delay = max(
            self.delay,
            parser.crawl_delay(USER_AGENT) or 0,
        )

        rate = parser.request_rate(USER_AGENT)

        if rate and rate.requests > 0:
            # Conservative: wait an entire rate window between requests.
            delay = max(delay, rate.seconds)

        self.robots_cache[origin] = (parser, delay)
        return parser, delay

    def get_html(self, url):
        """Fetch HTML, checking robots rules before each redirect hop."""
        for _ in range(6):
            parser, delay = self.get_robots_policy(url)

            if not parser.can_fetch(USER_AGENT, url):
                raise ValueError(
                    "Blocked by robots policy or unavailable robots.txt."
                )

            status, headers, body = self.request(url, delay)

            if status in {301, 302, 303, 307, 308}:
                target = clean_url(
                    url,
                    headers.get("Location", ""),
                )

                if not target or target == url:
                    raise ValueError("Invalid redirect.")

                url = target
                continue

            if status != 200:
                raise ValueError(f"HTTP status {status}")

            content_type = headers.get("Content-Type", "").lower()

            if "html" not in content_type:
                raise ValueError("Response is not HTML.")

            soup = BeautifulSoup(body, "html.parser")
            return url, soup

        raise ValueError("Too many redirects.")


def get_base_url(soup, page_url):
    """Respect an HTML <base href='...'> element when present."""
    base = soup.find("base", href=True)
    return urljoin(page_url, base["href"]) if base else page_url


def extract_forms(soup, page_url):
    """Record form metadata without submitting anything."""
    forms = []
    base_url = get_base_url(soup, page_url)

    for index, form in enumerate(soup.find_all("form")):
        fields = []

        for element in form.find_all(
            ["input", "select", "textarea", "button"]
        ):
            default_type = (
                "text" if element.name == "input" else element.name
            )

            fields.append({
                "name": element.get("name"),
                "type": element.get("type", default_type).lower(),
                "placeholder": element.get("placeholder", ""),
                "required": element.has_attr("required"),
                "disabled": element.has_attr("disabled"),
            })

        has_email_field = any(
            field["type"] == "email"
            or re.search(
                r"e.?mail",
                (field["name"] or "") + " " + field["placeholder"],
                re.IGNORECASE,
            )
            for field in fields
        )

        text = form.get_text(" ", strip=True)
        action = form.get("action", "").strip()

        signup_candidate = bool(
            has_email_field
            and SIGNUP_WORDS.search(text + " " + str(form.attrs))
        )

        forms.append({
            "index": index,
            "action": (
                clean_url(base_url, action) if action else page_url
            ),
            "method": form.get("method", "get").lower(),
            "fields": fields,
            "text": text[:600],
            "has_email_field": bool(has_email_field),
            "signup_candidate": signup_candidate,
        })

    return forms


def find_links(soup, page_url):
    """Find directory offers and likely newsletter/signup links."""
    base_url = get_base_url(soup, page_url)
    parts = urlsplit(page_url)

    is_directory = (
        parts.hostname in {"thefreesite.com", "www.thefreesite.com"}
        and parts.path.startswith("/Newest_Freebies")
    )

    for anchor in soup.find_all("a", href=True):
        url = clean_url(base_url, anchor["href"])

        if not url:
            continue

        label = anchor.get_text(" ", strip=True)

        # Directory offer titles commonly appear inside headings.
        is_offer = (
            is_directory
            and anchor.find_parent(["h2", "h3"]) is not None
        )

        is_signup = bool(
            SIGNUP_WORDS.search(label + " " + anchor["href"])
        )

        target = urlsplit(url)

        is_directory_page = (
            is_directory
            and target.hostname == parts.hostname
            and target.path.startswith("/Newest_Freebies/")
        )

        if is_offer or is_signup or is_directory_page:
            yield url


def crawl(max_pages, max_depth, delay, output):
    client = PoliteClient(delay)

    # Queue entries: URL, distance from starting page, referring page.
    queue = deque([(START_URL, 0, None)])
    queued = {START_URL}
    visited = set()

    attempted = 0

    with open(output, "w", encoding="utf-8") as file:
        while queue and attempted < max_pages:
            requested_url, depth, parent = queue.popleft()

            if requested_url in visited:
                continue

            visited.add(requested_url)
            attempted += 1

            print(f"[{attempted}/{max_pages}] {requested_url}")

            record = {
                "requested_url": requested_url,
                "depth": depth,
                "found_on": parent,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }

            try:
                final_url, soup = client.get_html(requested_url)
                visited.add(final_url)

                forms = extract_forms(soup, final_url)

                record.update({
                    "url": final_url,
                    "status": "ok",
                    "title": (
                        soup.title.get_text(strip=True)
                        if soup.title else ""
                    ),
                    "forms": forms,
                })

                email_forms = sum(
                    form["has_email_field"] for form in forms
                )

                print(
                    f"  Found {len(forms)} forms; "
                    f"{email_forms} contain email fields."
                )

                if depth < max_depth:
                    for link in find_links(soup, final_url):
                        # Bound the queue as well as the page count.
                        if (
                            link not in queued
                            and len(queued) < max_pages * 20
                        ):
                            queued.add(link)
                            queue.append(
                                (link, depth + 1, final_url)
                            )

            except (requests.RequestException, ValueError) as error:
                record.update({
                    "status": "skipped",
                    "error": str(error),
                })

                print(f"  Skipped: {error}")

            # JSONL: one JSON object on each line.
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
            file.flush()

    print(f"\nSaved {attempted} records to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Discover signup forms linked from TheFreeSite."
    )

    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--delay", type=float, default=3)
    parser.add_argument("--output", default="crawl.jsonl")

    args = parser.parse_args()

    if args.max_pages < 1 or args.depth < 0 or args.delay < 1:
        parser.error(
            "Use max-pages >= 1, depth >= 0, and delay >= 1."
        )

    crawl(
        max_pages=args.max_pages,
        max_depth=args.depth,
        delay=args.delay,
        output=args.output,
    )
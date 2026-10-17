import argparse
import json
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


DEFAULT_START_URLS = ["https://thefreesite.com/Newest_Freebies/"]
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " \
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
ROBOTS_USER_AGENT = "FreebieResearchBot"

SIGNUP_WORDS = re.compile(
    r"newsletter|subscribe|sign.?up|e.?club|join|register|reg.?form|"
    r"free.?trial|mailing.?list|opt.?in|get.?updates|email.?alerts?",
    re.IGNORECASE,
)

# Links that look like signup pages but are really login-only or
# account-management pages; used to rank candidates, never to exclude.
LOGIN_HINT = re.compile(
    r"log\s*-?\s*in|sign\s*-?\s*in|password|account|\blogin\b",
    re.IGNORECASE,
)

FILE_EXTENSIONS = re.compile(
    r"\.(pdf|zip|exe|jpg|jpeg|png|gif|svg|mp4|css|js)$",
    re.IGNORECASE,
)

# Common paths where sites host registration/newsletter pages.  Used
# as fallback seeds when a supplied URL is blocked or has no forms.
COMMON_SIGNUP_PATHS = (
    "/signup", "/sign-up", "/register", "/registration", "/join",
    "/account/create", "/account/register", "/create-account",
    "/newsletter", "/subscribe", "/subscription", "/mailing-list",
    "/email-signup", "/enews", "/e-newsletter", "/free-trial",
)

# Well-known third-party hosts whose page embeds the real signup form
# inside a cross-origin iframe (or redirects into one).
EMBED_HOSTS = re.compile(
    r"(^|\.)("
    r"mailchimp\.com|list-manage\.com|revue\.co|buttondown\.email|"
    r"convertkit\.com|grabify\.io|clerk\.io|substack\.api\.be|"
    r"forms\.hubspot\.com|hsforms\.net|hsforms\.com|jotform\.com|"
    r"typeform\.com|google\.com|docs\.google\.com|airtable\.com|"
    r"zoho\.com|surveymonkey\.com|olark\.com"
    r")\b",
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
        # Send a normal browser User-Agent so sites do not block the
        # crawler outright; robots.txt rules are still evaluated for
        # the named research bot below.
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        })

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

            elif status in {301, 302, 303, 307, 308}:
                # Follow one hop of a redirected robots.txt instead of
                # giving up and blocking the entire site.
                target = clean_url(robots_url, headers.get("Location", ""))

                if target and get_origin(target) == origin:
                    status2, _headers2, body2 = self.request(
                        target,
                        self.delay,
                    )

                    if status2 == 200:
                        text = body2.decode("utf-8", errors="replace")

                        if "<html" not in text[:1000].lower():
                            rules = text.splitlines()
                        else:
                            rules = ["User-agent: *", "Disallow:"]

            # Other statuses remain blocked under this conservative
            # policy.

        except (requests.RequestException, ValueError) as error:
            print(f"Could not retrieve {robots_url}: {error}")

        parser.parse(rules)

        delay = max(
            self.delay,
            parser.crawl_delay(USER_AGENT) or 0,
            parser.crawl_delay(ROBOTS_USER_AGENT) or 0,
        )

        rate = (
            parser.request_rate(USER_AGENT)
            or parser.request_rate(ROBOTS_USER_AGENT)
        )

        if rate and rate.requests > 0:
            # Conservative: wait an entire rate window between requests.
            delay = max(delay, rate.seconds)

        self.robots_cache[origin] = (parser, delay)
        return parser, delay

    def can_fetch(self, url):
        """True when the robots policy permits fetching this URL."""
        parser, _delay = self.get_robots_policy(url)
        return parser.can_fetch(ROBOTS_USER_AGENT, url)

    def fetch_html(self, url):
        """Fetch one URL without redirect-following; return (status, soup)."""
        status, headers, body = self.request(url, self.delay)

        content_type = headers.get("Content-Type", "").lower()
        is_htmlish = "html" in content_type or "xml" in content_type \
            or b"<" in body[:2000]

        if status != 200 or not is_htmlish:
            return status, None

        return status, BeautifulSoup(body, "html.parser")

    def get_html(self, url):
        """Fetch HTML, checking robots rules before each redirect hop."""
        for _ in range(6):
            parser, delay = self.get_robots_policy(url)

            if not parser.can_fetch(ROBOTS_USER_AGENT, url):
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


def decode_response_body(headers, body):
    """Best-effort text decoding of a response body."""
    charset = None
    content_type = headers.get("Content-Type", "")

    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)

    if match:
        charset = match.group(1)

    if not charset:
        head = body[:4096]

        match = re.search(
            rb'charset=["\']?([\w-]+)',
            head,
            re.IGNORECASE,
        )

        if match:
            charset = match.group(1).decode("ascii", "ignore")

    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def extract_scripts(soup):
    """Return inline JavaScript from script tags and on* attributes."""
    chunks = []

    for script in soup.find_all("script"):
        if script.get("src"):
            continue

        text = script.string or script.get_text() or ""

        if text.strip():
            chunks.append(text)

    for element in soup.find_all(attrs=True):
        for name, value in element.attrs.items():
            if name.startswith("on"):
                if isinstance(value, list):
                    value = " ".join(value)

                chunks.append(value)

    return "\n".join(chunks)


def find_form_actions_in_scripts(soup, page_url):
    """Discover AJAX endpoints referenced by inline scripts.

    Many modern sites render a plain <form> (or none at all) and post
    the data with fetch()/XMLHttpRequest to a JSON endpoint.  This
    heuristic pulls candidate endpoint strings out of inline JS so the
    submitter can fall back to them.
    """
    scripts = extract_scripts(soup)

    if not scripts:
        return []

    candidates = set()

    patterns = (
        r"""['"]((?:https?:)?//[A-Za-z0-9._~:/?#@!$&()*+,;=%-]+?(?:sign[-_]?up|register|subscribe|join|account/create|newsletter)[A-Za-z0-9._~:/?#@!$&()*+,;=%-]*)['"]""",
        r"""['"](/[A-Za-z0-9._~/-]*?(?:sign[-_]?up|register|subscribe|join|newsletter|mailinglist)[A-Za-z0-9._~/-]*)['"]""",
        r"""\.(?:post|put)\(\s*['"]([^'"]+)['"]""",
        r"""fetch\(\s*[`'"]([^`'"]+)[`'"]""",
        r"""action\s*[:=]\s*['"]([^'"]*(?:signup|register|subscribe)[^'"]*)['"]""",
    )

    for pattern in patterns:
        for match in re.finditer(pattern, scripts, re.IGNORECASE):
            raw = match.group(1)

            # Skip template placeholders such as `/api/${id}/subscribe`.
            if "${" in raw or "%s" in raw or "{" in raw or "}" in raw:
                continue

            url = clean_url(page_url, raw)

            if url:
                candidates.add(url)

    return sorted(candidates)


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

        # Forms on pages whose title/URL screams signup count as
        # candidates even when the surrounding label text is missing.
        if has_email_field and not signup_candidate:
            context = f"{page_url} {(soup.title.text if soup.title else '')}"

            if SIGNUP_WORDS.search(context):
                signup_candidate = True

        forms.append({
            "index": index,
            "action": (
                clean_url(base_url, action) if action else page_url
            ),
            "method": form.get("method", "get").lower(),
            "enctype": form.get(
                "enctype",
                "application/x-www-form-urlencoded",
            ).lower(),
            "fields": fields,
            "text": text[:600],
            "has_email_field": bool(has_email_field),
            "signup_candidate": signup_candidate,
        })

    return forms


def extract_iframe_sources(soup, page_url):
    """Find iframe srcs that likely host a third-party signup form."""
    base_url = get_base_url(soup, page_url)
    found = []

    for iframe in soup.find_all("iframe", src=True):
        url = clean_url(base_url, iframe["src"])

        if not url:
            continue

        label = " ".join(filter(None, (
            iframe.get("title", ""),
            iframe.get("id", ""),
            iframe.get("class") and " ".join(iframe["class"]),
            url,
        )))

        if EMBED_HOSTS.search(urlsplit(url).hostname or "") \
                or SIGNUP_WORDS.search(label):
            found.append(url)

    # Some embeds are anchor-based (e.g. Mailchimp inline links).
    for anchor in soup.find_all("a", href=True):
        if "list-manage.com/subscribe" in anchor["href"] \
                or "mailchimp.com/" in anchor["href"]:
            url = clean_url(base_url, anchor["href"])

            if url:
                found.append(url)

    return list(dict.fromkeys(found))


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


def candidate_signup_links(soup, page_url):
    """Rank same-origin links that plausibly lead to a signup page."""
    base_url = get_base_url(soup, page_url)
    scored = {}

    for anchor in soup.find_all("a", href=True):
        url = clean_url(base_url, anchor["href"])

        if not url:
            continue

        label = anchor.get_text(" ", strip=True)
        blob = f"{label} {anchor['href']} {anchor.get('title', '')}"

        if not SIGNUP_WORDS.search(blob):
            continue

        # Login-only pages are poor registration candidates; keep them
        # but rank them below explicit signup/register links.
        penalty = 1 if LOGIN_HINT.search(blob) and \
            not re.search(r"sign.?up|register|join|create", blob, re.I) else 0

        if url not in scored or penalty < scored[url][0]:
            scored[url] = (penalty, url)

    return [item[1] for item in sorted(scored.values())]


def fallback_signup_urls(client, page_url, visited):
    """Probe common registration paths when a seed page has no forms."""
    origin = get_origin(page_url)
    found = []

    for path in COMMON_SIGNUP_PATHS:
        url = origin + path

        if url in visited:
            continue

        visited.add(url)

        try:
            if not client.can_fetch(url):
                continue

            status, soup = client.fetch_html(url)

            if status == 200 and soup:
                forms = extract_forms(soup, url)

                if any(form["has_email_field"] for form in forms):
                    print(f"  Fallback probe hit: {url}")
                    found.append((url, soup, forms))
        except (requests.RequestException, ValueError) as error:
            print(f"  Fallback probe failed for {url}: {error}")

    return found


def crawl(max_pages, max_depth, delay, output, seeds=None):
    client = PoliteClient(delay)

    seeds = list(dict.fromkeys(seeds or DEFAULT_START_URLS))

    # Queue entries: URL, distance from starting page, referring page.
    queue = deque([(seed, 0, None) for seed in seeds])
    queued = set(seeds)
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
                iframe_sources = extract_iframe_sources(soup, final_url)
                ajax_endpoints = find_form_actions_in_scripts(
                    soup,
                    final_url,
                )

                record.update({
                    "url": final_url,
                    "status": "ok",
                    "title": (
                        soup.title.get_text(strip=True)
                        if soup.title else ""
                    ),
                    "forms": forms,
                    "iframe_sources": iframe_sources,
                    "ajax_endpoints": ajax_endpoints,
                })

                email_forms = sum(
                    form["has_email_field"] for form in forms
                )

                print(
                    f"  Found {len(forms)} forms; "
                    f"{email_forms} contain email fields; "
                    f"{len(iframe_sources)} signup iframes; "
                    f"{len(ajax_endpoints)} AJAX endpoints."
                )

                # Seed pages without usable forms: look harder before
                # giving up on this input link.
                if depth == 0 and not email_forms:
                    extra_links = candidate_signup_links(soup, final_url)

                    if not extra_links:
                        hits = fallback_signup_urls(
                            client,
                            final_url,
                            visited,
                        )

                        for hit_url, hit_soup, hit_forms in hits:
                            record.setdefault("fallback_pages", []).append({
                                "url": hit_url,
                                "title": (
                                    hit_soup.title.get_text(strip=True)
                                    if hit_soup.title else ""
                                ),
                                "forms": hit_forms,
                                "iframe_sources": extract_iframe_sources(
                                    hit_soup, hit_url
                                ),
                                "ajax_endpoints":
                                    find_form_actions_in_scripts(
                                        hit_soup, hit_url
                                    ),
                            })
                    else:
                        print(
                            f"  No direct form; queuing "
                            f"{len(extra_links)} candidate signup links."
                        )

                    for link in extra_links:
                        if link not in queued:
                            queued.add(link)
                            queue.appendleft((link, 1, final_url))

                elif depth == 0:
                    # Still enqueue obvious signup pages so a better
                    # form than the footer newsletter box can be found.
                    for link in candidate_signup_links(soup, final_url)[:5]:
                        if link not in queued:
                            queued.add(link)
                            queue.append((link, 1, final_url))

                # Third-party iframe embeds carry their own real form.
                for src in iframe_sources:
                    if src not in queued and len(queued) < max_pages * 20:
                        queued.add(src)
                        queue.append((src, depth + 1, final_url))

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

                # A blocked or broken seed should not end the run for
                # that input link: try common registration paths.
                if depth == 0:
                    hits = fallback_signup_urls(
                        client,
                        requested_url,
                        visited,
                    )

                    for hit_url, hit_soup, hit_forms in hits:
                        fallback_record = {
                            "requested_url": hit_url,
                            "url": hit_url,
                            "depth": 1,
                            "found_on": requested_url,
                            "fetched_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                            "status": "ok",
                            "title": (
                                hit_soup.title.get_text(strip=True)
                                if hit_soup.title else ""
                            ),
                            "forms": hit_forms,
                            "iframe_sources": extract_iframe_sources(
                                hit_soup, hit_url
                            ),
                            "ajax_endpoints": find_form_actions_in_scripts(
                                hit_soup, hit_url
                            ),
                        }

                        file.write(
                            json.dumps(
                                fallback_record, ensure_ascii=False
                            ) + "\n"
                        )
                        file.flush()

            # JSONL: one JSON object on each line.
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
            file.flush()

    print(f"\nSaved {attempted} records to {output}")


def load_seeds(url_args, url_file):
    """Collect seed URLs from command-line args and an optional file."""
    seeds = []

    for url in url_args:
        url = url.strip()

        if url:
            seeds.append(url)

    if url_file:
        text = Path(url_file).read_text(encoding="utf-8")

        for line in text.splitlines():
            line = line.strip()

            # Allow plain lists, CSV first column, or JSON arrays.
            if not line or line.startswith("#"):
                continue

            if "," in line:
                line = line.split(",")[0].strip()

            if line:
                seeds.append(line)

    cleaned = []

    for url in seeds:
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url

        parts = urlsplit(url)

        if parts.scheme in {"http", "https"} and parts.hostname:
            cleaned.append(url)
        else:
            print(f"Ignoring invalid seed URL: {url}", file=sys.stderr)

    return cleaned


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Discover signup forms from any input links "
            "(defaults to TheFreeSite)."
        )
    )

    parser.add_argument(
        "urls",
        nargs="*",
        help="One or more seed links to crawl (any website).",
    )
    parser.add_argument(
        "--url-file",
        help="File with one seed URL per line (# comments allowed).",
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

    seeds = load_seeds(args.urls, args.url_file) or DEFAULT_START_URLS

    crawl(
        max_pages=args.max_pages,
        max_depth=args.depth,
        delay=args.delay,
        output=args.output,
        seeds=seeds,
    )
"""Submit newsletter/registration forms discovered by freebies_crawler.

This revision removes the old restrictions that caused most sites to be
skipped: GET-only forms, multipart forms, select dropdowns, checkboxes,
hidden email inputs, multiple submit buttons, and externally-associated
fields are now handled automatically.  When a page has no usable HTML
form, the crawler's iframe sources and AJAX endpoint hints are used as
fallback submission targets.
"""
import argparse
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qsl

import requests
from bs4 import BeautifulSoup

from freebies_crawler import (
    SIGNUP_WORDS,
    PoliteClient,
    decode_response_body,
    extract_iframe_sources,
    find_form_actions_in_scripts,
)


AGENT = "FreebieResearchBot"

# Combine the crawler's discovery vocabulary with submitter-specific
# wording so pages/links found by either can be submitted.
SIGNUP = re.compile(
    r"newsletter|subscribe|sign\s*-?\s*up|register|join|e-?club|"
    r"mailing\s*-?\s*list|free\s*trial|opt\s*-?\s*in|"
    r"create\s+(?:an\s+)?account|get\s+updates|email\s+alerts",
    re.I,
)

CAPTCHA = re.compile(
    r"recaptcha|hcaptcha|turnstile|funcaptcha|areyouhuman|captcha",
    re.I,
)

SUCCESS = re.compile(
    r"successfully subscribed|subscription (?:is )?confirmed|"
    r"thank you for subscribing|thanks for subscribing|"
    r"you(?:'re| are| have been)? (?:subscribed|registered|signed up)|"
    r"welcome aboard|"
    r"account (?:created|success\w*)|registration (?:complete|success)\w*|"
    r"signed up successfully|thanks for signing up|"
    r"we'?ll keep you (?:posted|updated)",
    re.I,
)

PENDING = re.compile(
    r"check your (?:email|inbox)|confirm your (?:email|subscription)|"
    r"confirmation (?:email|link) (?:has been |was )?sent|"
    r"verify your (?:email|account)",
    re.I,
)

FAILURE = re.compile(
    r"invalid email|email (?:address )?is invalid|"
    r"please complete the captcha|required field|missing required|"
    r"subscription failed|unable to subscribe|is already registered|"
    r"is already taken|something went wrong|error occurred|"
    r"please fix the (?:following )?error",
    re.I,
)

# Field-name keywords mapped to safe synthetic values so that required
# registration fields no longer block an attempt.
NAME_KEYS = {"name", "fullname", "subscribername", "yourname", "username"}
FIRST_KEYS = {"firstname", "fname", "givenname"}
LAST_KEYS = {"lastname", "lname", "surname", "familyname"}
PHONE_KEYS = {"phone", "phonenumber", "mobile", "tel", "telephone"}
COUNTRY_KEYS = {"country", "countrycode"}
STATE_KEYS = {"state", "province", "region"}
ZIP_KEYS = {"zip", "zipcode", "postalcode", "postcode", "postal", "zip/postalcode"}
COMPANY_KEYS = {"company", "companyname", "organization", "org"}
ADDRESS_KEYS = {"address", "street", "address1", "addressline1"}
CITY_KEYS = {"city", "town"}
BIRTH_KEYS = {"birthday", "birthdate", "dob", "dateofbirth"}

EMAIL_KEY_RE = re.compile(r"e[-_.]?mail|emailaddress", re.I)

DEFAULT_PASSWORD = "Fb!2026xSec"


def origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def read_log(path):
    if not Path(path).exists():
        return []

    rows = []

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

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


def normalize_key(name):
    return re.sub(r"[^a-z]", "", (name or "").lower())


def looks_like_email_field(field):
    """True when an input/select is meant to receive an email address."""
    name = field.get("name") or ""
    placeholder = field.get("placeholder") or ""
    label = field.get("aria-label") or ""
    id_attr = field.get("id") or ""
    kind = (field.get("type") or "").lower()

    blob = f"{name} {placeholder} {label} {id_attr}"

    if kind == "email":
        return True

    if EMAIL_KEY_RE.search(blob.replace("-", "")) or \
            re.search(r"e.?mail", blob, re.I):
        return True

    # Hidden inputs named e-mail / EMAIL_1 etc.
    if kind == "hidden" and EMAIL_KEY_RE.search(normalize_key(name)):
        return True

    return False


def select_default_value(select):
    """Pick a value for a <select>: preferred/first non-empty option."""
    options = select.find_all("option")

    if not options:
        return None

    chosen = None

    for option in options:
        if option.has_attr("selected"):
            chosen = option
            break

    if chosen is None:
        # Skip empty-value placeholders where possible.
        real = [o for o in options if (o.get("value") or "") != ""]
        chosen = real[0] if real else options[0]

    return chosen.get("value", chosen.get_text(strip=True))


def fill_value(kind, key, normalized, field, args, required):
    """Return the value to submit for one control, or None to skip."""
    email = args.email
    first = (args.name.split() or ["Alex"])[0]
    last = (args.name.split()[1:] or ["Rivera"])[0]

    if looks_like_email_field(field):
        return email

    if kind == "hidden":
        # Keep existing hidden value; some sites store the email there.
        existing = field.get("value", "")

        if required and not existing and EMAIL_KEY_RE.search(normalized):
            return email

        return existing

    if normalized in NAME_KEYS and args.name:
        return args.name

    if normalized in FIRST_KEYS:
        return first

    if normalized in LAST_KEYS:
        return last

    if normalized in PHONE_KEYS:
        return args.phone

    if normalized in COUNTRY_KEYS:
        return args.country

    if normalized in ZIP_KEYS:
        return args.zip

    if normalized in COMPANY_KEYS:
        return args.company or "N/A"

    if normalized in ADDRESS_KEYS:
        return args.address

    if normalized in CITY_KEYS:
        return args.city

    if normalized in STATE_KEYS:
        return args.state

    if normalized in BIRTH_KEYS:
        return args.birthdate

    if kind == "password" or re.search(
        r"passw|pwd", key or "", re.I
    ):
        return args.password

    if kind == "checkbox":
        if field.has_attr("checked"):
            return field.get("value", "on")

        if required:
            return field.get("value", "on")

        # Optional marketing-consent boxes stay unchecked.
        return None

    if kind == "radio":
        if field.has_attr("checked"):
            return field.get("value", "on")

        if required:
            return field.get("value", "on")

        return None

    if kind in {"number", "tel"}:
        if required:
            return "0"

        return None

    if kind in {"date", "datetime-local", "month", "week", "time"}:
        if required:
            return args.birthdate if "birth" in normalized else "2000-01-01"

        return None

    if kind == "url":
        return args.website

    if kind in {"text", "search", "email", "textarea", "select"}:
        if required:
            # Unknown required text field: use a neutral placeholder
            # instead of abandoning the signup.
            return args.name or first

        return None

    return None


def form_index_of(soup, form):
    """Position of `form` among all forms on the page."""
    try:
        return soup.find_all("form").index(form)
    except ValueError:
        return None


def prepare(form, page_url, soup, args, page_meta=None):
    """
    Build the form payload.

    Hidden fields are preserved for submission, but their values
    are omitted from the audit log because they may contain tokens.
    Raises ValueError only for genuinely manual-only forms (CAPTCHAs,
    file uploads, JS-gated widgets).
    """
    description = (
        form.get_text(" ", strip=True)
        + " "
        + str(form.attrs)
        + " "
        + (soup.title.text if soup.title else "")
        + " "
        + page_url
    )

    page_context = f"{page_url} {soup.title.text if soup.title else ''}"

    # The crawler's own per-form classification lets a form qualify even
    # when the wording is unusual but was flagged during discovery.
    crawler_flagged = False

    if page_meta:
        for recorded in page_meta.get("forms", []):
            if recorded.get("index") == form_index_of(soup, form):
                crawler_flagged = bool(recorded.get("signup_candidate"))
                break

    if not (SIGNUP.search(description) or SIGNUP_WORDS.search(description)
            or crawler_flagged):
        if not (SIGNUP.search(page_context)
                or SIGNUP_WORDS.search(page_context)):
            raise ValueError(
                "Not clearly a newsletter/signup/registration form"
            )

    if CAPTCHA.search(str(form)) or CAPTCHA.search(
        str(form.find_parent(["div", "section", "form"]) or "")
    ):
        raise ValueError("CAPTCHA requires browser completion")

    if form.find("input", type="file"):
        raise ValueError("File upload required")

    method = form.get("method", "get").lower()

    encoding = form.get(
        "enctype",
        "application/x-www-form-urlencoded",
    ).lower()

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
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("Invalid submission endpoint")

    # Controls outside the <form> but linked via form="id".
    associated = []

    if form.get("id"):
        associated = soup.find_all(
            ["input", "select", "textarea"],
            attrs={"form": form["id"]},
        )

    controls = list(
        form.find_all(["input", "select", "textarea"])
    ) + associated

    data = []
    audit = []
    email_found = False

    seen_radios = set()

    for field in controls:
        key = field.get("name")

        if field.has_attr("disabled") or not key:
            continue

        tag = field.name
        kind = (
            field.get("type", "text" if tag == "input" else tag).lower()
        )
        normalized = normalize_key(key)

        if kind in {"submit", "button", "reset", "image"}:
            continue

        if tag == "select":
            value = select_default_value(field)

            if value is None:
                if field.has_attr("required"):
                    raise ValueError(
                        f"Select '{key}' has no options to choose"
                    )

                continue
        else:
            required = field.has_attr("required")

            if kind == "radio":
                # Submit exactly one radio per group.
                if key in seen_radios:
                    continue

                group = [f for f in controls
                         if f.name == "input"
                         and f.get("name") == key
                         and (f.get("type", "").lower() == "radio")]
                pick = next(
                    (f for f in group if f.has_attr("checked")),
                    group[0] if group else field,
                )
                seen_radios.add(key)
                field = pick
                value = pick.get("value", "on")

                if not value and required:
                    value = "on"
            else:
                value = fill_value(
                    kind, key, normalized, field, args, required
                )

        if kind == "email" or looks_like_email_field(field):
            email_found = True

        if value is None:
            continue

        if field.has_attr("readonly") and field.get("value"):
            value = field.get("value")

        if tag == "textarea" and value is None:
            value = field.get_text(strip=True) or None

            if value is None and field.has_attr("required"):
                value = args.name or "N/A"

        data.append((key, value))

        audit.append({
            "field": key,
            "value": (
                "[hidden value omitted]"
                if kind == "hidden" else value
            ),
        })

    if not email_found:
        # Single-input pages sometimes rely on a visible email box
        # without any recognizable naming; refuse rather than post junk.
        raise ValueError("No recognizable email field")

    buttons = form.select(
        'input[type="submit"], '
        'button:not([type]), '
        'button[type="submit"]'
    )

    button = None

    if buttons:
        # Prefer the button whose label mentions submitting; otherwise
        # the first one. Multiple buttons are no longer fatal.
        def score(b):
            text = (
                b.get("value", "")
                + " "
                + b.get_text(" ", strip=True)
            )

            return 0 if re.search(
                r"submit|sign|join|subscribe|register|send|go|yes|ok",
                text,
                re.I,
            ) else 1

        button = sorted(buttons, key=score)[0]

        if button.has_attr("formaction"):
            endpoint = urljoin(base_url, button["formaction"])

        if button.has_attr("formmethod"):
            method = button["formmethod"].lower()

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

    return endpoint, data, audit, method, encoding


def policy(client, endpoint):
    """Support either earlier version of freebies_crawler.py."""
    getter = (
        getattr(client, "get_robots_policy", None)
        or client.policy
    )

    rules, delay = getter(endpoint)

    if not rules.can_fetch(AGENT, endpoint):
        raise ValueError(
            "Submission endpoint blocked/unavailable under robots policy"
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
            phrase = match.group().strip()

            if not phrase:
                continue

            # Ignore success messages already present before submission.
            if phrase.lower() not in original_text.lower():
                return result, phrase

    return (
        "unverified",
        "HTTP response alone does not confirm subscription",
    )


def submit_form(client, endpoint, data, method, encoding, referer, delay):
    """Send the payload; returns (status, headers, body_bytes)."""
    time.sleep(delay)

    headers = {
        "Referer": referer,
        "Origin": origin(referer),
    }

    kwargs = {
        "timeout": 25,
        "allow_redirects": False,
        "stream": True,
        "headers": headers,
    }

    if method == "get":
        # Encode the collected values into the query string.
        from urllib.parse import urlencode

        separator = "&" if "?" in endpoint else "?"
        response = client.session.get(
            endpoint + separator + urlencode(data),
            **kwargs,
        )
    elif encoding == "multipart/form-data":
        response = client.session.post(
            endpoint,
            files=[(k, (None, v)) for k, v in data],
            **kwargs,
        )
    else:
        response = client.session.post(endpoint, data=data, **kwargs)

    with response:
        body = bytearray()

        for chunk in response.iter_content(65536):
            body.extend(chunk)

            if len(body) > 2_000_000:
                raise ValueError(
                    "Response too large; submission outcome unknown"
                )

        return response.status_code, dict(response.headers), bytes(body)


def follow_redirects(client, start_url, delay):
    """GET a URL following redirects manually; return (final_url, soup)."""
    url = start_url

    for _ in range(5):
        status, headers, body = client.request(url, min(delay, 2))

        if status in {301, 302, 303, 307, 308}:
            target = urljoin(url, headers.get("Location", ""))
            parts = urlsplit(target)

            if parts.scheme not in {"http", "https"} or target == url:
                return None

            url = target
            continue

        if status != 200:
            return None

        return url, BeautifulSoup(
            decode_response_body(headers, body), "html.parser"
        )

    return None


def gather_pages(crawl_rows):
    """Yield (page_url, title, forms, iframe_sources, ajax_endpoints)."""
    for row in crawl_rows:
        if row.get("status") != "ok":
            continue

        yield (
            row.get("url", row.get("requested_url", "")),
            row.get("title", ""),
            row.get("forms", []),
            row.get("iframe_sources", []),
            row.get("ajax_endpoints", []),
            row.get("fallback_pages", []),
        )


def try_ajax_fallback(client, page, endpoints, args, log_path, base_extra,
                      attempted=None):
    """POST JSON/URLENCODED guesses to script-discovered endpoints."""
    attempted = attempted if attempted is not None else set()
    results = []

    for endpoint in endpoints[:6]:
        if not client.can_fetch(endpoint):
            continue

        payload_variants = [
            ("json", {
                "email": args.email,
                "firstName": (args.name.split() or ["Alex"])[0],
                "lastName": " ".join(args.name.split()[1:]) or "Rivera",
                "name": args.name or "Alex Rivera",
            }),
            ("form", [("email", args.email)]),
        ]

        for style, payload in payload_variants:
            key = hashlib.sha256(
                (endpoint + style + args.email.lower()).encode()
            ).hexdigest()

            if key in attempted:
                continue

            attempted.add(key)

            try:
                delay = policy(client, endpoint)
                time.sleep(delay)

                if style == "json":
                    response = client.session.post(
                        endpoint,
                        json=payload,
                        timeout=25,
                        headers={
                            "Referer": page,
                            "Origin": origin(page),
                            "X-Requested-With": "XMLHttpRequest",
                        },
                        allow_redirects=False,
                    )
                else:
                    response = client.session.post(
                        endpoint,
                        data=payload,
                        timeout=25,
                        headers={
                            "Referer": page,
                            "Origin": origin(page),
                            "X-Requested-With": "XMLHttpRequest",
                        },
                        allow_redirects=False,
                    )

                status = response.status_code
                text = decode_response_body(
                    dict(response.headers), response.content
                )

                result, evidence = classify(status, text, "")

                entry = {
                    **base_extra,
                    "key": key,
                    "endpoint": endpoint,
                    "style": style,
                    "event": "attempt_result",
                    "result": result,
                    "http_status": status,
                    "evidence": evidence[:300],
                }
                record(log_path, entry)
                results.append(entry)

                # First promising endpoint wins.
                if result in {
                    "likely_success", "confirmation_required", "unverified"
                } and 200 <= status < 300:
                    return results
            except (requests.RequestException, ValueError) as error:
                record(log_path, {
                    **base_extra,
                    "key": key,
                    "endpoint": endpoint,
                    "style": style,
                    "event": "attempt_result",
                    "result": "error",
                    "evidence": str(error)[:300],
                })
                results.append({})

    return results


def attempt_site(client, url, soup, title, original_text, site_key,
                 args, attempted, used_sites, outcomes, page_meta=None):
    """Try every normal form on one page; return (submitted, attempts)."""
    forms = soup.find_all("form")
    attempts = 0

    for index, form in enumerate(forms):
        base = {
            "page": url,
            "service": title,
            "form_index": index,
        }

        try:
            endpoint, data, audit, method, encoding = prepare(
                form,
                url,
                soup,
                args,
                page_meta,
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
            method=method.upper(),
        )

        if not args.submit:
            print(f"PREVIEW: {title} -> {endpoint}\n  {audit}")

            record(args.log, {**base, "event": "preview"})
            continue

        # Log before sending: a lost response can still mean success.
        record(args.log, {
            **base,
            "event": "attempt_started",
        })

        attempted.add(key)
        used_sites.add(site_key)
        attempts += 1

        result, evidence, http_status = send_attempt(
            client, base, data, method, encoding, url, delay,
            original_text, args.log,
        )

        outcomes[result] += 1
        print(f"{title}: {result} ({evidence})")

        # One *sent* signup per source origin/email; but keep trying
        # other forms if this one could not even be prepared.
        if result != "error":
            return True, attempts

    return False, attempts


def send_attempt(client, base, data, method, encoding, referer, delay,
                 original_text, log_path):
    """Submit one prepared payload and classify the outcome."""
    endpoint = base["endpoint"]
    result = "unknown"
    evidence = ""
    http_status = None

    try:
        status, headers, body = submit_form(
            client, endpoint, data, method, encoding, referer, delay
        )
        http_status = status

        after = BeautifulSoup(
            decode_response_body(headers, body), "html.parser"
        ).get_text(" ", strip=True)

        if status in {301, 302, 303} and headers.get("Location"):
            landing = follow_redirects(
                client,
                urljoin(endpoint, headers.get("Location", "")),
                delay,
            )

            if landing:
                _, landing_soup = landing
                after = landing_soup.get_text(" ", strip=True)

            result, evidence = classify(200, after, original_text)

        elif status in {307, 308} and headers.get("Location"):
            # Replay the same method at the new location.
            retry_endpoint = urljoin(endpoint, headers["Location"])

            status, headers, body = submit_form(
                client, retry_endpoint, data, method, encoding,
                referer, delay,
            )
            http_status = status
            after = BeautifulSoup(
                decode_response_body(headers, body), "html.parser"
            ).get_text(" ", strip=True)
            result, evidence = classify(status, after, original_text)

        else:
            result, evidence = classify(http_status, after,
                                        original_text)

    except (requests.RequestException, ValueError) as error:
        result = "error"
        evidence = str(error)

    record(log_path, {
        **base,
        "event": "attempt_result",
        "result": result,
        "http_status": http_status,
        "evidence": evidence,
    })

    return result, evidence, http_status


def attempt_iframes(client, url, meta, title, original_text, site_key,
                    args, attempted, used_sites, outcomes):
    """Submit forms hosted inside third-party signup iframes."""
    attempts = 0

    for src in meta.get("iframes", [])[:3]:
        try:
            iframe_status, iframe_soup = client.fetch_html(src)

            if iframe_status != 200 or iframe_soup is None:
                continue

            inner_forms = iframe_soup.find_all("form")

            for index, form in enumerate(inner_forms):
                base = {
                    "page": url,
                    "service": title,
                    "form_index": f"iframe:{index}",
                    "iframe_src": src,
                }

                try:
                    endpoint, data, audit, method, enc = prepare(
                        form, src, iframe_soup, args
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
                    method=method.upper(),
                )

                if not args.submit:
                    print(f"PREVIEW (iframe): {title} -> {endpoint}")
                    record(args.log, {**base, "event": "preview"})
                    continue

                record(args.log, {**base, "event": "attempt_started"})

                attempted.add(key)
                used_sites.add(site_key)
                attempts += 1

                result, evidence, _status = send_attempt(
                    client, base, data, method, enc, src, delay,
                    original_text, args.log,
                )

                outcomes[result] += 1
                print(f"{title} (iframe): {result} ({evidence})")

                if result != "error":
                    return True, attempts

        except (requests.RequestException, ValueError):
            continue

    return False, attempts


def run(args):
    client = PoliteClient(3)

    # Respect the caller's pacing preference for all requests.
    client.delay = args.delay

    existing = read_log(args.log)

    # Remember attempts even if the previous run crashed or timed out.
    attempted = {
        row["key"]
        for row in existing
        if row.get("event") in {"attempt_started", "attempt_result"}
        and "key" in row
    }

    used_sites = {
        row["site_key"]
        for row in existing
        if row.get("event") in {"attempt_started", "attempt_result"}
        and "site_key" in row
    }

    outcomes = Counter()
    submissions = 0

    crawl_rows = read_log(args.input)

    recorded_rows = {}

    for row in crawl_rows:
        if row.get("status") != "ok":
            continue
        recorded_rows[row.get("url", row.get("requested_url", ""))] = row
        for fp in row.get("fallback_pages", []) or []:
            recorded_rows[fp.get("url", "")] = fp

    pages = []

    for (
        page_url,
        title,
        forms,
        iframe_sources,
        ajax_endpoints,
        fallback_pages,
    ) in gather_pages(crawl_rows):
        if not page_url:
            continue

        pages.append({
            "url": page_url,
            "title": title,
            "iframes": iframe_sources,
            "ajax": ajax_endpoints,
        })

        for fp in fallback_pages or []:
            pages.append({
                "url": fp.get("url", ""),
                "title": fp.get("title", ""),
                "iframes": fp.get("iframe_sources", []),
                "ajax": fp.get("ajax_endpoints", []),
            })

    # Deduplicate by URL first...
    seen_page_urls = set()
    unique_pages = []

    for page in pages:
        if page["url"] and page["url"] not in seen_page_urls:
            seen_page_urls.add(page["url"])
            unique_pages.append(page)

    # ...then group pages by origin so each site gets exactly one
    # submission slot; every page of an origin is tried (in order)
    # before the next site is considered.
    by_origin = {}
    origin_order = []

    for page in unique_pages:
        key = origin(page["url"])

        if key not in by_origin:
            by_origin[key] = []
            origin_order.append(key)

        by_origin[key].append(page)

    ordered_pages = [
        page
        for key in origin_order
        for page in by_origin[key]
    ]

    meta_by_url = {p["url"]: p for p in ordered_pages}

    done_origins = set()

    for page in ordered_pages:
        if submissions >= args.max_submissions:
            break

        url = page["url"]

        if origin(url) in done_origins:
            continue

        try:
            # Reload the form to obtain fresh hidden fields and cookies.
            url, soup = client.get_html(url)
        except (requests.RequestException, ValueError) as error:
            record(args.log, {
                "event": "page_skipped",
                "page": url,
                "reason": str(error),
            })
            continue

        title = page["title"] or (
            soup.title.get_text(strip=True)
            if soup.title else urlsplit(url).hostname
        )

        original_text = soup.get_text(" ", strip=True)

        site_key = hashlib.sha256(
            (origin(url) + args.email.lower()).encode()
        ).hexdigest()

        if site_key in used_sites:
            continue

        submitted_for_site, attempts = attempt_site(
            client,
            url,
            soup,
            title,
            original_text,
            site_key,
            args,
            attempted,
            used_sites,
            outcomes,
            recorded_rows.get(url),
        )

        submissions += attempts

        # No form could be sent for this page: try third-party iframe
        # embeds, then AJAX endpoint guesses discovered by the crawler.
        if not submitted_for_site and args.submit:
            meta = meta_by_url.get(url, {})

            submitted_for_site, attempts = attempt_iframes(
                client, url, meta, title, original_text, site_key,
                args, attempted, used_sites, outcomes,
            )
            submissions += attempts

            if not submitted_for_site and meta.get("ajax"):
                ajax_attempts = try_ajax_fallback(
                    client,
                    url,
                    meta["ajax"],
                    args,
                    args.log,
                    {
                        "page": url,
                        "service": title,
                        "site_key": site_key,
                        "form_index": "ajax",
                    },
                    attempted,
                )

                if ajax_attempts:
                    submissions += 1
                    used_sites.add(site_key)

        done_origins.add(origin(url))


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
        description=(
            "Submit newsletter/registration forms from crawl.jsonl "
            "(supports GET/POST, multipart, selects, checkboxes)."
        )
    )

    parser.add_argument("--input", default="crawl.jsonl")
    parser.add_argument("--log", default="signups.jsonl")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--max-submissions", type=int, default=10)
    parser.add_argument("--delay", type=float, default=3)
    parser.add_argument("--submit", action="store_true")

    # Synthetic profile values let previously-blocked required fields
    # be filled automatically.
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--phone", default="")
    parser.add_argument("--country", default="US")
    parser.add_argument("--state", default="")
    parser.add_argument("--zip", default="10001")
    parser.add_argument("--city", default="New York")
    parser.add_argument("--address", default="123 Main Street")
    parser.add_argument("--company", default="")
    parser.add_argument("--website", default="https://example.com")
    parser.add_argument("--birthdate", default="1990-01-01")

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

    if not args.name:
        args.name = "Alex Rivera"

    run(args)

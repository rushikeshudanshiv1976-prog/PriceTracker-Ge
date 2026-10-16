import ipaddress
import json
import re
import socket
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


ALLOWED_SCHEMES = {"http", "https"}
REQUEST_TIMEOUT = 15
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

GENERIC_TITLES = {
    "amazon",
    "amazon.in",
    "flipkart",
    "flipkart.com",
    "online shopping",
    "just a moment...",
}

PROVIDER_HOSTS = {
    "google.com",
    "google.co.in",
    "shopping.google.com",
    "serpapi.com",
}


def _clean_text(value):
    if isinstance(value, str):
        value = re.sub(r"\s+", " ", value).strip()
        return value or None
    return None


def _valid_http_url(value):
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None

    if (
        parsed.scheme not in ALLOWED_SCHEMES
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None

    return value.strip()


def is_provider_url(url: str) -> bool:
    """Return True for provider URLs and malformed URLs."""
    if not isinstance(url, str) or not url.strip():
        return True

    try:
        parsed = urlparse(url.strip())
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return True

    return any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in PROVIDER_HOSTS
    )


def _is_provider_url(url):
    return is_provider_url(url)


class _UnsafeURL(Exception):
    """Raised when a URL targets a non-public network location."""


def _assert_public_host(url: str) -> None:
    """
    Refuse URLs that resolve to loopback, private, link-local or otherwise
    non-public addresses (SSRF protection: cloud metadata endpoints,
    localhost services, the internal network).
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise _UnsafeURL("missing host")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise _UnsafeURL("invalid port") from exc

    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise _UnsafeURL("host could not be resolved") from exc

    if not infos:
        raise _UnsafeURL("host could not be resolved")

    for info in infos:
        address = info[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise _UnsafeURL("invalid address") from exc

        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped

        if not ip.is_global:
            raise _UnsafeURL("non-public address")


def _fetch(url: str):
    """
    GET a page, validating every redirect hop against the SSRF rules and
    capping the body size. Returns the final requests.Response with its
    content loaded.
    """
    current = url

    for _ in range(MAX_REDIRECTS + 1):
        if not _valid_http_url(current):
            raise _UnsafeURL("invalid URL")

        _assert_public_host(current)

        response = requests.get(
            current,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise _UnsafeURL("redirect without a destination")
            current = urljoin(current, location)

            # Stop following once we land on a provider page; the caller
            # reports it without fetching it.
            if is_provider_url(current):
                response = requests.models.Response()
                response.status_code = 302
                response.url = current
                response._content = b""
                return response
            continue

        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) >= MAX_RESPONSE_BYTES:
                break
        response.close()

        response._content = bytes(body)
        response._content_consumed = True
        return response

    raise _UnsafeURL("too many redirects")


def _valid_product_title(value, hostname):
    title = _clean_text(value)
    if not title:
        return None

    normalized = title.lower()
    host = (hostname or "").lower().removeprefix("www.")

    if normalized in GENERIC_TITLES:
        return None

    if normalized == host or normalized == host.split(".")[0]:
        return None

    return title


def _title_from_url(url: str) -> str | None:
    """Display-only fallback for Amazon India product URLs."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "amazon.in" and not host.endswith(".amazon.in"):
        return None

    match = re.search(
        r"/([^/]+)/dp/([A-Z0-9]{10})(?:/|$)",
        parsed.path,
        re.IGNORECASE,
    )

    if not match:
        match = re.search(
            r"/dp/([A-Z0-9]{10})(?:/|$)",
            parsed.path,
            re.IGNORECASE,
        )
        if not match:
            return None
        return f"Amazon product {match.group(1).upper()}"

    slug = unquote(match.group(1))
    slug = re.sub(r"[-_]+", " ", slug)
    slug = re.sub(r"\s+", " ", slug).strip()

    return (
        slug[:250]
        if slug
        else f"Amazon product {match.group(2).upper()}"
    )


def title_from_url(url: str) -> str | None:
    """
    Readable product name taken from the URL path (Amazon /name/dp/ASIN,
    Flipkart /name/p/itm...). Used only when the store blocks the page.
    Returns None when the URL carries no real name.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().rstrip(".")
    slug = None

    if "flipkart." in host:
        match = re.match(r"^/([^/]+)/p/", parsed.path or "")
        if match:
            slug = match.group(1)
    elif "amazon." in host:
        match = re.search(r"/([^/]+)/(?:dp|gp/product)/[A-Z0-9]{10}",
                          parsed.path or "", re.IGNORECASE)
        if match:
            slug = match.group(1)

    if not slug:
        return None

    slug = re.sub(r"[-_]+", " ", unquote(slug))
    slug = re.sub(r"\s+", " ", slug).strip()
    return slug[:250] if len(slug) >= 3 else None


def _extract_price(value):
    """Parse positive prices from numbers or strings."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        price = float(value)
        return price if price > 0 else None

    text = str(value).strip().replace(",", "")
    if not text:
        return None

    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None

    try:
        price = float(match.group())
        return price if price > 0 else None
    except (ValueError, TypeError):
        return None


def _extract_jsonld_products(data):
    """Walk nested JSON-LD objects and @graph structures."""
    stack = [data]

    while stack:
        candidate = stack.pop()

        if isinstance(candidate, list):
            stack.extend(candidate)
            continue

        if not isinstance(candidate, dict):
            continue

        yield candidate

        for key in ("@graph", "mainEntity", "itemListElement"):
            nested = candidate.get(key)
            if isinstance(nested, (dict, list)):
                stack.append(nested)


def _offer_values(offers):
    """Yield offer dictionaries, including AggregateOffer values."""
    if isinstance(offers, dict):
        yield offers
        nested = offers.get("offers")
        if isinstance(nested, (dict, list)):
            yield from _offer_values(nested)

    elif isinstance(offers, list):
        for offer in offers:
            yield from _offer_values(offer)


def _extract_jsonld(soup, hostname):
    title = None
    price = None
    image_url = None
    currency = None

    for script in soup.find_all(
        "script",
        attrs={"type": re.compile(r"application/ld\+json", re.I)},
    ):
        raw = script.string or script.get_text()

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        for candidate in _extract_jsonld_products(data):
            types = candidate.get("@type", [])
            if isinstance(types, str):
                types = [types]

            if not any(
                str(item).lower() in {"product", "productgroup"}
                for item in types
            ):
                continue

            title = title or _valid_product_title(
                candidate.get("name"), hostname
            )

            image = candidate.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url") or image.get("contentUrl")

            image_url = image_url or _clean_text(image)

            for offer in _offer_values(candidate.get("offers")):
                if not isinstance(offer, dict):
                    continue

                offer_price = _extract_price(offer.get("price"))
                if offer_price is None:
                    offer_price = _extract_price(offer.get("lowPrice"))

                if price is None and offer_price is not None:
                    price = offer_price

                currency = currency or _clean_text(
                    offer.get("priceCurrency")
                )

    return title, price, image_url, currency


def _meta_content(soup, attributes):
    node = soup.find("meta", attrs=attributes)
    if not node:
        return None
    return _clean_text(node.get("content"))


def _extract_meta_price(soup):
    selectors = [
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
        {"name": "price"},
    ]

    for selector in selectors:
        price = _extract_price(_meta_content(soup, selector))
        if price is not None:
            return price

    node = soup.find(attrs={"itemprop": "price"})
    if node:
        price = _extract_price(
            node.get("content")
            or node.get("value")
            or node.get_text(" ", strip=True)
        )
        if price is not None:
            return price

    return None


def _extract_html_price(soup, hostname):
    """
    Read the displayed selling price from the product page HTML.

    Amazon and Flipkart rarely put the price in JSON-LD or meta tags, so the
    price is read from the visible price element instead. Struck-through
    list prices (M.R.P.) are skipped on purpose.
    """
    host = (hostname or "").lower()

    if "amazon." in host:
        selectors = (
            "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
            "#corePrice_feature_div .priceToPay .a-offscreen",
            "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
            "#apex_desktop .priceToPay .a-offscreen",
            "#apex_desktop .a-price:not(.a-text-price) .a-offscreen",
            "span.priceToPay .a-offscreen",
            "#priceblock_dealprice",
            "#priceblock_ourprice",
            "#price_inside_buybox",
            "#tp_price_block_total_price_ww .a-offscreen",
            ".a-price:not(.a-text-price) .a-offscreen",
        )
    elif "flipkart." in host:
        selectors = (
            "div.Nx9bqj.CxhGGd",
            "div.Nx9bqj",
            "div._30jeq3._16Jk6d",
            "div._30jeq3",
            "div.hZ3P6w",
        )
    else:
        return None

    for selector in selectors:
        try:
            nodes = soup.select(selector)
        except Exception:
            continue

        for node in nodes[:3]:
            price = _extract_price(
                node.get("content") or node.get_text(" ", strip=True)
            )
            if price is not None:
                return price

    return None


def _looks_like_block_page(html, response_url="", status_code=None):
    """
    Detect common retailer access-denial pages.

    Avoid searching raw HTML for the word 'captcha' alone because it
    frequently appears in scripts and hidden markup on normal pages.
    """
    html = html or ""

    if status_code in {403, 429, 503}:
        return True

    try:
        parsed = urlparse(response_url or "")
    except ValueError:
        parsed = urlparse("")

    path = (parsed.path or "").lower()
    if any(
        marker in path
        for marker in ("/captcha", "/validatecaptcha", "/robot")
    ):
        return True

    soup = BeautifulSoup(html, "html.parser")

    for node in soup(
        ["script", "style", "noscript", "template"]
    ):
        node.decompose()

    visible_text = re.sub(
        r"\s+", " ", soup.get_text(" ", strip=True)
    ).lower()

    challenge_phrases = (
        "robot check",
        "are you a robot",
        "verify you are human",
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "automated access to amazon data",
        "to discuss automated access",
        "complete the security check",
        "security verification",
        "unusual traffic",
        "request blocked",
        "access denied",
    )

    if any(phrase in visible_text for phrase in challenge_phrases):
        return True

    host = (parsed.hostname or "").lower()
    is_amazon = (
        host == "amazon.in"
        or host.endswith(".amazon.in")
        or host == "amazon.com"
        or host.endswith(".amazon.com")
    )

    # Only use the short-page heuristic for Amazon. A short page without
    # product data is suspicious, but this is not proof of a CAPTCHA.
    if is_amazon and len(html) < 15000:
        lowered_html = html.lower()
        product_signals = (
            "producttitle",
            "landingimage",
            "priceblock",
            "apex_desktop",
            "coreprice",
        )
        if not any(signal in lowered_html for signal in product_signals):
            return True

    return False


def _failure(message, url=None, status="error"):
    return {
        "status": status,
        "message": message,
        "title": None,
        "current_price": None,
        "image_url": None,
        "currency": None,
        "url": url,
        "price_status": "unavailable",
    }


def scrape_product_page(url: str):
    """Scrape a retailer page without treating provider links as prices."""
    validated_url = _valid_http_url(url)

    if not validated_url:
        return _failure("Enter a valid HTTP or HTTPS product URL.")

    if is_provider_url(validated_url):
        return _failure(
            "This is a Google Shopping/provider URL, not a direct "
            "retailer product page. Open the retailer's product page "
            "before automatic price refresh can work.",
            validated_url,
        )

    try:
        response = _fetch(validated_url)
    except _UnsafeURL:
        return _failure(
            "This URL cannot be fetched. Use a public retailer product URL.",
            validated_url,
        )
    except requests.RequestException as exc:
        return _failure(
            f"Could not retrieve product page: {type(exc).__name__}.",
            validated_url,
        )

    final_url = response.url
    final_validated_url = _valid_http_url(final_url)

    if not final_validated_url:
        return _failure(
            "The retailer redirected to an invalid URL.",
            validated_url,
        )

    if is_provider_url(final_validated_url):
        return _failure(
            "The retailer redirected to a Google Shopping/provider page.",
            final_validated_url,
        )

    if _looks_like_block_page(
        response.text,
        final_validated_url,
        response.status_code,
    ):
        return _failure(
            "The retailer returned an access-denied or verification "
            "page. No new price was recorded. Try again later or use "
            "the retailer's permitted access method.",
            final_validated_url,
            status="blocked",
        )

    try:
        response.raise_for_status()
    except requests.RequestException:
        # 404/410 mean the product page is really gone; any other error
        # (403, 429, 5xx, Flipkart's 529) is how stores turn bots away.
        return _failure(
            f"The retailer returned HTTP {response.status_code}.",
            final_validated_url,
            status=(
                "error"
                if response.status_code in {404, 410}
                else "blocked"
            ),
        )

    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and (
        "html" not in content_type and "xhtml" not in content_type
    ):
        return _failure(
            "The URL did not return an HTML product page.",
            final_validated_url,
        )

    soup = BeautifulSoup(response.text, "html.parser")
    hostname = urlparse(final_validated_url).hostname or ""

    title, price, image_url, currency = _extract_jsonld(
        soup, hostname
    )

    if not title:
        for selector in (
            {"property": "og:title"},
            {"name": "twitter:title"},
        ):
            title = _valid_product_title(
                _meta_content(soup, selector), hostname
            )
            if title:
                break

    if not title:
        node = soup.select_one("#productTitle")
        if node:
            title = _valid_product_title(
                node.get_text(" ", strip=True), hostname
            )

    if not title and soup.title:
        title = _valid_product_title(
            soup.title.get_text(" ", strip=True), hostname
        )

    if not image_url:
        image_url = (
            _meta_content(soup, {"property": "og:image"})
            or _meta_content(soup, {"name": "twitter:image"})
        )

    if price is None:
        price = _extract_meta_price(soup)

    if price is None:
        price = _extract_html_price(soup, hostname)

    if not image_url:
        node = soup.select_one("#landingImage, #imgTagWrapperId img")
        if node:
            image_url = _clean_text(
                node.get("data-old-hires") or node.get("src")
            )

    if not currency:
        currency = (
            _meta_content(
                soup, {"property": "product:price:currency"}
            )
            or _meta_content(soup, {"itemprop": "priceCurrency"})
        )

    # Amazon URL slug is only a display-name fallback, not proof
    # that the page contains a verified product price.
    if not title:
        title = _title_from_url(final_validated_url)

    if not title:
        return {
            "status": "partial",
            "message": (
                "The page was retrieved, but no reliable product title "
                "was found. The retailer may restrict automated access."
            ),
            "title": None,
            "current_price": None,
            "image_url": image_url,
            "currency": currency,
            "url": final_validated_url,
            "price_status": "unavailable",
        }

    if price is None:
        return {
            "status": "partial",
            "message": (
                "The product page was retrieved, but no reliable current "
                "price was found. No price history or price alert should "
                "be created from this scrape."
            ),
            "title": title,
            "current_price": None,
            "image_url": image_url,
            "currency": currency,
            "url": final_validated_url,
            "price_status": "unavailable",
        }

    return {
        "status": "success",
        "message": "Current price extracted from the retailer page.",
        "title": title,
        "current_price": float(price),
        "image_url": image_url,
        "currency": currency or "INR",
        "url": final_validated_url,
        "price_status": "observed",
    }
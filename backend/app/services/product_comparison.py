
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse


def normalize_title(title: str) -> str:
    text = (title or "").lower()
    text = re.sub(r"\b(new|official|online|latest)\b", " ", text)
    text = re.sub(r"[^\w.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(title: str) -> set[str]:
    return set(normalize_title(title).split())


def _gpu(title: str) -> set[str]:
    text = normalize_title(title)
    return {
        "".join(match)
        for match in re.findall(
            r"\b(rtx|gtx|rx)\s*(\d{3,4})\b", text
        )
    }


def _processor(title: str) -> set[str]:
    text = normalize_title(title)
    found = set()

    patterns = (
        r"\bcore\s*[3579]\s*\d{3,5}[a-z]*\b",
        r"\bi[3579]\s*\d{3,5}[a-z]*\b",
        r"\bryzen\s*[3579]\s*\d{3,5}[a-z]*\b",
    )

    for pattern in patterns:
        for match in re.findall(pattern, text):
            found.add(re.sub(r"\s+", "", match))

    return found


def _product_family(title: str) -> str | None:
    text = normalize_title(title)

    patterns = (
        (r"\baspire\s*7\b", "aspire 7"),
        (r"\baspire\s*lite\b", "aspire lite"),
        (r"\baspire\s*3\b", "aspire 3"),
        (r"\baspire\b", "aspire"),
        (r"\balg\b", "alg"),
        (r"\bnitro\b", "nitro"),
        (r"\bpredator\b", "predator"),
        (r"\btravelmate\b", "travelmate"),
    )

    for pattern, family in patterns:
        if re.search(pattern, text):
            return family

    return None


def _variant_attributes(title: str) -> dict[str, set[str]]:
    """
    Extract attributes that can distinguish product variants.
    Missing attributes do not count as conflicts.
    """
    text = normalize_title(title)
    attributes: dict[str, set[str]] = {}

    # GPU and processor models
    gpu = _gpu(text)
    processor = _processor(text)

    if gpu:
        attributes["gpu"] = gpu

    if processor:
        attributes["processor"] = processor

    # Laptop memory/storage specifications
    for key, pattern in {
        "ram": r"\b(4|8|12|16|24|32|48|64|96|128)\s*gb\s*ram\b",
        "storage": r"\b(128|256|512|1024|2048|4096)\s*(gb|tb)\s*(ssd|hdd|storage)\b",
    }.items():
        matches = set(re.findall(pattern, text))
        if matches:
            attributes[key] = {
                " ".join(match) if isinstance(match, tuple) else match
                for match in matches
            }

    # Shoe sizes, when explicitly identified
    shoe_sizes = set(
        re.findall(
            r"\b(?:uk|us|eu)\s*(\d{1,2}(?:\.\d)?)\b",
            text,
        )
    )

    # Only use shoe sizes when a size marker exists.
    marked_sizes = set(
        re.findall(
            r"\b(uk|us|eu)\s*(\d{1,2}(?:\.\d)?)\b",
            text,
        )
    )
    if marked_sizes:
        attributes["shoe_size"] = {
            f"{system}{size}" for system, size in marked_sizes
        }

    # Explicit gender categories can distinguish product variants.
    for key, pattern in {
        "gender": r"\b(men|mens|women|womens|boys|girls)\b",
        "storage_capacity": r"\b(64|128|256|512|1024|2048)\s*gb\b",
    }.items():
        matches = set(re.findall(pattern, text))
        if matches:
            attributes[key] = matches

    return attributes


def _has_conflicting_attributes(first: str, second: str) -> bool:
    a = _variant_attributes(first)
    b = _variant_attributes(second)

    for attribute in a.keys() & b.keys():
        if a[attribute] != b[attribute]:
            return True

    return False


def same_product(first: dict, second: dict) -> bool:
    a = normalize_title(first.get("title", ""))
    b = normalize_title(second.get("title", ""))

    if not a or not b:
        return False

    if a == b:
        return True

    family_a = _product_family(a)
    family_b = _product_family(b)

    if family_a and family_b and family_a != family_b:
        return False

    if _has_conflicting_attributes(a, b):
        return False

    gpu_a, gpu_b = _gpu(a), _gpu(b)
    if gpu_a and gpu_b and gpu_a != gpu_b:
        return False

    processor_a, processor_b = _processor(a), _processor(b)
    if processor_a and processor_b and processor_a != processor_b:
        return False

    tokens_a = title_tokens(a)
    tokens_b = title_tokens(b)

    overlap = len(tokens_a & tokens_b) / max(
        1, len(tokens_a | tokens_b)
    )
    similarity = SequenceMatcher(None, a, b).ratio()

    # Conservative matching: require strong title agreement.
    return overlap >= 0.75 or similarity >= 0.88


def _valid_price(offer: dict) -> bool:
    price = offer.get("price")

    return (
        isinstance(price, (int, float))
        and not isinstance(price, bool)
        and price > 0
    )


def _canonical_retailer(retailer: str | None) -> str:
    name = re.sub(r"[^a-z0-9]+", " ", (retailer or "").lower())
    name = re.sub(r"\s+", " ", name).strip()

    if "flipkart" in name or "shopsy" in name:
        return "flipkart"

    if "tata cliq" in name or "tatacliq" in name:
        return "tata cliq"

    if "amazon" in name:
        return "amazon"

    return name


def _image_identity(offer: dict) -> str:
    image = offer.get("image_url")

    if not isinstance(image, str) or not image.strip():
        return ""

    try:
        parsed = urlparse(image.strip())
        return f"{parsed.netloc.lower()}{parsed.path.rstrip('/').lower()}"
    except ValueError:
        return image.strip().lower()


def _listing_identity(offer: dict) -> tuple:
    """
    Identify repeated provider listings independently of provider IDs
    and tracking parameters in their URLs.
    """
    return (
        _canonical_retailer(offer.get("retailer")),
        normalize_title(offer.get("title", "")),
        round(float(offer["price"]), 2) if _valid_price(offer) else None,
        _image_identity(offer),
    )


def _offer_quality(offer: dict) -> tuple:
    return (
        _valid_price(offer),
        offer.get("link_type") == "retailer_direct",
        bool(offer.get("image_url")),
    )


def compare_product_offers(results: list[dict]) -> list[dict]:
    groups = []

    for result in results:
        if not isinstance(result, dict):
            continue

        if not result.get("title") or not result.get("url"):
            continue

        matching_group = next(
            (
                group
                for group in groups
                if same_product(result, group["offers"][0])
            ),
            None,
        )

        if matching_group is None:
            matching_group = {
                "title": result["title"],
                "offers": [],
            }
            groups.append(matching_group)

        identity = _listing_identity(result)

        duplicate_index = next(
            (
                index
                for index, existing in enumerate(
                    matching_group["offers"]
                )
                if _listing_identity(existing) == identity
            ),
            None,
        )

        if duplicate_index is None:
            matching_group["offers"].append(result)
        else:
            previous = matching_group["offers"][duplicate_index]

            if _offer_quality(result) > _offer_quality(previous):
                matching_group["offers"][duplicate_index] = result

    for group in groups:
        group["offers"].sort(
            key=lambda offer: (
                not _valid_price(offer),
                offer["price"] if _valid_price(offer) else float("inf"),
            )
        )

       
        # Search results carry "provider_reported" prices (verified ones only
        # exist for scraped retailer pages), so accept both. Each offer keeps
        # its own price_status so the frontend can still label the basis.
        priced_offers = [
            offer
            for offer in group["offers"]
            if _valid_price(offer)
            and offer.get("price_status") in {"verified", "provider_reported"}
        ]

        cheapest = priced_offers[0] if priced_offers else None

        group["lowest_price"] = (
            cheapest["price"] if cheapest else None
        )
        group["lowest_price_retailer"] = (
            cheapest["retailer"] if cheapest else None
        )
        group["offer_count"] = len(group["offers"])
        group["verified_offer_count"] = len(priced_offers)


    groups.sort(
        key=lambda group: (
            group["lowest_price"] is None,
            (
                group["lowest_price"]
                if group["lowest_price"] is not None
                else float("inf")
            ),
        )
    )

    return groups

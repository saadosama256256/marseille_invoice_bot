import os
import re
import logging
import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)

from app.ai_parser import parse_invoice_message, InvoiceRequest, InvoiceItem
from app.database import (
    init_db,
    create_request,
    update_request,
    get_request,
    claim_approval,
    claim_client_creation,
    set_client_created,
    set_client_state,
    get_or_set_invoice_number,
    set_invoice_created,
    set_request_status,
)
from app.invoice_api import InvoiceNinjaAPI
from app.product_matcher import match_product
from app.invoice_builder import (
    prepare_invoice_items,
    build_invoice_payload,
    InvoiceValidationError,
)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MANAGER_TELEGRAM_ID = os.getenv("MANAGER_TELEGRAM_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in .env")
if not MANAGER_TELEGRAM_ID:
    raise ValueError("MANAGER_TELEGRAM_ID is missing in .env")

try:
    MANAGER_TELEGRAM_ID = int(MANAGER_TELEGRAM_ID)
except ValueError as exc:
    raise ValueError("MANAGER_TELEGRAM_ID must be a number") from exc

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# الطلبات التي تنتظر اختيار المدير. لا تُنشأ فاتورة في هذه المرحلة.
pending_product_choices = {}


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def is_manager(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == MANAGER_TELEGRAM_ID


def normalize_digits(text: str) -> str:
    translation = str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789",
    )
    return text.translate(translation)


def normalize_text(text: str) -> str:
    text = normalize_digits(text).strip().lower()
    text = re.sub(r"[\u064B-\u065F\u0670ـ]", "", text)
    replacements = {"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def parse_amount(text: str):
    text = normalize_digits(text).strip().replace(",", "").replace("،", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ألف|الف|مليون)?", text)
    if not match:
        return None
    try:
        number = Decimal(match.group(1))
    except InvalidOperation:
        return None
    scale = match.group(2)
    if scale in ("ألف", "الف"):
        number *= 1000
    elif scale == "مليون":
        number *= 1000000
    if not number.is_finite() or number < 0:
        return None
    return float(number)


def handle_simple_message(text: str) -> str | None:
    normalized = normalize_text(text)
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    greetings = {
        "السلام عليكم", "السلام عليكم ورحمة الله",
        "السلام عليكم ورحمة الله وبركاته", "وعليكم السلام",
        "هلا", "هلا والله", "مرحبا", "مرحب", "اهلا", "اهلين",
        "يا هلا", "صباح الخير", "مساء الخير", "صباح النور", "مساء النور",
    }
    thanks = {
        "شكرا", "شكرا ليك", "تسلم", "تسلم يا غالي", "مشكور",
        "بارك الله فيك", "جزاك الله خير",
    }
    if normalized in greetings:
        return "وعليكم السلام ورحمة الله وبركاته 🌹\nأهلاً بيك يا مدير، كيف نقدر نخدمك؟"
    if normalized in thanks:
        return "العفو يا مدير، تحت أمرك دائماً ❤️"
    return None


# --------------------------------------------------
# Local invoice parsing
# --------------------------------------------------

UNIT_PATTERNS = [
    ("متر مربع", r"متر\s*مربع"),
    ("متر طولي", r"متر\s*طولي"),
    ("كرتونة", r"كرتونة|كرتونه"),
    ("قطعة", r"قطعة|قطعه"),
    ("طقم", r"طقم"),
    ("كف", r"كف"),
    ("متر", r"متر"),
]


def split_message_segments(message: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[/\r\n]+", message)
        if part.strip()
    ]


def extract_client_and_address(parts: list[str]):
    client_name = None
    client_address = None
    remaining = []

    for part in parts:
        current = part.strip()

        # تصحيح أخطاء الكتابة الشائعة
        current = re.sub(
            r"العنون",
            "العنوان",
            current
        )

        # استخراج العنوان الصريح إذا ورد في جزء مستقل مثل "العنوان الخرطوم"
        explicit_address_match = re.match(
            r"^العنوان\s*[:\-]?\s*(.+)$",
            current,
        )
        if explicit_address_match:
            address_value = explicit_address_match.group(1).strip()
            if address_value:
                client_address = address_value
            continue

        # استخراج اسم العميل
        client_match = re.search(
            r"(?:الفاتورة|الفاتوره)?\s*(?:بي\s*)?اسم\s+(.+)$",
            current,
        )

        if not client_match:
            client_match = re.search(
                r"للعميل\s+(.+)$",
                current
            )

        if client_match:
            extracted_text = client_match.group(1).strip()

            if not extracted_text:
                return None, None, None

            # فصل الاسم عن العنوان إذا وردت كلمة العنوان
            address_match = re.search(
                r"\s+(?:العنوان)\s*[:\-]?\s*(.+)$",
                extracted_text
            )

            if address_match:
                client_name = extracted_text[:address_match.start()].strip()
                client_address = address_match.group(1).strip()
            else:
                client_name = extracted_text

            if not client_name:
                return None, None, None

            current = current[:client_match.start()].strip()

            current = re.sub(
                r"(?:اعمل\s+لي\s+فاتورة|اعمل\s+لي\s+فاتوره|"
                r"اعمل\s+فاتورة|اعمل\s+فاتوره|"
                r"جهز\s+لي\s+فاتورة|جهز\s+لي\s+فاتوره|"
                r"داير\s+فاتورة|داير\s+فاتوره|"
                r"عايز\s+فاتورة|عايز\s+فاتوره|"
                r"الفاتورة|الفاتوره)",
                "",
                current,
            ).strip()

        if current:
            remaining.append(current)

    # استخراج العنوان من الجزء الأخير فقط إذا لم يكن صنفاً أو سعراً أو يحتوي على أرقام/وحدات
    if client_name and remaining and not client_address:
        last = remaining[-1].strip()
        normalized_last = normalize_digits(last)

        has_digits = bool(re.search(r"\d", normalized_last))
        has_unit = any(
            re.search(pattern, last, flags=re.IGNORECASE)
            for _, pattern in UNIT_PATTERNS
        )
        looks_like_price = parse_separate_price_line(last) is not None

        if not has_digits and not has_unit and not looks_like_price:
            client_address = last
            remaining.pop()

    return client_name, client_address, remaining



def extract_inline_price(text: str):
    text = normalize_digits(text)
    match = re.search(
        r"(?:السعر\s*(?:بي|بـ|ب)?|بسعر)\s*"
        r"(\d+(?:\.\d+)?\s*(?:ألف|الف|مليون)?)",
        text,
    )
    if not match:
        return text, None, False
    raw_amount = match.group(1)
    price = parse_amount(raw_amount)
    if price is None:
        return None, None, False
    has_scale = bool(re.search(r"(?:ألف|الف|مليون)", raw_amount))
    cleaned = text[:match.start()] + " " + text[match.end():]
    return cleaned.strip(), price, has_scale


def parse_item_line(text: str):
    text = normalize_digits(text.strip())

    quantity_match = re.match(
        r"^(\d+(?:\.\d+)?)\s+(.+)$",
        text
    )

    if not quantity_match:
        return None

    try:
        quantity = float(quantity_match.group(1))
    except ValueError:
        return None

    if quantity <= 0:
        return None

    remainder = quantity_match.group(2).strip()

    unit = None
    product_name = remainder

    for unit_name, pattern in UNIT_PATTERNS:
        match = re.match(
            r"^(?:" + pattern + r")\s+(.+)$",
            remainder,
            flags=re.IGNORECASE
        )

        if match:
            unit = unit_name
            if unit_name == "كف":
                product_name = remainder
            else:
                product_name = match.group(1).strip()
            break

    if not unit or not product_name:
        return None

    return {
        "name": product_name,
        "quantity": quantity,
        "unit": unit,
    }


def parse_separate_price_line(text: str):
    text = normalize_digits(text.strip())
    match = re.fullmatch(
        r"(.+?)\s*(?:بي|بـ|ب)\s*"
        r"(\d+(?:\.\d+)?\s*(?:ألف|الف|مليون)?)",
        text,
    )
    if not match:
        return None
    label = normalize_text(match.group(1))
    raw_amount = match.group(2)
    price = parse_amount(raw_amount)
    if price is None:
        return None
    has_scale = bool(re.search(r"(?:ألف|الف|مليون)", raw_amount))
    if "المربع" in label:
        key = "متر مربع"
    elif "الطولي" in label:
        key = "متر طولي"
    elif "كف" in label and "اسد" in label:
        key = "كف أسد"
    else:
        return None
    return {"key": key, "price": price, "has_scale": has_scale}


def apply_neighboring_item_context(items: list[dict]) -> None:
    """
    Preserve product-family context (e.g. مارسيليا) when a subsequent item
    is written only as an origin adjective like 'إيطالي' after 'مارسيليا إيطاليه'.
    """
    last_marseille_origin = None
    for item in items:
        norm_name = normalize_text(item["name"])
        if "مارسيليا" in norm_name or "مارسليا" in norm_name:
            if "ايطالي" in norm_name or "ايطاليه" in norm_name:
                last_marseille_origin = "إيطالي"
            else:
                last_marseille_origin = None
        elif norm_name in ("ايطالي", "ايطاليه") and last_marseille_origin == "إيطالي":
            item["name"] = f"مارسيليا {item['name']}"


def build_abbreviated_price_clarification(items: list[InvoiceItem]) -> str | None:
    ambiguous = [
        item for item in items
        if item.custom_price is not None and 0 < item.custom_price < 1000
    ]
    if not ambiguous:
        return None
    details = "، ".join(
        f"{item.name}: {item.custom_price:g}"
        for item in ambiguous
    )
    return (
        "الأسعار المذكورة مختصرة وقد تحتمل أكثر من تفسير "
        f"({details}).\n"
        "هل المقصود بها بالآلاف (مثل 340,000) أم القيمة كما هي؟"
    )


def count_expected_item_segments(message: str) -> int:
    normalized = normalize_digits(message)
    segments = split_message_segments(normalized)
    _, _, remaining = extract_client_and_address(segments)
    if remaining is None:
        return 0
    count = 0
    for seg in remaining:
        if parse_separate_price_line(seg) is not None:
            continue
        if re.match(r"^\d+(?:\.\d+)?\s+\S+", seg):
            count += 1
    return count


def verify_extraction_completeness(message: str, parsed: InvoiceRequest) -> str | None:
    if parsed.clarification_needed:
        return parsed.clarification_needed

    if not parsed.items:
        return (
            "لم أتمكن من استخراج أصناف من رسالتك.\n"
            "يرجى توضيح الأصناف والكميات."
        )

    expected_count = count_expected_item_segments(message)
    if expected_count > len(parsed.items):
        return (
            f"تم رصد {expected_count} أصناف في رسالتك، "
            f"لكن تم استخراج {len(parsed.items)} فقط.\n"
            "يرجى توضيح بقية الأصناف قبل المتابعة."
        )

    for idx, item in enumerate(parsed.items, start=1):
        if not item.name or not item.name.strip():
            return f"اسم الصنف رقم {idx} غير واضح. يرجى توضيحه."
        if item.quantity is None or item.quantity <= 0:
            return f"كمية الصنف ({item.name}) غير صحيحة. يرجى توضيحها."

    return build_abbreviated_price_clarification(parsed.items)


def local_parse_invoice_message(message: str):
    if not message or not message.strip():
        return None
    message = normalize_digits(message)
    parts = split_message_segments(message)
    client_name, client_address, parts = extract_client_and_address(parts)
    if parts is None:
        return None

    client_phone = None
    item_parts = []

    for part in parts:
        phone_match = re.fullmatch(
            r"(?:رقم\s*)?(?:الهاتف|هاتف|الجوال|جوال|تلفون|تليفون|موبايل)"
            r"\s*[:\-]?\s*(\+?\d[\d\s\-]{6,}\d)",
            part.strip(),
        )
        if phone_match:
            if client_phone is None:
                client_phone = re.sub(r"[\s\-]", "", phone_match.group(1))
            continue

        item_parts.append(part)

    parts = item_parts

    items = []
    separate_prices = []
    for part in parts:
        separate_price = parse_separate_price_line(part)
        if separate_price:
            separate_prices.append(separate_price)
            continue
        cleaned_line, inline_price, _ = extract_inline_price(part)
        if cleaned_line is None:
            return None
        parsed_item = parse_item_line(cleaned_line)
        if parsed_item is None:
            return None
        parsed_item["custom_price"] = inline_price
        items.append(parsed_item)

    if not items:
        return None

    used_price_keys = set()
    for price_info in separate_prices:
        key = price_info["key"]
        if key in used_price_keys:
            return None
        used_price_keys.add(key)
        matching_items = []
        for item in items:
            if key in ("متر مربع", "متر طولي") and item["unit"] == key:
                matching_items.append(item)
            elif key == "كف أسد":
                normalized_name = normalize_text(item["name"])
                if item["unit"] == "كف" and "كف" in normalized_name and "اسد" in normalized_name:
                    matching_items.append(item)
        if len(matching_items) != 1:
            return None
        target_item = matching_items[0]
        if target_item["custom_price"] is not None:
            return None
        target_item["custom_price"] = price_info["price"]

    apply_neighboring_item_context(items)

    invoice_items = [
        InvoiceItem(
            name=item["name"],
            quantity=item["quantity"],
            unit=item["unit"],
            custom_price=item["custom_price"],
        )
        for item in items
    ]

    clarification = build_abbreviated_price_clarification(invoice_items)
    missing_info = ["توضيح الأسعار المختصرة"] if clarification else []

    return InvoiceRequest(
        client_name=client_name,
        client_phone=client_phone,
        client_address=client_address,
        items=invoice_items,
        missing_information=missing_info,
        clarification_needed=clarification,
    )


# --------------------------------------------------
# Invoice Ninja product matching and preview
# --------------------------------------------------

async def get_product_catalog():
    api = InvoiceNinjaAPI()
    try:
        result = await api.get_products(page=1)
        return result.get("data", [])
    finally:
        await api.close()


def format_money(value) -> str:
    return f"{float(value):,.2f}"


@dataclass
class ClientResolution:
    """Outcome of a client lookup. Never silently selects partial matches."""
    status: str  # exact | ambiguous | none
    client: dict | None = None
    candidates: list = field(default_factory=list)


def match_clients(client_name: str, clients: list) -> ClientResolution:
    """
    Match a client only by full normalized name.

    Invoice Ninja's `filter` search is partial/fuzzy, so its results are
    candidates only. A partial result must never be treated as a match.
    """
    target = normalize_text(client_name or "")
    if not target:
        return ClientResolution(status="none", client=None, candidates=[])

    exact = {}
    for client in clients or []:
        names = [
            client.get("name"),
            client.get("display_name"),
        ]
        if any(
            name and normalize_text(str(name)) == target
            for name in names
        ):
            exact[client.get("id")] = client

    exact_clients = list(exact.values())
    if len(exact_clients) == 1:
        return ClientResolution(
            status="exact",
            client=exact_clients[0],
            candidates=exact_clients,
        )
    if len(exact_clients) > 1:
        return ClientResolution(
            status="ambiguous",
            client=None,
            candidates=exact_clients,
        )
    return ClientResolution(
        status="none",
        client=None,
        candidates=list(clients or []),
    )


def select_client(client_name: str, clients: list):
    """Return a client only when the full normalized name matches exactly."""
    return match_clients(client_name, clients).client


async def resolve_client(parsed: InvoiceRequest) -> ClientResolution:
    if not parsed.client_name:
        return ClientResolution(status="none", client=None, candidates=[])

    api = InvoiceNinjaAPI()
    try:
        clients = await api.search_clients(parsed.client_name)
    except Exception:
        logging.exception("Failed to search clients for %s", parsed.client_name)
        return ClientResolution(status="none", client=None, candidates=[])
    finally:
        await api.close()

    return match_clients(parsed.client_name, clients)


def client_display_name(client: dict) -> str:
    return (
        (client or {}).get("name")
        or (client or {}).get("display_name")
        or (client or {}).get("id")
        or "عميل غير معروف"
    )


def client_name_matches(expected_name: str, client: dict) -> bool:
    target = normalize_text(expected_name or "")
    if not target or not isinstance(client, dict):
        return False
    return any(
        name and normalize_text(str(name)) == target
        for name in (client.get("name"), client.get("display_name"))
    )


def build_client_payload(client_name: str, client_phone: str = None,
                         client_address: str = None) -> dict:
    """
    Build a minimal Invoice Ninja v5 client payload.

    v5 supports name, display_name, phone, city and address1. The business
    stores the area/address in `city`, so the extracted address goes there.
    """
    name = (client_name or "").strip()
    if not name:
        raise ValueError("اسم العميل مطلوب لإنشاء عميل جديد.")

    payload = {
        "name": name,
        "display_name": name,
    }

    phone = (client_phone or "").strip()
    if phone:
        payload["phone"] = phone

    address = (client_address or "").strip()
    if address:
        payload["city"] = address

    return payload


def build_invoice_keyboard(request_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ اعتماد وإنشاء الفاتورة",
                callback_data=f"approve:{request_id}",
            ),
            InlineKeyboardButton(
                text="❌ إلغاء",
                callback_data=f"cancel:{request_id}",
            ),
        ]
    ])


def build_client_choice_keyboard(
    request_id: int,
    candidates: list = None,
    include_pick: bool = True,
    include_create: bool = True,
    include_force_create: bool = False,
):
    """Buttons for choosing an existing client, creating one, or cancelling."""
    rows = []

    for client in (candidates or [])[:8]:
        client_id = client.get("id")
        if not client_id:
            continue
        rows.append([
            InlineKeyboardButton(
                text=f"👤 {client_display_name(client)}"[:60],
                callback_data=f"csel:{request_id}:{client_id}",
            )
        ])

    if include_pick:
        rows.append([
            InlineKeyboardButton(
                text="🔎 اختيار عميل موجود",
                callback_data=f"cpick:{request_id}",
            )
        ])
    if include_create:
        rows.append([
            InlineKeyboardButton(
                text="➕ إنشاء عميل جديد",
                callback_data=f"ccreate:{request_id}",
            )
        ])
    if include_force_create:
        rows.append([
            InlineKeyboardButton(
                text="➕ إنشاء عميل جديد رغم التشابه",
                callback_data=f"ccreateforce:{request_id}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="❌ إلغاء",
            callback_data=f"cancel:{request_id}",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_product_keyboard(
    request_id: int,
    item_index: int,
    candidates: list,
    allow_free_form: bool = False,
):
    buttons = []

    for candidate_index, product in enumerate(candidates):
        name = (
            product.get("name")
            or product.get("product_key")
            or product.get("notes")
            or "منتج بدون اسم"
        )

        buttons.append([
            InlineKeyboardButton(
                text=name[:55],
                callback_data=f"pm:{request_id}:{item_index}:{candidate_index}",
            )
        ])

    if allow_free_form:
        buttons.append([
            InlineKeyboardButton(
                text="استخدام الوصف والسعر كما هو (بند حر)",
                callback_data=f"pf:{request_id}:{item_index}",
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_item_search_name(item) -> str:
    """
    Build the catalog search string for an item.
    Avoid duplicating the unit when the item name already contains it
    (e.g. "كف أسد إيطالي" + unit "كف" must not become "كف كف أسد إيطالي").
    """
    name = (item.name or "").strip()
    unit = (item.unit or "").strip()

    if not unit:
        return name

    if normalize_text(unit) in normalize_text(name):
        return name

    return f"{unit} {name}".strip()


async def continue_product_matching(message: Message, request_id: int):
    state = pending_product_choices.get(request_id)
    if not state:
        return

    parsed = state["parsed"]
    products = state["products"]

    for item_index, item in enumerate(parsed.items):
        # تم حسم هذا الصنف مسبقاً (مطابقة كتالوج أو بند حر)
        if item_index in state["matches"]:
            continue

        search_name = build_item_search_name(item)
        result = match_product(search_name, products)
        status = result.get("status")

        # 1) تطابق قوي وواضح: اعتماده تلقائياً دون سؤال المدير
        if status == "matched":
            state["matches"][item_index] = {
                "status": "matched",
                "product": result["product"],
            }
            continue

        # 2) لا يوجد تطابق موثوق، لكن المدير زوّد وصفاً وسعراً: بند حر
        if status == "not_found" and item.custom_price is not None:
            state["matches"][item_index] = {
                "status": "free_form",
                "product": None,
            }
            continue

        # 3) حالة غامضة: نعرض المرشحين المتوافقين فقط،
        #    مع السماح بالبند الحر إذا زوّد المدير سعراً.
        candidates = result.get("candidates", [])
        allow_free_form = item.custom_price is not None

        if not candidates and not allow_free_form:
            await message.answer(
                f"لم أجد منتجاً مناسباً للصنف: {item.name}\n\n"
                "يرجى مراجعة اسم الصنف في الكتالوج أو إضافة سعر الوحدة "
                "لإنشاء بند حر، ثم إعادة إرسال الطلب."
            )
            pending_product_choices.pop(request_id, None)
            await update_request(request_id, status="needs_product_review")
            return

        state["waiting_item_index"] = item_index
        state["waiting_candidates"] = candidates
        state["waiting_allow_free_form"] = allow_free_form

        prompt_lines = [
            f"الصنف: {item.name}",
            f"الكمية: {item.quantity:g} {item.unit or ''}".rstrip(),
            "",
        ]
        if candidates:
            prompt_lines.append(
                "اختر المنتج الصحيح من الكتالوج، "
                "أو استخدم وصفك وسعرك كبند حر:"
            )
        else:
            prompt_lines.append(
                "لم أجد مطابقة كتالوج موثوقة. يمكنك اعتماد الوصف والسعر كبند حر:"
            )

        await message.answer(
            "\n".join(prompt_lines),
            reply_markup=build_product_keyboard(
                request_id,
                item_index,
                candidates,
                allow_free_form=allow_free_form,
            ),
        )
        return

    await show_invoice_preview(message, request_id)


def build_prepared_data(prepared: dict, parsed: InvoiceRequest,
                        client: dict = None, source: str = None) -> dict:
    return {
        "client_id": client.get("id") if client else None,
        "client_name": (
            client_display_name(client) if client else parsed.client_name
        ),
        "client_extracted_name": parsed.client_name,
        "client_phone": parsed.client_phone,
        "client_address": parsed.client_address,
        "items": prepared["items"],
        "subtotal": prepared["subtotal"],
        "client_source": source,
    }


def format_invoice_preview_lines(
    request_id: int,
    prepared: dict,
    client_name: str,
    client_phone: str,
    client_address: str,
    resolved_client: dict = None,
    note: str = None,
) -> list:
    has_free_form = any(
        line.get("is_free_form") for line in prepared.get("items", [])
    )

    header = (
        "تم تجهيز الفاتورة (يشمل بنوداً حرة بوصف المدير) ✅"
        if has_free_form
        else "تمت مطابقة جميع الأصناف من الكتالوج ✅"
    )

    lines = [
        header,
        "",
        f"رقم الطلب: {request_id}",
        f"العميل: {client_name or 'غير محدد'}",
        f"الهاتف: {client_phone or 'غير مذكور'}",
        f"العنوان: {client_address or 'غير مذكور'}",
        "",
        "تفاصيل الفاتورة:",
    ]

    for item in prepared.get("items", []):
        source = "بند حر" if item.get("is_free_form") else "من الكتالوج"
        lines.extend([
            f"\n• {item['description']}",
            f"  الكمية: {item['quantity']:g}",
            f"  سعر الوحدة: {format_money(item['unit_price'])}",
            f"  الإجمالي: {format_money(item['line_total'])}",
            f"  المصدر: {source}",
        ])

    lines.extend([
        "",
        f"الإجمالي قبل الضرائب: {format_money(prepared.get('subtotal', 0))}",
    ])

    if note:
        lines.extend(["", note])

    if resolved_client:
        lines.extend([
            "",
            f"العميل في Invoice Ninja: {client_display_name(resolved_client)}",
            "هذه معاينة فقط. لم يتم إنشاء الفاتورة.",
            "لن تُنشأ الفاتورة قبل ضغط زر الاعتماد.",
            "لتصحيح أي صنف أو إضافة سعر، أرسل الطلب المعدّل من جديد.",
        ])
    else:
        lines.extend([
            "",
            "لم أجد تطابقاً دقيقاً لاسم العميل في Invoice Ninja.",
            "نتائج البحث الجزئي مجرد مرشحين، وليست دليلاً على التطابق.",
            "لم يتم إنشاء أي عميل أو فاتورة بعد.",
        ])

    return lines


async def show_invoice_preview(message: Message, request_id: int):
    state = pending_product_choices.get(request_id)
    if not state:
        return

    parsed = state["parsed"]
    matched_products = [
        state["matches"].get(index, {"status": "not_found", "product": None})
        for index in range(len(parsed.items))
    ]

    try:
        prepared = prepare_invoice_items(parsed.items, matched_products)
    except InvoiceValidationError as error:
        await message.answer(f"تعذر تجهيز المعاينة:\n{error}")
        await update_request(request_id, status="validation_error")
        pending_product_choices.pop(request_id, None)
        return

    resolution = await resolve_client(parsed)

    if resolution.status == "exact":
        client = resolution.client
        await set_client_created(
            request_id, client.get("id"), client_display_name(client),
        )
        prepared_data = build_prepared_data(
            prepared, parsed, client=client, source="exact",
        )
        await update_request(
            request_id=request_id,
            status="awaiting_approval",
            prepared_data=json.dumps(prepared_data, ensure_ascii=False),
        )
        lines = format_invoice_preview_lines(
            request_id,
            prepared,
            prepared_data["client_name"],
            parsed.client_phone,
            parsed.client_address,
            resolved_client=client,
        )
        await message.answer(
            "\n".join(lines),
            reply_markup=build_invoice_keyboard(request_id),
        )
        pending_product_choices.pop(request_id, None)
        return

    prepared_data = build_prepared_data(prepared, parsed)
    await update_request(
        request_id=request_id,
        status="needs_client_choice",
        prepared_data=json.dumps(prepared_data, ensure_ascii=False),
    )

    if resolution.status == "ambiguous":
        note = (
            "وجدت أكثر من عميل بنفس الاسم. اختر العميل الصحيح بنفسك، "
            "أو أنشئ عميلاً جديداً، أو ألغِ الطلب."
        )
        candidates = resolution.candidates
        include_pick = False
    else:
        note = (
            "لم أجد تطابقاً دقيقاً. يمكنك إنشاء عميل جديد، "
            "أو اختيار عميل موجود، أو إلغاء الطلب."
        )
        candidates = None
        include_pick = True

    lines = format_invoice_preview_lines(
        request_id,
        prepared,
        parsed.client_name,
        parsed.client_phone,
        parsed.client_address,
        resolved_client=None,
        note=note,
    )
    await message.answer(
        "\n".join(lines),
        reply_markup=build_client_choice_keyboard(
            request_id,
            candidates=candidates,
            include_pick=include_pick,
            include_create=True,
        ),
    )
    pending_product_choices.pop(request_id, None)


async def show_client_ready_preview(message, request_id: int, client: dict,
                                    source: str, note: str = None):
    record = await get_request(request_id)
    if not record:
        await message.answer("الطلب غير موجود.")
        return

    if record.get("invoice_id"):
        await message.answer(
            "الفاتورة موجودة مسبقاً لهذا الطلب. لا يمكن تغيير العميل."
        )
        return

    try:
        prepared_data = json.loads(record.get("prepared_data") or "{}")
    except (TypeError, ValueError):
        prepared_data = {}

    prepared_data["client_id"] = client.get("id")
    prepared_data["client_name"] = client_display_name(client)
    prepared_data["client_source"] = source

    await update_request(
        request_id=request_id,
        status="awaiting_approval",
        prepared_data=json.dumps(prepared_data, ensure_ascii=False),
    )

    prepared = {
        "items": prepared_data.get("items", []),
        "subtotal": prepared_data.get("subtotal", 0),
    }
    lines = format_invoice_preview_lines(
        request_id,
        prepared,
        prepared_data.get("client_name"),
        prepared_data.get("client_phone"),
        prepared_data.get("client_address"),
        resolved_client=client,
        note=note,
    )
    await message.answer(
        "\n".join(lines),
        reply_markup=build_invoice_keyboard(request_id),
    )


async def find_exact_client(api, client_name: str) -> ClientResolution:
    clients = await api.search_clients(client_name)
    return match_clients(client_name, clients)


async def show_client_candidates(message, request_id: int, candidates: list,
                                 note: str = None,
                                 include_force_create: bool = False):
    if not candidates:
        await message.answer(
            (note + "\n\n" if note else "")
            + "لم أجد أي عميل مرشح. يمكنك إنشاء عميل جديد أو الإلغاء.",
            reply_markup=build_client_choice_keyboard(
                request_id,
                candidates=None,
                include_pick=False,
                include_create=True,
            ),
        )
        return

    await message.answer(
        (note + "\n\n" if note else "")
        + "هذه نتائج بحث جزئية، وليست مطابقة مؤكدة. اختر العميل الصحيح:",
        reply_markup=build_client_choice_keyboard(
            request_id,
            candidates=candidates,
            include_pick=False,
            include_create=True,
            include_force_create=include_force_create,
        ),
    )


async def recover_uncertain_client(message, request_id: int, api,
                                   client_name: str):
    """After an uncertain POST, search before allowing any new POST."""
    try:
        resolution = await find_exact_client(api, client_name)
    except Exception:
        logging.exception(
            "Could not verify client creation for request %s", request_id
        )
        await set_client_state(request_id, "unknown")
        await message.answer(
            "لم أستطع تأكيد نتيجة إنشاء العميل. لن أُنشئ فاتورة.\n"
            "راجع Invoice Ninja يدوياً قبل إعادة المحاولة."
        )
        return

    if resolution.status == "exact":
        client = resolution.client
        await set_client_created(request_id, client.get("id"), client_name)
        await show_client_ready_preview(
            message,
            request_id,
            client,
            source="existing",
            note="تبيّن أن العميل موجود مسبقاً. تم استخدامه ولم يُنشأ عميل جديد.",
        )
        return

    if resolution.status == "ambiguous":
        await set_client_state(request_id, "needs_client_choice")
        await show_client_candidates(
            message,
            request_id,
            resolution.candidates,
            note="وجدت أكثر من عميل بنفس الاسم بعد انقطاع الاتصال. اختر بنفسك.",
            include_force_create=True,
        )
        return

    await set_client_state(request_id, "unknown")
    await message.answer(
        "لم أتأكد من إنشاء العميل بعد انقطاع الاتصال. لن أُنشئ فاتورة.\n"
        "راجع Invoice Ninja يدوياً قبل إعادة المحاولة."
    )


async def create_client_for_request(message, request_id: int,
                                    force_new: bool = False):
    """
    Manager-approved client creation/selection.

    This is only called from an explicit manager button. It never creates a
    client from the preview itself. `force_new` is only used after the manager
    receives an ambiguity warning and explicitly chooses to create anyway.
    """
    record = await get_request(request_id)
    if not record:
        await message.answer("الطلب غير موجود.")
        return

    if record.get("invoice_id"):
        await message.answer(
            "الفاتورة موجودة مسبقاً لهذا الطلب. لا يمكن تغيير العميل."
        )
        return

    if record.get("client_id"):
        api = InvoiceNinjaAPI()
        try:
            result = await api.get_client(record["client_id"])
            client = result.get("data", result)
        except Exception:
            logging.exception("Failed to reload client for request %s", request_id)
            client = None
        finally:
            await api.close()
        if isinstance(client, dict) and client.get("id"):
            await show_client_ready_preview(
                message, request_id, client, source="existing",
                note="العميل محدد مسبقاً لهذا الطلب.",
            )
        else:
            await message.answer(
                "العميل المحفوظ لم يعد متاحاً. أرسل الطلب من جديد."
            )
        return

    try:
        prepared_data = json.loads(record.get("prepared_data") or "{}")
    except (TypeError, ValueError):
        prepared_data = {}

    client_name = (
        prepared_data.get("client_extracted_name")
        or prepared_data.get("client_name")
        or ""
    ).strip()
    if not client_name:
        await set_client_state(request_id, "failed")
        await message.answer(
            "اسم العميل غير محدد. لا يمكن إنشاء عميل. لم تُنشأ فاتورة."
        )
        return

    claimed = await claim_client_creation(request_id)
    if not claimed:
        current = await get_request(request_id)
        if current and current.get("client_id"):
            await message.answer(
                "العميل محدد بالفعل لهذا الطلب. راجع المعاينة السابقة."
            )
            return

        if current and current.get("client_state") in ("creating", "unknown"):
            api = InvoiceNinjaAPI()
            try:
                try:
                    resolution = await find_exact_client(api, client_name)
                except Exception:
                    resolution = ClientResolution(status="none")
            finally:
                await api.close()

            if resolution.status == "exact":
                client = resolution.client
                await set_client_created(
                    request_id, client.get("id"), client_name
                )
                await show_client_ready_preview(
                    message,
                    request_id,
                    client,
                    source="existing",
                    note="تم العثور على العميل بعد التحقق. لم يُنشأ عميل مكرر.",
                )
                return

        await message.answer(
            "جاري إنشاء أو تحديد العميل لهذا الطلب بالفعل. "
            "لن يتم إنشاء عميل مكرر."
        )
        return

    api = InvoiceNinjaAPI()
    try:
        try:
            resolution = await find_exact_client(api, client_name)
        except Exception:
            logging.exception(
                "Failed to run pre-create client dedupe for request %s",
                request_id,
            )
            await set_client_state(request_id, "failed")
            await message.answer(
                "تعذر البحث عن العميل قبل الإنشاء. "
                "لم يتم إنشاء عميل أو فاتورة."
            )
            return

        if resolution.status == "exact":
            client = resolution.client
            await set_client_created(request_id, client.get("id"), client_name)
            await show_client_ready_preview(
                message,
                request_id,
                client,
                source="existing",
                note="وجدت عميلاً مطابقاً بالاسم. تم استخدامه ولم يُنشأ عميل جديد.",
            )
            return

        if resolution.status == "ambiguous" and not force_new:
            await set_client_state(request_id, "needs_client_choice")
            await show_client_candidates(
                message,
                request_id,
                resolution.candidates,
                note=(
                    "يوجد أكثر من عميل بنفس الاسم. "
                    "اختر عميلاً موجوداً، أو اضغط زر الإنشاء رغم التشابه "
                    "إذا كنت متأكداً أن العميل جديد فعلاً."
                ),
                include_force_create=True,
            )
            return

        if resolution.status == "ambiguous" and force_new:
            logging.warning(
                "Request %s: creating client despite %s exact name matches",
                request_id,
                len(resolution.candidates),
            )

        try:
            payload = build_client_payload(
                client_name,
                prepared_data.get("client_phone"),
                prepared_data.get("client_address"),
            )
        except ValueError:
            await set_client_state(request_id, "failed")
            await message.answer(
                "اسم العميل غير صالح للإنشاء. لم يتم إنشاء عميل أو فاتورة."
            )
            return

        try:
            result = await api.create_client(payload)
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            logging.exception(
                "Client creation failed for request %s with HTTP %s",
                request_id,
                status_code,
            )
            if status_code >= 500:
                await recover_uncertain_client(
                    message, request_id, api, client_name,
                )
            else:
                await set_client_state(request_id, "failed")
                await message.answer(
                    "رفض Invoice Ninja إنشاء العميل. "
                    "لم يتم إنشاء عميل أو فاتورة."
                )
            return
        except Exception:
            logging.exception(
                "Client creation result uncertain for request %s", request_id
            )
            await recover_uncertain_client(
                message, request_id, api, client_name,
            )
            return

        data = result.get("data", result) if isinstance(result, dict) else {}
        client_id = data.get("id") if isinstance(data, dict) else None
        if not client_id:
            await recover_uncertain_client(
                message, request_id, api, client_name,
            )
            return

        client = dict(data)
        if not client.get("name") and not client.get("display_name"):
            client["name"] = client_name
        client["id"] = client_id

        await set_client_created(request_id, client_id, client_name)
        await show_client_ready_preview(
            message,
            request_id,
            client,
            source="created",
            note="تم إنشاء العميل. راجع الفاتورة ثم اضغط اعتماد لإنشاء فاتورة واحدة.",
        )
    finally:
        await api.close()


async def select_existing_client(message, request_id: int, client_id: str):
    """Manager explicitly chose an existing candidate."""
    record = await get_request(request_id)
    if not record:
        await message.answer("الطلب غير موجود.")
        return

    if record.get("invoice_id"):
        await message.answer(
            "الفاتورة موجودة مسبقاً لهذا الطلب. لا يمكن تغيير العميل."
        )
        return

    api = InvoiceNinjaAPI()
    try:
        try:
            result = await api.get_client(client_id)
            client = result.get("data", result)
        except Exception:
            logging.exception(
                "Failed to load selected client %s for request %s",
                client_id,
                request_id,
            )
            await message.answer(
                "تعذر جلب العميل المختار. لم يتم إنشاء أي فاتورة."
            )
            return
    finally:
        await api.close()

    if (
        not isinstance(client, dict)
        or not client.get("id")
        or client.get("is_deleted")
        or not (client.get("name") or client.get("display_name"))
    ):
        await message.answer(
            "العميل المختار غير صالح أو محذوف. لم يتم إنشاء أي فاتورة."
        )
        return

    await set_client_created(request_id, client.get("id"), client_display_name(client))
    await show_client_ready_preview(
        message,
        request_id,
        client,
        source="selected",
        note="تم اعتماد العميل المختار من قِبل المدير.",
    )


# --------------------------------------------------
# Telegram handlers
# --------------------------------------------------

@dp.message(CommandStart())
async def start_handler(message: Message):
    if not is_manager(message):
        return
    await message.answer(
        "أهلاً يا مدير 👋\n"
        "بوت فواتير بيت المارسليا جاهز لاستقبال طلباتك."
    )


@dp.message(F.text)
async def message_handler(message: Message):
    if not is_manager(message):
        return

    simple_reply = handle_simple_message(message.text)
    if simple_reply:
        await message.answer(simple_reply)
        return

    request_id = await create_request(
        user_id=message.from_user.id,
        request_text=message.text,
    )
    processing = await message.answer("جاري فهم طلبك يا مدير، لحظات... ⏳")

    try:
        parsed = local_parse_invoice_message(message.text)
        if parsed is not None:
            processing_source = "محلي"
            logging.info("Request %s parsed locally. AI not used.", request_id)
        else:
            processing_source = "AI"
            logging.info("Request %s sent to AI parser.", request_id)
            parsed = await parse_invoice_message(message.text)

        extracted_data = parsed.model_dump()
        await update_request(
            request_id=request_id,
            status="parsed",
            extracted_data=json.dumps(extracted_data, ensure_ascii=False),
        )

        clarification_message = verify_extraction_completeness(message.text, parsed)
        if clarification_message:
            await update_request(request_id=request_id, status="needs_clarification")
            await processing.edit_text(
                "محتاج توضيح منك يا مدير:\n\n" + clarification_message
            )
            return

        await processing.edit_text(
            "تم فهم طلبك بنجاح 👍\n"
            f"طريقة الفهم: {processing_source}\n"
            "جاري جلب كتالوج المنتجات ومطابقة الأصناف..."
        )

        products = await get_product_catalog()
        if not products:
            await processing.edit_text("لم أجد منتجات في كتالوج Invoice Ninja.")
            await update_request(request_id, status="empty_catalog")
            return

        pending_product_choices[request_id] = {
            "parsed": parsed,
            "products": products,
            "matches": {},
            "waiting_item_index": None,
            "waiting_candidates": [],
            "waiting_allow_free_form": False,
        }

        await continue_product_matching(message, request_id)

    except Exception:
        logging.exception("Failed to process invoice request %s", request_id)
        await update_request(request_id, status="error")
        await processing.edit_text(
            "حدثت مشكلة أثناء معالجة الطلب يا مدير.\n"
            "لم يتم إنشاء أي فاتورة.\n"
            "راجع سجل البوت لمعرفة التفاصيل."
        )


@dp.callback_query(F.data.startswith("pm:"))
async def product_choice_handler(callback: CallbackQuery):
    if callback.from_user.id != MANAGER_TELEGRAM_ID:
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    try:
        _, request_id_text, item_index_text, candidate_index_text = callback.data.split(":")
        request_id = int(request_id_text)
        item_index = int(item_index_text)
        candidate_index = int(candidate_index_text)
    except (ValueError, AttributeError):
        await callback.answer("بيانات الاختيار غير صحيحة.", show_alert=True)
        return

    state = pending_product_choices.get(request_id)
    if not state:
        await callback.answer("انتهت صلاحية الطلب. أرسل الطلب مرة أخرى.", show_alert=True)
        return

    if state.get("waiting_item_index") != item_index:
        await callback.answer("هذا الاختيار لم يعد صالحاً.", show_alert=True)
        return

    candidates = state.get("waiting_candidates", [])
    if candidate_index < 0 or candidate_index >= len(candidates):
        await callback.answer("الخيار غير موجود.", show_alert=True)
        return

    state["matches"][item_index] = {
        "status": "matched",
        "product": candidates[candidate_index],
    }
    state["waiting_item_index"] = None
    state["waiting_candidates"] = []
    state["waiting_allow_free_form"] = False

    await callback.answer("تم اختيار المنتج.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await continue_product_matching(callback.message, request_id)


@dp.callback_query(F.data.startswith("pf:"))
async def free_form_choice_handler(callback: CallbackQuery):
    if callback.from_user.id != MANAGER_TELEGRAM_ID:
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    try:
        _, request_id_text, item_index_text = callback.data.split(":")
        request_id = int(request_id_text)
        item_index = int(item_index_text)
    except (ValueError, AttributeError):
        await callback.answer("بيانات الاختيار غير صحيحة.", show_alert=True)
        return

    state = pending_product_choices.get(request_id)
    if not state:
        await callback.answer("انتهت صلاحية الطلب. أرسل الطلب مرة أخرى.", show_alert=True)
        return

    if state.get("waiting_item_index") != item_index:
        await callback.answer("هذا الاختيار لم يعد صالحاً.", show_alert=True)
        return

    parsed = state["parsed"]
    if item_index < 0 or item_index >= len(parsed.items):
        await callback.answer("الصنف غير موجود.", show_alert=True)
        return

    item = parsed.items[item_index]
    if item.custom_price is None:
        await callback.answer(
            "لا يمكن إنشاء بند حر دون سعر وحدة يحدده المدير.",
            show_alert=True,
        )
        return

    state["matches"][item_index] = {
        "status": "free_form",
        "product": None,
    }
    state["waiting_item_index"] = None
    state["waiting_candidates"] = []
    state["waiting_allow_free_form"] = False

    await callback.answer("تم اعتماد البند الحر.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await continue_product_matching(callback.message, request_id)


@dp.callback_query(F.data.startswith("ccreate:"))
async def client_create_handler(callback: CallbackQuery):
    if callback.from_user.id != MANAGER_TELEGRAM_ID:
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    try:
        request_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("بيانات الطلب غير صحيحة.", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer("جاري إنشاء أو تحديد العميل...")
    await create_client_for_request(callback.message, request_id)


@dp.callback_query(F.data.startswith("ccreateforce:"))
async def client_force_create_handler(callback: CallbackQuery):
    if callback.from_user.id != MANAGER_TELEGRAM_ID:
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    try:
        request_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("بيانات الطلب غير صحيحة.", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer("تم تأكيد إنشاء عميل جديد رغم التشابه.")
    await create_client_for_request(
        callback.message, request_id, force_new=True,
    )


@dp.callback_query(F.data.startswith("cpick:"))
async def client_pick_handler(callback: CallbackQuery):
    if callback.from_user.id != MANAGER_TELEGRAM_ID:
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    try:
        request_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("بيانات الطلب غير صحيحة.", show_alert=True)
        return

    record = await get_request(request_id)
    if not record:
        await callback.answer("الطلب غير موجود.", show_alert=True)
        return

    try:
        prepared_data = json.loads(record.get("prepared_data") or "{}")
    except (TypeError, ValueError):
        prepared_data = {}

    client_name = (
        prepared_data.get("client_extracted_name")
        or prepared_data.get("client_name")
        or ""
    ).strip()
    if not client_name:
        await callback.answer("اسم العميل غير محدد.", show_alert=True)
        return

    await callback.answer("جاري البحث عن العملاء...")

    api = InvoiceNinjaAPI()
    try:
        try:
            clients = await api.search_clients(client_name)
            resolution = match_clients(client_name, clients)
        except Exception:
            logging.exception(
                "Failed to search candidate clients for request %s",
                request_id,
            )
            await callback.message.answer(
                "تعذر البحث عن العملاء. لم يتم إنشاء أي فاتورة."
            )
            return
    finally:
        await api.close()

    await show_client_candidates(
        callback.message,
        request_id,
        resolution.candidates,
    )


@dp.callback_query(F.data.startswith("csel:"))
async def client_select_handler(callback: CallbackQuery):
    if callback.from_user.id != MANAGER_TELEGRAM_ID:
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    try:
        _, request_id_text, client_id = callback.data.split(":", 2)
        request_id = int(request_id_text)
    except (ValueError, IndexError):
        await callback.answer("بيانات الاختيار غير صحيحة.", show_alert=True)
        return

    if not client_id:
        await callback.answer("العميل غير صالح.", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer("جاري اعتماد العميل...")
    await select_existing_client(callback.message, request_id, client_id)


# --------------------------------------------------
# Approval, invoice creation, and PDF delivery
# --------------------------------------------------

def invoice_number_for_request(request_id: int) -> str:
    return f"MRS-BOT-{request_id:06d}"


async def find_invoice_by_number(api: InvoiceNinjaAPI, invoice_number: str):
    invoices = await api.search_invoices(invoice_number)
    for invoice in invoices:
        if str(invoice.get("number")) == invoice_number:
            return invoice
    return None


async def send_invoice_pdf(request_id: int, invoice_id: str,
                           invoice_number: str):
    """Download and send the PDF to the manager only. Never to the client."""
    api = InvoiceNinjaAPI()
    try:
        pdf_bytes = await api.download_invoice_pdf(invoice_id)
        if not pdf_bytes.startswith(b"%PDF"):
            raise ValueError("Downloaded file is not a PDF.")
    except Exception:
        logging.exception("Failed to download PDF for invoice %s", invoice_id)
        await set_request_status(request_id, "pdf_failed")
        await bot.send_message(
            MANAGER_TELEGRAM_ID,
            f"تم إنشاء الفاتورة {invoice_number}، لكن تعذر تنزيل ملف PDF.\n"
            "اضغط زر الاعتماد مرة أخرى لإعادة إرسال PDF فقط. "
            "لن يتم إنشاء فاتورة جديدة.",
        )
        return
    finally:
        await api.close()

    try:
        await bot.send_document(
            chat_id=MANAGER_TELEGRAM_ID,
            document=BufferedInputFile(
                pdf_bytes,
                filename=f"{invoice_number}.pdf",
            ),
            caption=f"فاتورة {invoice_number} (Draft)",
        )
        await set_request_status(request_id, "pdf_sent")
    except Exception:
        logging.exception("Failed to send PDF for invoice %s", invoice_id)
        await set_request_status(request_id, "pdf_failed")
        await bot.send_message(
            MANAGER_TELEGRAM_ID,
            f"تم إنشاء الفاتورة {invoice_number}، لكن تعذر إرسال PDF.\n"
            "اضغط زر الاعتماد مرة أخرى لإعادة إرسال PDF فقط. "
            "لن يتم إنشاء فاتورة جديدة.",
        )


async def notify_invoice_created(request_id: int, invoice_id: str,
                                 invoice_number: str):
    await set_invoice_created(request_id, invoice_id, invoice_number)
    await send_invoice_pdf(request_id, invoice_id, invoice_number)


async def recover_invoice_by_number(request_id: int, invoice_number: str):
    """
    Check whether a previous uncertain POST already created the invoice.
    Never sends a new POST from this recovery path.
    """
    api = InvoiceNinjaAPI()
    try:
        existing = None
        for attempt in range(3):
            existing = await find_invoice_by_number(api, invoice_number)
            if existing:
                break
            await asyncio.sleep(2)
    except Exception:
        logging.exception(
            "Could not verify invoice %s after creation error",
            invoice_number,
        )
        await set_request_status(request_id, "create_unknown")
        await bot.send_message(
            MANAGER_TELEGRAM_ID,
            f"لم أستطع تأكيد نتيجة إنشاء الفاتورة {invoice_number}.\n"
            "لا تضغط زر الاعتماد مرة أخرى قبل مراجعة Invoice Ninja، "
            "حفظاً لمنع تكرار الفاتورة.",
        )
        return
    finally:
        await api.close()

    if not existing:
        await set_request_status(request_id, "create_failed")
        await bot.send_message(
            MANAGER_TELEGRAM_ID,
            f"لم يتم العثور على فاتورة {invoice_number} بعد المحاولة.\n"
            "لم يتم إنشاء فاتورة جديدة تلقائياً. أعد إرسال الطلب إذا رغبت.",
        )
        return

    await notify_invoice_created(
        request_id,
        existing.get("id"),
        existing.get("number") or invoice_number,
    )


async def create_invoice_from_request(request_id: int):
    record = await get_request(request_id)
    if not record:
        return

    if record.get("invoice_id"):
        await send_invoice_pdf(
            request_id,
            record["invoice_id"],
            record.get("invoice_number") or invoice_number_for_request(request_id),
        )
        return

    try:
        prepared_data = json.loads(record.get("prepared_data") or "{}")
        client_id = prepared_data.get("client_id") or record.get("client_id")
        expected_client_name = prepared_data.get("client_name")
        prepared = {
            "items": prepared_data["items"],
            "subtotal": prepared_data.get("subtotal", 0),
        }
    except (KeyError, TypeError, ValueError):
        logging.exception("Invalid prepared data for request %s", request_id)
        await set_request_status(request_id, "create_failed")
        await bot.send_message(
            MANAGER_TELEGRAM_ID,
            "بيانات الفاتورة المحفوظة غير مكتملة. لم يتم إنشاء أي فاتورة.",
        )
        return

    if not client_id or not expected_client_name:
        logging.error("Request %s has no confirmed client", request_id)
        await set_request_status(request_id, "create_failed")
        await bot.send_message(
            MANAGER_TELEGRAM_ID,
            "لا يوجد عميل مؤكد لهذا الطلب. لم يتم إنشاء أي فاتورة.",
        )
        return

    if record.get("client_id") and record["client_id"] != client_id:
        logging.error(
            "Client id mismatch for request %s: %s != %s",
            request_id,
            record.get("client_id"),
            client_id,
        )
        await set_request_status(request_id, "create_failed")
        await bot.send_message(
            MANAGER_TELEGRAM_ID,
            "بيانات العميل غير متطابقة. لم يتم إنشاء أي فاتورة.",
        )
        return

    invoice_number = await get_or_set_invoice_number(
        request_id,
        invoice_number_for_request(request_id),
    )
    payload = build_invoice_payload(
        client_id=client_id,
        prepared=prepared,
        invoice_number=invoice_number,
    )

    api = InvoiceNinjaAPI()
    try:
        try:
            client_result = await api.get_client(client_id)
            live_client = client_result.get("data", client_result)
        except Exception:
            logging.exception(
                "Failed to verify client %s before invoice for request %s",
                client_id,
                request_id,
            )
            await set_request_status(request_id, "create_failed")
            await bot.send_message(
                MANAGER_TELEGRAM_ID,
                "تعذر التحقق من العميل قبل إنشاء الفاتورة.\n"
                "لم يتم إنشاء أي فاتورة.",
            )
            return

        if (
            not isinstance(live_client, dict)
            or not live_client.get("id")
            or live_client.get("is_deleted")
            or not client_name_matches(expected_client_name, live_client)
        ):
            logging.error(
                "Client verification failed for request %s (client %s)",
                request_id,
                client_id,
            )
            await set_request_status(request_id, "create_failed")
            await bot.send_message(
                MANAGER_TELEGRAM_ID,
                "العميل المحفوظ لم يعد مطابقاً أو أصبح محذوفاً.\n"
                "لم يتم إنشاء أي فاتورة.",
            )
            return

        result = await api.create_invoice(payload)
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        logging.exception(
            "Invoice creation failed for request %s with HTTP %s",
            request_id,
            status_code,
        )
        if status_code < 500:
            await set_request_status(request_id, "create_failed")
            await bot.send_message(
                MANAGER_TELEGRAM_ID,
                "تعذر إنشاء الفاتورة بسبب بيانات مرفوضة من Invoice Ninja.\n"
                "لم يتم إنشاء فاتورة. راجع البيانات ثم أرسل الطلب من جديد.",
            )
        else:
            await recover_invoice_by_number(request_id, invoice_number)
        return
    except Exception:
        logging.exception(
            "Invoice creation result is uncertain for request %s",
            request_id,
        )
        await recover_invoice_by_number(request_id, invoice_number)
        return
    finally:
        await api.close()

    data = result.get("data", result) if isinstance(result, dict) else {}
    invoice_id = data.get("id") if isinstance(data, dict) else None
    if not invoice_id:
        await recover_invoice_by_number(request_id, invoice_number)
        return

    await notify_invoice_created(
        request_id,
        invoice_id,
        data.get("number") or invoice_number,
    )


@dp.callback_query(F.data.startswith("approve:"))
async def approve_handler(callback: CallbackQuery):
    if callback.from_user.id != MANAGER_TELEGRAM_ID:
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    try:
        request_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("بيانات الاعتماد غير صحيحة.", show_alert=True)
        return

    record = await get_request(request_id)
    if not record:
        await callback.answer("الطلب غير موجود.", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if record.get("invoice_id"):
        await callback.answer("الفاتورة موجودة مسبقاً. جاري إعادة إرسال PDF.")
        await send_invoice_pdf(
            request_id,
            record["invoice_id"],
            record.get("invoice_number") or invoice_number_for_request(request_id),
        )
        return

    if record.get("status") in ("creating", "create_unknown"):
        await callback.answer("جاري التحقق من نتيجة الإنشاء السابقة.")
        await recover_invoice_by_number(
            request_id,
            record.get("invoice_number") or invoice_number_for_request(request_id),
        )
        return

    if record.get("status") != "awaiting_approval":
        await callback.answer(
            "لا يمكن اعتماد هذا الطلب الآن.",
            show_alert=True,
        )
        return

    claimed = await claim_approval(request_id)
    if not claimed:
        current = await get_request(request_id)
        if current and current.get("invoice_id"):
            await callback.answer("الفاتورة موجودة مسبقاً. جاري إعادة إرسال PDF.")
            await send_invoice_pdf(
                request_id,
                current["invoice_id"],
                current.get("invoice_number")
                or invoice_number_for_request(request_id),
            )
        elif current and current.get("status") in ("creating", "create_unknown"):
            await callback.answer("جاري التحقق من نتيجة الإنشاء السابقة.")
            await recover_invoice_by_number(
                request_id,
                current.get("invoice_number")
                or invoice_number_for_request(request_id),
            )
        else:
            await callback.answer(
                "تمت معالجة هذا الطلب بالفعل.",
                show_alert=True,
            )
        return

    await callback.answer("تم الاعتماد. جاري إنشاء الفاتورة...")
    await create_invoice_from_request(request_id)


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_handler(callback: CallbackQuery):
    if callback.from_user.id != MANAGER_TELEGRAM_ID:
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    try:
        request_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("بيانات الإلغاء غير صحيحة.", show_alert=True)
        return

    record = await get_request(request_id)
    if not record or record.get("status") not in (
        "awaiting_approval",
        "needs_client_choice",
    ):
        await callback.answer("لا يمكن إلغاء هذا الطلب الآن.", show_alert=True)
        return

    await set_request_status(request_id, "cancelled")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_text(
            (callback.message.text or "")
            + "\n\nتم إلغاء الطلب. لم يتم إنشاء أي فاتورة."
        )
    except Exception:
        pass
    await callback.answer("تم إلغاء الطلب. لم يتم إنشاء أي فاتورة.")


# --------------------------------------------------
# Startup
# --------------------------------------------------

async def main():
    logging.info("Initializing database...")
    await init_db()
    logging.info("Database initialized successfully.")
    logging.info("Starting Marseille Invoice Bot...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

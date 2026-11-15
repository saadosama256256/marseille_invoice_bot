
import re
from difflib import SequenceMatcher


# الكلمات الوصفية التي تميز نوع/مادة المنتج ويجب عدم الخلط بينها
VARIANT_CONFLICT_GROUPS = {
    "pvc": ("pvc", "بي في سي"),
    "ceramic": ("فخارية", "فخاري", "فخار"),
    "metallic": ("معدنية", "معدني", "معدن"),
    "italian": ("ايطالية", "ايطالي"),
    "flat": ("فلات",),
}

UNIT_GROUPS = {
    "sqm": ("متر مربع",),
    "lm": ("متر طولي",),
    "kaf": ("كف",),
    "piece": ("قطعة", "قطعه"),
    "set": ("طقم",),
    "carton": ("كرتونة", "كرتونه"),
    "bag": ("جوال",),
    "pack": ("باكو",),
}


def normalize_arabic(text: str) -> str:
    """توحيد الكتابة العربية مع الحفاظ على هوية الصنف."""

    text = str(text or "").strip().lower()

    text = re.sub(r"[\u064B-\u065F\u0670ـ]", "", text)

    replacements = {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ى": "ي",
        "ة": "ه",
        "ؤ": "و",
        "ئ": "ي",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    arabic_digits = "٠١٢٣٤٥٦٧٨٩"
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"

    for index, digit in enumerate(arabic_digits):
        text = text.replace(digit, str(index))

    for index, digit in enumerate(persian_digits):
        text = text.replace(digit, str(index))

    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def canonicalize_token(token: str) -> str:
    """توحيد الاختلافات الإملائية الشائعة في كلمات الكتالوج."""
    if token in ("مارسليا", "مارسيليا"):
        return "مارسيليا"
    if token in ("ايطاليه", "ايطالي"):
        return "ايطالي"
    if token in ("معدنيه", "معدني"):
        return "معدني"
    if token in ("فخاريه", "فخاري"):
        return "فخاري"
    if token in ("اسطوانه", "اسطوانة"):
        return "اسطوانه"
    if token in ("قطعه", "قطعة"):
        return "قطعه"
    if token in ("كرتونه", "كرتونة"):
        return "كرتونه"
    if token == "مسمسار":
        return "مسمار"
    return token


def canonical_tokens(text: str) -> list[str]:
    norm = normalize_arabic(text)
    if not norm:
        return []
    return [canonicalize_token(tok) for tok in norm.split()]


def detect_variant_flags(norm_text: str) -> set[str]:
    tokens = set(norm_text.split())
    flags = set()
    for group, keywords in VARIANT_CONFLICT_GROUPS.items():
        for kw in keywords:
            kw_norm = normalize_arabic(kw)
            if " " in kw_norm:
                if kw_norm in norm_text:
                    flags.add(group)
                    break
            elif kw_norm in tokens:
                flags.add(group)
                break
    return flags


def detect_unit_flags(norm_text: str) -> set[str]:
    tokens = set(norm_text.split())
    flags = set()
    for group, keywords in UNIT_GROUPS.items():
        for kw in keywords:
            kw_norm = normalize_arabic(kw)
            if " " in kw_norm:
                if kw_norm in norm_text:
                    flags.add(group)
                    break
            elif kw_norm in tokens:
                flags.add(group)
                break
    return flags


def has_semantic_conflict(query_norm: str, product_norm: str) -> bool:
    """رفض المطابقة عند وجود تعارض دلالي في المادة أو النوع أو الوحدة."""
    q_units = detect_unit_flags(query_norm)
    p_units = detect_unit_flags(product_norm)
    if q_units and p_units and q_units != p_units:
        return True

    # كف أسد لا يطابق منتجات ليست من نوع كف
    if "kaf" in q_units and "kaf" not in p_units:
        return True
    if "kaf" in p_units and "kaf" not in q_units:
        return True

    q_variants = detect_variant_flags(query_norm)
    p_variants = detect_variant_flags(product_norm)

    # PVC، فلات، فخاري، معدني، إيطالي: وجود أي منها في طرف دون الآخر يمثل تعارضاً
    for group in ("pvc", "flat", "ceramic", "metallic", "italian"):
        if (group in q_variants) != (group in p_variants):
            return True

    return False


def get_product_names(product: dict) -> list[str]:
    """استخراج الأسماء والأوصاف المتاحة من سجل المنتج."""
    fields = ("product_key", "name", "notes", "description")

    names = []

    for field in fields:
        value = product.get(field)

        if isinstance(value, str) and value.strip():
            normalized = normalize_arabic(value)

            if normalized and normalized not in names:
                names.append(normalized)

    return names


def similarity(a: str, b: str) -> float:
    """مقارنة اسم الصنف كاملاً مع إعطاء أهمية للكلمات المشتركة."""

    a_norm = normalize_arabic(a)
    b_norm = normalize_arabic(b)

    if not a_norm or not b_norm:
        return 0.0

    if has_semantic_conflict(a_norm, b_norm):
        return 0.0

    a_tokens = canonical_tokens(a_norm)
    b_tokens = canonical_tokens(b_norm)

    a_set = set(a_tokens)
    b_set = set(b_tokens)

    if not a_set or not b_set:
        return 0.0

    if a_set == b_set:
        return 1.0

    token_score = len(a_set & b_set) / len(a_set | b_set)
    sequence_score = SequenceMatcher(None, " ".join(a_tokens), " ".join(b_tokens)).ratio()

    # إذا كانت كل كلمات الاستعلام موجودة في وصف المنتج، فهذا دليل قوي
    # (مثال: "متر طولي معدني" مقابل "مارسليا معدنية / متر طولي").
    query_coverage = len(a_set & b_set) / len(a_set)

    return max(sequence_score, token_score, query_coverage * 0.95)


def match_product(search_name: str, products: list) -> dict:
    """
    المطابقة الدقيقة أو المتطابقة في الكلمات المفتاحية بدون تعارض تعتمد تلقائياً.
    المطابقة التقريبية القوية جداً والواضحة تعتمد تلقائياً، وإلا تُعرض اقتراحات صالحة فقط.
    """
    search = normalize_arabic(search_name)

    if not search:
        return {
            "status": "not_found",
            "product": None,
            "candidates": [],
            "candidate_scores": [],
            "reason": "empty_query",
        }

    search_token_set = set(canonical_tokens(search))

    # أولاً: البحث عن تطابق نصي أو تطابق كامل في الكلمات (بغض النظر عن الترتيب)
    exact_matches = []
    for product in products:
        names = get_product_names(product)
        for name in names:
            if has_semantic_conflict(search, name):
                continue
            if search == name or (search_token_set and set(canonical_tokens(name)) == search_token_set):
                exact_matches.append(product)
                break

    if len(exact_matches) == 1:
        return {
            "status": "matched",
            "product": exact_matches[0],
            "candidates": [],
            "candidate_scores": [],
            "reason": "exact_or_token_match",
        }

    if len(exact_matches) > 1:
        return {
            "status": "ambiguous",
            "product": None,
            "candidates": exact_matches[:5],
            "candidate_scores": [1.0] * min(len(exact_matches), 5),
            "reason": "multiple_exact_matches",
        }

    # ثانياً: ترتيب المنتجات المتوافقة دلالياً فقط
    scored = []
    for product in products:
        names = get_product_names(product)
        if not names:
            continue

        best_score = max(similarity(search, name) for name in names)
        if best_score > 0:
            scored.append({
                "product": product,
                "score": round(best_score, 3),
            })

    scored.sort(key=lambda item: item["score"], reverse=True)

    # اعتماد تلقائي فقط إذا كان التطابق قوياً جداً وفارق الثقة واضحاً
    if scored:
        top = scored[0]
        second_score = scored[1]["score"] if len(scored) > 1 else 0.0
        if top["score"] >= 0.85 and (top["score"] - second_score) >= 0.10:
            return {
                "status": "matched",
                "product": top["product"],
                "candidates": [],
                "candidate_scores": [],
                "reason": "strong_unambiguous_match",
            }

    # تجاهل الاقتراحات الضعيفة أو غير ذات الصلة
    suggestions = [
        item for item in scored
        if item["score"] >= 0.55
    ][:5]

    if not suggestions:
        return {
            "status": "not_found",
            "product": None,
            "candidates": [],
            "candidate_scores": [],
            "reason": "no_compatible_match",
        }

    return {
        "status": "ambiguous",
        "product": None,
        "candidates": [item["product"] for item in suggestions],
        "candidate_scores": [item["score"] for item in suggestions],
        "reason": "multiple_compatible_candidates",
    }

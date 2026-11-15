
from decimal import Decimal, InvalidOperation


class InvoiceValidationError(Exception):
    pass


def to_decimal(value, field_name):
    if value is None or isinstance(value, bool):
        raise InvoiceValidationError(
            f"قيمة {field_name} غير موجودة."
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise InvoiceValidationError(
            f"قيمة {field_name} غير صحيحة."
        )

    if not number.is_finite():
        raise InvoiceValidationError(
            f"قيمة {field_name} غير صحيحة."
        )

    return number


def prepare_invoice_items(parsed_items, matched_products):
    """
    Prepare invoice lines after product matching.

    Supports both:
    - Catalog-linked lines (match["status"] == "matched" with product dict)
    - Free-form lines (no catalog product required when item.name and item.custom_price are provided)

    The manager's custom unit price always takes priority over the catalog price.
    """

    if len(parsed_items) != len(matched_products):
        raise InvoiceValidationError(
            "عدد الأصناف لا يتطابق مع نتائج المطابقة."
        )

    invoice_items = []
    subtotal = Decimal("0")

    for item, match in zip(parsed_items, matched_products):
        item_name = (item.name or "").strip()
        if not item_name:
            raise InvoiceValidationError("اسم الصنف غير موجود.")

        status = (match or {}).get("status")
        product = (match or {}).get("product")
        custom_price = item.custom_price

        if status == "matched":
            if not product:
                raise InvoiceValidationError(
                    f"بيانات المنتج غير مكتملة: {item_name}"
                )
            is_free_form = False
        elif custom_price is not None:
            # بند حر معتمد بوصف المدير وسعره الخاص دون اشتراط product_id
            product = None
            is_free_form = True
        else:
            raise InvoiceValidationError(
                f"لم يتم التأكد من الصنف أو سعره: {item_name}"
            )

        quantity = to_decimal(item.quantity, "الكمية")
        if quantity <= 0:
            raise InvoiceValidationError(
                f"كمية الصنف {item_name} يجب أن تكون أكبر من صفر."
            )

        if custom_price is not None:
            unit_price = to_decimal(custom_price, "سعر الوحدة")
        else:
            unit_price = to_decimal(product.get("price"), "السعر المسجل")

        if unit_price < 0:
            raise InvoiceValidationError(
                f"سعر الصنف {item_name} لا يمكن أن يكون سالبًا."
            )

        line_total = quantity * unit_price
        subtotal += line_total

        description = item_name
        if item.unit and item.unit.strip() and item.unit.strip() not in item_name:
            description = f"{item_name} - {item.unit.strip()}"

        invoice_items.append({
            "product_id": product.get("id") if product else None,
            "product_key": (
                (product.get("product_key") or product.get("name") or description)
                if product
                else item_name
            ),
            "name": item_name,
            "description": description,
            "unit": item.unit,
            "quantity": float(quantity),
            "unit_price": float(unit_price),
            "line_total": float(line_total),
            "is_free_form": is_free_form,
        })

    return {
        "items": invoice_items,
        "subtotal": float(subtotal),
    }


def to_invoice_ninja_line_item(line: dict) -> dict:
    """
    Map an internal prepared invoice line to an Invoice Ninja v5 line_items payload.
    Invoice Ninja v5 accepts product_key, notes, cost, quantity, and type_id
    without requiring a catalog product_id.
    """
    return {
        "product_key": (
            line.get("product_key")
            or line.get("name")
            or line["description"]
        ),
        "notes": line["description"],
        "cost": float(to_decimal(line["unit_price"], "سعر الوحدة")),
        "quantity": float(to_decimal(line["quantity"], "الكمية")),
        "type_id": "2" if line.get("is_free_form") else "1",
    }


def to_invoice_ninja_line_items(prepared: dict) -> list[dict]:
    """Map all prepared invoice lines to Invoice Ninja v5 API line_items."""
    return [to_invoice_ninja_line_item(line) for line in prepared.get("items", [])]


def build_invoice_payload(
    client_id: str,
    prepared: dict,
    invoice_number: str,
    status_id: int = 1,
) -> dict:
    """Build a draft Invoice Ninja invoice payload from prepared lines."""
    return {
        "client_id": client_id,
        "status_id": status_id,
        "number": invoice_number,
        "line_items": to_invoice_ninja_line_items(prepared),
    }

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.identities import contact_label

MAX_ANALYTICS_DAYS = 366


def local_period(date_from: date, date_to: date, timezone: str) -> tuple[datetime, datetime]:
    """Convert an inclusive local-date range to a half-open UTC interval."""
    if date_to < date_from:
        raise ValueError("Дата завершення не може бути раніше дати початку")
    if (date_to - date_from).days >= MAX_ANALYTICS_DAYS:
        raise ValueError("Період не може перевищувати 366 днів")
    zone = ZoneInfo(timezone)
    start = datetime.combine(date_from, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start, end


async def build_analytics(
    session: AsyncSession,
    *,
    connection: models.Connection,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    start, end = local_period(date_from, date_to, connection.timezone)

    activities = list(
        await session.scalars(
            select(models.ContactActivity)
            .where(models.ContactActivity.connection_id == connection.id)
            .order_by(models.ContactActivity.contact_id)
        )
    )
    contacts: dict[int, dict[str, Any]] = {
        row.contact_id: _empty_contact(
            row.contact_id,
            contact_name=row.contact_name,
            contact_username=row.contact_username,
        )
        for row in activities
    }

    message_rows = (
        await session.execute(
            select(
                models.MessageLog.contact_id,
                models.MessageLog.direction,
                models.MessageLog.category,
                models.MessageLog.action,
                func.count(models.MessageLog.id),
            )
            .where(
                models.MessageLog.connection_id == connection.id,
                models.MessageLog.occurred_at >= start,
                models.MessageLog.occurred_at < end,
                models.MessageLog.action != LogAction.CAPTURED.value,
            )
            .group_by(
                models.MessageLog.contact_id,
                models.MessageLog.direction,
                models.MessageLog.category,
                models.MessageLog.action,
            )
        )
    ).all()
    for contact_id, direction, category, action, count in message_rows:
        item = contacts.setdefault(contact_id, _empty_contact(contact_id))
        value = int(count)
        item["messages"] += value
        item["message_directions"][direction] = item["message_directions"].get(direction, 0) + value
        category_key = category or "unknown"
        item["categories"][category_key] = item["categories"].get(category_key, 0) + value
        item["actions"][action] = item["actions"].get(action, 0) + value

    request_rows = (
        await session.execute(
            select(
                models.ContactRequest.contact_id,
                models.ContactRequest.status,
                models.ContactRequest.category,
                models.ContactRequest.currency,
                models.ContactRequest.price_amount,
            ).where(
                models.ContactRequest.connection_id == connection.id,
                models.ContactRequest.occurred_at >= start,
                models.ContactRequest.occurred_at < end,
            )
        )
    ).all()
    for contact_id, request_status, category, currency, price_amount in request_rows:
        item = contacts.setdefault(contact_id, _empty_contact(contact_id))
        item["requests_total"] += 1
        if request_status == "paid":
            item["paid_requests"] += 1
            if currency and price_amount is not None:
                item["paid_amounts"][currency] = _decimal_string(
                    Decimal(item["paid_amounts"].get(currency, "0")) + price_amount
                )
        else:
            item["ordinary_requests"] += 1
        category_key = category or "unknown"
        item["request_categories"][category_key] = (
            item["request_categories"].get(category_key, 0) + 1
        )

    summary_rows = (
        await session.execute(
            select(
                models.SummaryItem.contact_id,
                models.SummaryItem.contact_name,
                models.SummaryItem.contact_username,
                func.sum(models.SummaryItem.questions_asked),
                func.sum(models.SummaryItem.questions_closed),
            )
            .join(models.SummaryRun, models.SummaryRun.id == models.SummaryItem.run_id)
            .where(
                models.SummaryRun.connection_id == connection.id,
                models.SummaryRun.period_start >= start,
                models.SummaryRun.period_end <= end,
            )
            .group_by(
                models.SummaryItem.contact_id,
                models.SummaryItem.contact_name,
                models.SummaryItem.contact_username,
            )
        )
    ).all()
    for contact_id, contact_name, contact_username, asked, closed in summary_rows:
        item = contacts.setdefault(
            contact_id,
            _empty_contact(
                contact_id,
                contact_name=contact_name,
                contact_username=contact_username,
            ),
        )
        if not item["contact_name"] and contact_name:
            item["contact_name"] = contact_name
        if not item["contact_username"] and contact_username:
            item["contact_username"] = contact_username
        item["questions_asked"] += int(asked or 0)
        item["questions_closed"] += int(closed or 0)

    items = sorted(
        contacts.values(),
        key=lambda item: (
            -item["requests_total"],
            -item["messages"],
            (item["contact_name"] or "").casefold(),
            item["contact_id"],
        ),
    )
    totals = _totals(items)
    return {
        "period": {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "timezone": connection.timezone,
            "started_at": start.isoformat(),
            "ended_at": end.isoformat(),
        },
        "totals": totals,
        "items": items,
    }


def render_monthly_pdf(report: dict[str, Any], *, generated_at: datetime | None = None) -> bytes:
    font_name, bold_font_name = _register_fonts()
    generated = (generated_at or datetime.now(UTC)).astimezone(UTC)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title="Місячний звіт Personal Secretary",
        author="Personal Secretary",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "UkrainianTitle",
        parent=styles["Title"],
        fontName=bold_font_name,
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#172033"),
        alignment=TA_LEFT,
        spaceAfter=3 * mm,
    )
    meta_style = ParagraphStyle(
        "UkrainianMeta",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#526075"),
    )
    cell_style = ParagraphStyle(
        "UkrainianCell",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=7.5,
        leading=9.5,
        alignment=TA_LEFT,
    )
    number_style = ParagraphStyle(
        "UkrainianNumber",
        parent=cell_style,
        alignment=TA_RIGHT,
    )
    header_style = ParagraphStyle(
        "UkrainianHeader",
        parent=cell_style,
        fontName=bold_font_name,
        textColor=colors.white,
        alignment=TA_CENTER,
    )

    period = report["period"]
    totals = report["totals"]
    story: list[Any] = [
        Paragraph("Місячний звіт Personal Secretary", title_style),
        Paragraph(
            f"Період: {_format_date(period['date_from'])} — {_format_date(period['date_to'])} "
            f"· часовий пояс: {_escape(period['timezone'])} · сформовано: "
            f"{generated:%d.%m.%Y %H:%M} UTC",
            meta_style,
        ),
        Spacer(1, 4 * mm),
    ]

    overview = Table(
        [
            [
                _p("Контактів", header_style),
                _p("Повідомлень", header_style),
                _p("Звичайних звернень", header_style),
                _p("Платних звернень", header_style),
                _p("Питань задано", header_style),
                _p("Питань закрито", header_style),
            ],
            [
                _p(str(totals["contacts"]), number_style),
                _p(str(totals["messages"]), number_style),
                _p(str(totals["ordinary_requests"]), number_style),
                _p(str(totals["paid_requests"]), number_style),
                _p(str(totals["questions_asked"]), number_style),
                _p(str(totals["questions_closed"]), number_style),
            ],
        ],
        colWidths=[43 * mm] * 6,
    )
    overview.setStyle(_table_style(header_rows=1))
    story.extend([overview, Spacer(1, 6 * mm)])

    rows: list[list[Any]] = [
        [
            _p("Контакт", header_style),
            _p("Повідомлення", header_style),
            _p("Вхідні / вихідні", header_style),
            _p("Звичайні", header_style),
            _p("Платні", header_style),
            _p("Сума", header_style),
            _p("Напрямки", header_style),
            _p("Питання задано / закрито", header_style),
        ]
    ]
    for item in report["items"]:
        contact = contact_label(item["contact_name"], item["contact_username"])
        categories = _format_categories(item["request_categories"] or item["categories"])
        amounts = _format_amounts(item["paid_amounts"])
        rows.append(
            [
                _p(contact, cell_style),
                _p(str(item["messages"]), number_style),
                _p(
                    f"{item['message_directions'].get('in', 0)} / "
                    f"{item['message_directions'].get('out', 0)}",
                    number_style,
                ),
                _p(str(item["ordinary_requests"]), number_style),
                _p(str(item["paid_requests"]), number_style),
                _p(amounts, number_style),
                _p(categories, cell_style),
                _p(
                    f"{item['questions_asked']} / {item['questions_closed']}",
                    number_style,
                ),
            ]
        )
    if len(rows) == 1:
        rows.append([_p("За цей період контактів немає", cell_style)] + [""] * 7)
    details = Table(
        rows,
        repeatRows=1,
        colWidths=[45 * mm, 25 * mm, 30 * mm, 25 * mm, 22 * mm, 32 * mm, 43 * mm, 35 * mm],
        hAlign="LEFT",
    )
    details.setStyle(_table_style(header_rows=1))
    story.append(details)
    doc.build(story)
    return buffer.getvalue()


def _empty_contact(
    contact_id: int,
    *,
    contact_name: str | None = None,
    contact_username: str | None = None,
) -> dict[str, Any]:
    return {
        "contact_id": contact_id,
        "contact_name": contact_name,
        "contact_username": contact_username,
        "messages": 0,
        "message_directions": {},
        "categories": {},
        "actions": {},
        "requests_total": 0,
        "ordinary_requests": 0,
        "paid_requests": 0,
        "request_categories": {},
        "paid_amounts": {},
        "questions_asked": 0,
        "questions_closed": 0,
    }


def _totals(items: list[dict[str, Any]]) -> dict[str, Any]:
    amounts: defaultdict[str, Decimal] = defaultdict(Decimal)
    result: dict[str, Any] = {
        "contacts": len(items),
        "messages": 0,
        "ordinary_requests": 0,
        "paid_requests": 0,
        "questions_asked": 0,
        "questions_closed": 0,
        "paid_amounts": {},
    }
    for item in items:
        for key in (
            "messages",
            "ordinary_requests",
            "paid_requests",
            "questions_asked",
            "questions_closed",
        ):
            result[key] += item[key]
        for currency, amount in item["paid_amounts"].items():
            amounts[currency] += Decimal(amount)
    result["paid_amounts"] = {
        currency: _decimal_string(value) for currency, value in sorted(amounts.items())
    }
    return result


def _decimal_string(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _register_fonts() -> tuple[str, str]:
    regular_candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    )
    bold_candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    )
    regular = next((path for path in regular_candidates if path.exists()), None)
    bold = next((path for path in bold_candidates if path.exists()), None)
    if regular is None or bold is None:
        raise RuntimeError("Не знайдено шрифт із підтримкою української мови")
    if "SecretarySans" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("SecretarySans", regular))
    if "SecretarySansBold" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("SecretarySansBold", bold))
    return "SecretarySans", "SecretarySansBold"


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_escape(text).replace("\n", "<br/>"), style)


def _escape(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_date(value: str) -> str:
    return date.fromisoformat(value).strftime("%d.%m.%Y")


def _format_categories(categories: dict[str, int]) -> str:
    labels = {"general": "загальне", "money": "оплата", "unknown": "без напрямку"}
    if not categories:
        return "—"
    return ", ".join(
        f"{labels.get(key, key)}: {value}" for key, value in sorted(categories.items())
    )


def _format_amounts(amounts: dict[str, str]) -> str:
    if not amounts:
        return "—"
    return ", ".join(f"{amount} {currency}" for currency, amount in sorted(amounts.items()))


def _table_style(*, header_rows: int) -> TableStyle:
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, header_rows - 1), colors.HexColor("#315f91")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#c9d2df")),
            (
                "ROWBACKGROUNDS",
                (0, header_rows),
                (-1, -1),
                [colors.white, colors.HexColor("#f3f6fa")],
            ),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
    )

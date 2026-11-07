"""Согласия, акцепт оферты и возвраты.

Модель продукта: День 1 — бесплатный; дни 2–7 — шесть платных этапов за 990 ₽,
стоимость одного этапа — 990 / 6 = 165 ₽ (закреплена в оферте, п. 4).
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Delivery, LegalAction, Purchase, RefundRequest, User

# Редакция документов (оферта, политика, согласие). Меняй при изменении текстов документов —
# тогда пользователи заново подтвердят новую редакцию; ранее заключённые договоры не меняются.
DOC_VERSION = "2026-11-08"

PAID_STAGES = (2, 3, 4, 5, 6, 7)

PD_CONSENT = "pd_consent"          # редакция 2026-11-07: включала и учёт источника
PD_WITHDRAW = "pd_withdraw"
ANALYTICS_CONSENT = "analytics_consent"    # с 2026-11-08: отдельное согласие только на учёт источника
ANALYTICS_WITHDRAW = "analytics_withdraw"
FREE_ACCEPT = "free_accept"
OFFER_ACCEPT = "offer_accept"


async def record(session: AsyncSession, user: User, action: str, detail: str | None = None) -> None:
    session.add(LegalAction(user_id=user.id, action=action, doc_version=DOC_VERSION, detail=detail))
    await session.flush()


async def _last(session: AsyncSession, user: User, actions: tuple[str, ...]) -> LegalAction | None:
    return await session.scalar(
        select(LegalAction).where(LegalAction.user_id == user.id, LegalAction.action.in_(actions))
        .order_by(LegalAction.id.desc()).limit(1)
    )


async def has_pd_consent(session: AsyncSession, user: User) -> bool:
    """Действующее согласие: последнее из (согласие, отзыв) — согласие."""
    last = await _last(session, user, (PD_CONSENT, PD_WITHDRAW))
    return last is not None and last.action == PD_CONSENT


async def has_analytics_consent(session: AsyncSession, user: User) -> bool:
    """Действующее согласие на учёт источника перехода (ранний pd_consent его включал)."""
    last = await _last(session, user, (PD_CONSENT, ANALYTICS_CONSENT, PD_WITHDRAW, ANALYTICS_WITHDRAW))
    return last is not None and last.action in (PD_CONSENT, ANALYTICS_CONSENT)


async def has_offer_accept(session: AsyncSession, user: User) -> bool:
    """Акцепт текущей редакции оферты перед оплатой."""
    last = await session.scalar(
        select(LegalAction).where(
            LegalAction.user_id == user.id, LegalAction.action == OFFER_ACCEPT,
            LegalAction.doc_version == DOC_VERSION,
        ).limit(1)
    )
    return last is not None


def stage_price(price_total: int) -> int:
    """Стоимость одного платного этапа в тех же единицах, что и цена (копейки/звёзды)."""
    return price_total // len(PAID_STAGES)


async def stages_provided(session: AsyncSession, user: User) -> int:
    """Сколько платных этапов фактически предоставлено полностью: все материалы дня, включая
    аудиопрактику, направлены в чат (запись создаётся только после успешной отправки, а не по
    наступлению календарной даты; этап без аудио не учитывается)."""
    days = await session.scalars(
        select(Delivery.day).where(Delivery.user_id == user.id, Delivery.day.in_(PAID_STAGES),
                                   Delivery.complete.is_(True))
    )
    return len(set(days))


def refund_amount(paid_amount: int, stages: int) -> int:
    """Сумма к возврату: уплаченное минус стоимость фактически предоставленных платных этапов.
    Бесплатный День 1 в расчёт не входит. Иные удержания не применяются."""
    return max(paid_amount - stage_price(paid_amount) * stages, 0)


async def open_request(session: AsyncSession, purchase: Purchase) -> RefundRequest | None:
    return await session.scalar(
        select(RefundRequest).where(RefundRequest.purchase_id == purchase.id,
                                    RefundRequest.status == "registered")
    )


async def register_refund(session: AsyncSession, user: User, purchase: Purchase) -> tuple[RefundRequest, bool]:
    """Регистрирует отказ от договора. Идемпотентно: повторный вызов вернёт то же заявление.
    С момента регистрации выдача следующих этапов прекращается (покупка → refund_pending)."""
    existing = await open_request(session, purchase)
    if existing is not None:
        return existing, False
    stages = await stages_provided(session, user)
    req = RefundRequest(
        user_id=user.id, purchase_id=purchase.id, stages_provided=stages,
        amount_due=refund_amount(purchase.amount, stages), currency=purchase.currency,
    )
    session.add(req)
    purchase.status = "refund_pending"
    await session.flush()
    return req, True


async def complete_refund(session: AsyncSession, req: RefundRequest, final_amount: int | None = None) -> bool:
    """Фиксирует выполненный возврат. Повторное завершение не выполняется (False)."""
    if req.status == "completed":
        return False
    purchase = await session.get(Purchase, req.purchase_id)
    req.status = "completed"
    req.final_amount = req.amount_due if final_amount is None else final_amount
    req.completed_at = datetime.now(timezone.utc)
    if purchase is not None:
        purchase.status = "refunded"
    await session.flush()
    return True


def fmt_amount(amount: int, currency: str) -> str:
    return f"{amount / 100:.0f} ₽" if currency == "RUB" else f"{amount} ⭐"

"""Помощники для тестов."""
from datetime import date, timedelta
from decimal import Decimal

from rest_framework.test import APIClient

from accounts.models import User
from clients.models import Client
from core.models import today
from network.models import CommissionRule, Contract, Franchisee, Package, Product
from network.services import activate_contract, contract_products, preset_parts, price_quote

_counter = {"n": 0}


def uid():
    _counter["n"] += 1
    return _counter["n"]


def make_product(code="market", **kw):
    kw.setdefault("name", f"Продукт {code}")
    kw.setdefault("sphere", f"Сфера {code}")
    kw.setdefault("price_cash", Decimal("35000"))
    kw.setdefault("price_per_sale", Decimal("50000"))
    kw.setdefault("integration_enabled", True)
    return Product.objects.create(code=code, **kw)


def make_package(products, **kw):
    pkg = Package.objects.create(name=kw.pop("name", "Пакет"), price_cash=kw.pop("price_cash", 87000),
                                 price_prepay=kw.pop("price_prepay", 89000),
                                 price_per_sale=kw.pop("price_per_sale", 127000), **kw)
    pkg.products.set(products)
    return pkg


def make_fr(name="OZOK Ош", city="Ош", cities=None, status="franchisee", code=None):
    return Franchisee.objects.create(name=name, city=city, cities=cities if cities is not None else [city],
                                     status=status, partner_code=code or f"P-{uid()}",
                                     funnel_stage="activated" if status == "franchisee" else "new_application")


def make_user(fr=None, role=None, email=None, **kw):
    role = role or ("franchisee" if fr else "super_admin")
    n = uid()
    return User.objects.create_user(email=email or f"u{n}@test.kg", password="pass12345", role=role, franchisee=fr,
                                    first_name="Тест", last_name=f"{role}{n}", **kw)


def api(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


def make_client(fr, name=None):
    return Client.objects.create(franchisee=fr, business_name=name or f"Магазин {uid()}", city=fr.city)


def make_contract(fr, *, product=None, package=None, scheme="cash", fee=None, commission=None, signed=None,
                  activate=True, **kw):
    """Договор. Части оплаты берутся из пресета схемы, если не заданы явно (upfront_amount и т. д.).
    commission != 20 — создаёт правило вознаграждения этого франчайзи (ставку в договоре заменили правила, 5Д)."""
    quote = price_quote(product=product, package=package, scheme=scheme)
    fee_total = fee if fee is not None else quote["list_price"]
    parts = {k: kw.pop(k) for k in ("upfront_amount", "installments_total", "installments_count") if k in kw}
    installments = kw.pop("installments", 3)
    if not parts:
        parts = preset_parts(scheme, fee_total, kw.get("prepay_percent", 40), installments)
    c = Contract.objects.create(
        franchisee=fr, product=product, package=package, payment_scheme=scheme, signed_at=signed or today(),
        list_price=quote["list_price"], fee_total=fee_total, **parts, **kw)
    c.products.set(contract_products(c))
    if commission is not None and commission != 20:
        make_rule(franchisee=fr, product=product, sale_kind="subscription", percent=commission,
                  name=f"{fr.name}: {commission}% с абонплаты")
    return activate_contract(c) if activate else c


def make_rule(**kw):
    kw.setdefault("name", "Правило")
    kw.setdefault("valid_from", date(2020, 1, 1))
    return CommissionRule.objects.create(**kw)

def days_ago(n):
    return today() - timedelta(days=n)


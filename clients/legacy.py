"""Существующие клиенты OZOK (P0+ п.9): абонплата 100% OZOK, без партнёра и без комиссии.

Ввод по одному (create_legacy_client) и массовый импорт по сфере (CSV: template → preview → confirm).
Дубли — по ИНН, телефону, ID клиента в продукте и лицевому счёту Элкюар; действие: объединить или пропустить."""
from django.utils.translation import gettext, gettext_lazy
import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from core import audit
from core.exceptions import BusinessError
from core.models import q2, today
from network.commission import price_on
from network.models import Product, Tariff

from . import subscriptions as sub_svc
from .models import Branch, Client, Subscription

O = Client.Origin

TEMPLATE_HEADER = [gettext_lazy("Название"), gettext_lazy("ИНН"), gettext_lazy("Контакт"), gettext_lazy("Телефон"), gettext_lazy("Город"), gettext_lazy("Адрес"), gettext_lazy("Тариф"), gettext_lazy("Цена в месяц"),
                   "Пользуется с", "Оплачено до", "ID в продукте", "Лицевой счёт Элкюар"]
COLUMNS = {
    "business_name": ("название", "компания", "business_name", "name"),
    "inn": ("инн", "inn"),
    "contact_name": ("контакт", "контактное лицо", "contact_name", "contact"),
    "phone": ("телефон", "phone"),
    "city": ("город", "city"),
    "address": ("адрес", "address"),
    "tariff": ("тариф", "tariff"),
    "monthly_price": ("цена в месяц", "цена", "monthly_price", "price"),
    "started_at": ("пользуется с", "started_at", "с"),
    "paid_until": ("оплачено до", "paid_until", "до"),
    "external_id": ("id в продукте", "external_id", "id"),
    "elqr_account": ("лицевой счёт элкюар", "лицевой счет элкюар", "лицевой счёт", "лицевой счет", "elqr_account"),
}


def norm_phone(value):
    return re.sub(r"\D+", "", value or "")[-9:] if value else ""


def _date(value, field, errors, required=False):
    if value in (None, ""):
        if required:
            errors.append(gettext('Укажите «{field}».').format(field=field))
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    errors.append(gettext('Некорректная дата «{field}»: {s}.').format(field=field, s=s))
    return None


def clean_sphere(raw, *, product=None):
    """Проверка одной сферы клиента. Возвращает (dict | None, [ошибки])."""
    errors = []
    if product is None:
        pid = raw.get("product")
        product = (Product.objects.filter(pk=int(pid)) if str(pid).isdigit()
                   else Product.objects.filter(code=str(pid or ""))).first()
        if product is None:
            return None, [gettext("Сфера (product) не найдена.")]
    if not product.is_active:
        errors.append(gettext("Сфера отключена."))
    tariff = None
    t = raw.get("tariff")
    if t not in (None, ""):
        qs = Tariff.objects.filter(product=product)
        tariff = (qs.filter(pk=int(t)) if str(t).isdigit() else None)
        tariff = (tariff.first() if tariff is not None else None) or qs.filter(code__iexact=str(t).strip()).first() \
            or qs.filter(name__iexact=str(t).strip()).first()
        if tariff is None:
            errors.append(gettext('Тариф «{t}» не найден в сфере «{name}».').format(t=t, name=product.name))
    price = None
    if raw.get("monthly_price") not in (None, ""):
        try:
            price = q2(Decimal(str(raw["monthly_price"]).replace(" ", "").replace(",", ".")))
            if price < 0:
                raise InvalidOperation
        except InvalidOperation:
            errors.append(gettext("Некорректная цена в месяц."))
    if tariff is None and price is None and not errors:
        errors.append(gettext("Укажите тариф или цену в месяц."))
    started = _date(raw.get("started_at"), gettext("Пользуется с"), errors)
    paid_until = _date(raw.get("paid_until"), gettext("Оплачено до"), errors)
    if started and started > today():
        errors.append(gettext("«Пользуется с» не может быть в будущем."))
    if started and paid_until and paid_until < started:
        errors.append(gettext("«Оплачено до» раньше «Пользуется с»."))
    ext = str(raw.get("external_id") or "").strip()
    acc = str(raw.get("elqr_account") or "").strip()
    if errors:
        return None, errors
    return {"product": product, "tariff": tariff, "monthly_price": price, "started_at": started,
            "paid_until": paid_until, "external_id": ext, "elqr_account": acc}, []


def find_duplicates(company, spheres):
    """Совпадения с существующими клиентами: [{client, reasons[], can_merge}]. Партнёрских клиентов не объединяем."""
    found = {}

    def add(client, reason, hard=False):
        d = found.setdefault(client.id, {"client": client, "reasons": [], "hard": False})
        d["reasons"].append(reason)
        d["hard"] = d["hard"] or hard

    inn = (company.get("inn") or "").strip()
    if inn:
        for c in Client.objects.filter(inn=inn):
            add(c, gettext('ИНН {inn}').format(inn=inn))
    phone = norm_phone(company.get("phone"))
    if phone:
        for c in Client.objects.exclude(phone="").only("id", "phone", "origin", "business_name"):
            if norm_phone(c.phone) == phone:
                add(c, gettext('телефон {v}').format(v=company.get('phone')))
    for s in spheres:
        if s["external_id"]:
            for sub in Subscription.objects.filter(product=s["product"], external_id=s["external_id"]) \
                    .select_related("client"):
                add(sub.client, gettext('ID в продукте {v} ({name})').format(v=s['external_id'], name=s['product'].name), hard=True)
        if s["elqr_account"]:
            for b in Branch.objects.filter(elqr_account=s["elqr_account"]).select_related("client"):
                add(b.client, gettext('лицевой счёт Элкюар {v}').format(v=s['elqr_account']), hard=True)
    out = []
    for d in found.values():
        c = d["client"]
        taken = {s["product"].id for s in spheres if c.accounts.filter(product=s["product"])
                 .exclude(status="closed").exists()}
        d["can_merge"] = c.origin != O.PARTNER and not taken and not d["hard"]
        d["merge_blocker"] = (
            gettext("Клиент привлечён партнёром.") if c.origin == O.PARTNER else
            gettext("Подписка по этой сфере уже есть.") if taken else
            gettext("Совпадает ID продукта или лицевой счёт: сфера уже заведена.") if not d["can_merge"] else "")
        out.append(d)
    return out


def _dup_payload(d):
    c = d["client"]
    return {"client": c.id, "business_name": c.business_name, "origin": c.origin, "reasons": d["reasons"],
            "can_merge": d["can_merge"], "merge_blocker": d["merge_blocker"]}


def _add_sphere(client, s, *, branch_address="", user=None):
    """Филиал сферы + подписка (started_at/paid_until, статус считается), без партнёров и без комиссии."""
    branch = Branch.objects.create(
        client=client, name=s["product"].name if client.branches.exists() else gettext("Основной"),
        address=branch_address or client.address, city=client.city, region=client.region, product=s["product"],
        tariff=s["tariff"], launch_date=s["started_at"], elqr_account=s["elqr_account"] or None,
        is_main=not client.branches.exists(), status=Branch.Status.ACTIVE)
    fee = s["monthly_price"]
    if fee is None and s["tariff"] is not None:
        fee = price_on(s["tariff"], today())
    sub = Subscription(client=client, branch=branch, product=s["product"], external_id=s["external_id"],
                       tariff=s["tariff"], monthly_fee=fee, connected_at=s["started_at"] or today(),
                       started_at=s["started_at"], paid_until=s["paid_until"])
    sub.status = sub_svc.status_for(sub)
    sub.save()
    return sub


@transaction.atomic
def _create(company, spheres, user, *, merge_into=None):
    if merge_into is not None:
        client = merge_into
        for k in ("inn", "contact_name", "phone", "city", "address"):
            if not getattr(client, k) and company.get(k):
                setattr(client, k, company[k])
        client.save()
        action = "merged"
    else:
        client = Client.objects.create(
            franchisee=None, origin=O.OZOK_LEGACY, business_name=company["business_name"], inn=company.get("inn", ""),
            contact_name=company.get("contact_name", ""), phone=company.get("phone", ""),
            city=company.get("city", ""), region=company.get("region", ""), address=company.get("address", ""))
        action = "created"
    subs = [_add_sphere(client, s, branch_address=company.get("branch_address", ""), user=user) for s in spheres]
    audit.log_event("create" if action == "created" else "update", client, reason=gettext('Существующий клиент OZOK ({action})').format(action=action),
                    changes={"origin": [None, client.origin], "spheres": [None, [s.product.code for s in subs]]},
                    user=user)
    return client, subs


def _company(data):
    name = (data.get("business_name") or data.get("company") or "").strip()
    if not name:
        raise BusinessError(gettext("Укажите название компании (business_name)."))
    return {k: str(data.get(k) or "").strip() for k in ("inn", "contact_name", "phone", "city", "region", "address",
                                                         "branch_address")} | {"business_name": name}


def create_legacy_client(data, user):
    """POST clients/legacy/: одна компания + список сфер. Дубль → ошибка со списком; merge_into=<id> объединяет."""
    company = _company(data)
    raw_spheres = data.get("spheres")
    if not isinstance(raw_spheres, list) or not raw_spheres:
        raise BusinessError(gettext("Укажите хотя бы одну сферу (spheres)."))
    spheres, errors = [], {}
    for i, raw in enumerate(raw_spheres):
        s, errs = clean_sphere(raw or {})
        if errs:
            errors[f"spheres[{i}]"] = errs
        else:
            spheres.append(s)
    if errors:
        raise BusinessError({"spheres": errors})
    keys = [(s["product"].id, s["external_id"]) for s in spheres if s["external_id"]]
    if len(set(keys)) != len(keys) or len({s["product"].id for s in spheres}) != len(spheres):
        raise BusinessError(gettext("Сфера или ID в продукте повторяются в списке."))
    dups = find_duplicates(company, spheres)
    merge_into = None
    if data.get("merge_into"):
        merge_into = next((d["client"] for d in dups if str(d["client"].id) == str(data["merge_into"])
                           and d["can_merge"]), None)
        if merge_into is None:
            raise BusinessError(gettext("Объединить с этим клиентом нельзя: он не найден среди дублей или не подходит."))
    elif dups:
        raise BusinessError({"duplicates": [_dup_payload(d) for d in dups],
                             "detail": gettext("Найдены дубли: укажите merge_into или проверьте данные.")})
    return _create(company, spheres, user, merge_into=merge_into)


# ---- массовый импорт по сфере ----------------------------------------------------------------------------------

def template_csv(product):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(TEMPLATE_HEADER)
    tariff = product.tariffs.first() if hasattr(product, "tariffs") else None
    w.writerow([gettext("Кафе «Пример»"), "01234567890123", gettext("Иван Иванов"), "0555 123 456", gettext("Бишкек"), gettext("ул. Примерная, 1"),
                tariff.code if tariff else "", "" if tariff else "1500", "01.01.2025", "31.12.2026",
                "ID-123", "" if product.payment_source != "elqr" else "ELQR-0001"])
    return "﻿" + buf.getvalue()


def parse_csv(text):
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise BusinessError(gettext("Файл пустой."))
    delim = ";" if text[:2048].count(";") > text[:2048].count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header = [h.strip().lower() for h in next(reader)]
    idx = {k: header.index(n) for k, names in COLUMNS.items() for n in names if n in header}
    if "business_name" not in idx:
        raise BusinessError(gettext("В файле нет колонки «Название»."))
    rows = []
    for i, rec in enumerate(reader, start=2):
        if not any(c.strip() for c in rec):
            continue
        rows.append({"line": i, **{k: (rec[j].strip() if j < len(rec) else "") for k, j in idx.items()}})
    if not rows:
        raise BusinessError(gettext("В файле нет строк с клиентами."))
    return rows


def _resolve_product(value):
    product = (Product.objects.filter(pk=int(value)) if str(value or "").isdigit()
               else Product.objects.filter(code=str(value or ""))).first()
    if product is None:
        raise BusinessError(gettext("Укажите сферу (product): id или код."))
    return product


def preview(text, product_value):
    """Строки импорта: new | duplicate | error. Для дубля — найденные клиенты и возможность объединения."""
    product = _resolve_product(product_value)
    rows, seen = [], {"inn": set(), "phone": set(), "ext": set(), "acc": set()}
    for r in parse_csv(text):
        out = {"line": r["line"], "business_name": r.get("business_name", ""), "status": "new", "errors": [],
               "duplicates": [], "can_merge": False}
        if not r.get("business_name"):
            out["errors"].append(gettext("Не указано название."))
        sphere, errs = clean_sphere(r, product=product)
        out["errors"] += errs
        if out["errors"]:
            out["status"] = "error"
            rows.append(out)
            continue
        company = {k: r.get(k, "") for k in ("business_name", "inn", "contact_name", "phone", "city", "address")}
        dups = find_duplicates(company, [sphere])
        in_file = []
        for key, val in (("inn", company["inn"]), ("phone", norm_phone(company["phone"])),
                         ("ext", sphere["external_id"]), ("acc", sphere["elqr_account"])):
            if val and val in seen[key]:
                in_file.append(key)
            if val:
                seen[key].add(val)
        if in_file:
            out.update(status="duplicate", can_merge=False,
                       duplicates=[{"client": None, "reasons": [gettext("дубль внутри файла")], "can_merge": False,
                                    "merge_blocker": gettext("Повтор в этом же файле.")}])
        elif dups:
            out.update(status="duplicate", duplicates=[_dup_payload(d) for d in dups],
                       can_merge=any(d["can_merge"] for d in dups))
        rows.append(out)
    summary = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"product": {"id": product.id, "code": product.code, "name": product.name}, "rows": rows,
            "summary": summary}


def confirm(text, product_value, user, *, duplicates="skip", actions=None):
    """Импорт: новые создаются; дубли — по duplicates (merge|skip) и построчным actions {line: merge|skip}; ошибочные
    строки не блокируют остальные и возвращаются с причиной."""
    if duplicates not in ("merge", "skip"):
        raise BusinessError(gettext("duplicates: merge или skip."))
    actions = {str(k): v for k, v in (actions or {}).items()}
    product = _resolve_product(product_value)
    result = []
    for r in parse_csv(text):
        line = r["line"]
        res = {"line": line, "business_name": r.get("business_name", ""), "status": "", "client": None,
               "message": ""}
        result.append(res)
        sphere, errs = clean_sphere(r, product=product)
        if not r.get("business_name"):
            errs = [gettext("Не указано название.")] + errs
        if errs:
            res.update(status="error", message="; ".join(errs))
            continue
        company = {k: r.get(k, "") for k in ("business_name", "inn", "contact_name", "phone", "city", "address")}
        dups = find_duplicates(company, [sphere])
        try:
            if dups:
                action = actions.get(str(line), duplicates)
                target = next((d["client"] for d in dups if d["can_merge"]), None)
                if action != "merge" or target is None:
                    res.update(status="skipped", message=gettext("Дубль: ") + "; ".join(dups[0]["reasons"])
                               + ((". " + dups[0]["merge_blocker"]) if action == "merge" and target is None else ""))
                    continue
                client, _ = _create(company, [sphere], user, merge_into=target)
                res.update(status="merged", client=client.id)
            else:
                client, _ = _create(company, [sphere], user)
                res.update(status="created", client=client.id)
        except BusinessError as e:
            res.update(status="error", message=str(e.detail))
    summary = {}
    for r in result:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    audit.log_event("import", entity="clients.legacy_import", entity_id=product.id, user=user,
                    changes={"summary": [None, summary], "product": [None, product.code]})
    return {"product": product.code, "rows": result, "summary": summary}

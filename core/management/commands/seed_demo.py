"""Демо-данные OZOK Network (раздел 10 ТЗ): `manage.py seed_demo [--reset]`.

Всё создаётся через те же сервисные функции, что и API; абонплаты — через обработчик событий интеграции
(`payment.succeeded`). Даты считаются от сегодняшнего дня, чтобы дашборды выглядели живыми.
"""
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from billing.models import LedgerCategory, PartnerLedgerEntry, RecruiterReward
from billing import recruiters as recruiter_svc
from clients import deals as deal_svc
from clients.models import Deal
from core import change_requests
from core.exceptions import BusinessError
from core.settings_registry import get_decimal, set_setting
from billing.models import Charge, CommissionPayout, FranchiseePayment
from billing.services import finance_map
from clients.models import Client, ClientProductAccount, ClientSale, Implementation
from clients.services import create_client_sale
from core.models import add_months, month_first, q2, today
from integrations.models import IntegrationEvent
from integrations.services import process_event
from leads import services as lead_services
from leads.models import Lead
from network.commission import create_tariff, price_on, set_price
from network.models import (CommissionRule, Contract, Equipment, FranchiseOffer, Franchisee, Package,
                            PartnerStatusHistory, Product, Tariff)
from network.partners import change_partner_field
from network.services import activate_contract, contract_products, preset_parts, price_quote
from planning.models import SalesPlan
from quality import services as quality_services
from quality.models import Audit, Ticket
from tasks import services as task_services
from tasks.models import Task, TaskComment
from training import services as training_services
from training.models import Course, Enrollment

PASSWORD = "demo12345"

PRODUCTS = [
    # code, name, sphere, icon, cash, per_sale, description
    ("nur_retail", "NUR CRM Market", "Магазины", "shopping-cart", 35000, 50000,
     "Учёт товаров, касса и склад для розничных магазинов."),
    ("nur_wholesale", "NUR CRM Опт", "Оптовый бизнес", "truck", 26000, 37000,
     "Оптовые продажи, заказы покупателей и взаиморасчёты."),
    ("kezek", "КЕЗЕК", "Сфера услуг", "calendar-check", 22000, 32000,
     "Онлайн-запись и учёт клиентов для салонов, клиник и сервисов."),
    ("coca", "COCA", "Кафе", "coffee", 44000, 63000,
     "Заказы, кухня и официанты для кафе и ресторанов."),
    ("onai", "OnAI CRM", "Клиентские коммуникации", "briefcase", 40000, 58000,
     "CRM с каналами WhatsApp, Instagram, Facebook и телефонией (установка 20 000 = 12 000 партнёру + 8 000 внедренцу)."),
    ("el_bilim", "El-bilim", "Образование", "graduation-cap", 18000, 26000,
     "Электронный журнал и учёт обучения для школ и учебных центров."),
    ("kechikpe", "Kechikpe", "Отсрочка платежей", "box", 15000, 22000,
     "Сервис отсрочки платежей клиентов для малого бизнеса."),
]
SETUP_PRICE = {"nur_retail": 15000, "nur_wholesale": 12000, "kezek": 10000, "coca": 18000, "onai": 20000,
               "el_bilim": 8000, "kechikpe": 6000}
# тарифы абонплаты: (код, название, текущая цена, прежняя цена); цена менялась ~100 дней назад
TARIFFS = {
    "nur_retail": [("start", "Старт", 1800, 1500), ("biz", "Бизнес", 2500, 2200), ("net", "Сеть", 4000, 4000)],
    "nur_wholesale": [("start", "Старт", 3500, 3500), ("biz", "Бизнес", 4500, 4000)],
    "onai": [("start", "Старт", 3000, 3000), ("pro", "Профи", 5000, 4500)],
    "el_bilim": [("school", "Школа", 2000, 2000)],
    "kechikpe": [("base", "Базовый", 1500, 1500)],
    "kezek": [("start", "Старт", 2000, 2000), ("pro", "Профи", 3000, 2500), ("net", "Сеть", 4500, 4500)],
    "coca": [("base", "Базовый", 2500, 2500), ("biz", "Бизнес", 3500, 3000), ("net", "Сеть", 5500, 5000)],
}
PRICE_CHANGE_DAYS_AGO = 100
EQUIPMENT = [  # название, цена, сфера (None — для всех)
    ("POS-терминал", 28000, None), ("Сканер штрихкодов", 4500, None), ("Чековый принтер", 9500, None),
    ("Денежный ящик", 3800, None), ("Принтер этикеток", 11000, "nur_retail"), ("Планшет для заказов", 16500, None),
    ("Роутер Wi-Fi", 3200, None), ("Весы торговые", 14000, "nur_retail"),
]
GROWTH_SINCE = {"BSK-01": 8, "BSK-02": 6, "KAR-01": 5}  # партнёр -> месяцев назад получил уровень Growth
REGION = {"Бишкек": "Бишкек", "Ош": "Ош", "Джалал-Абад": "Джалал-Абад", "Каракол": "Иссык-Куль",
          "Токмок": "Чуй", "Нарын": "Нарын", "Талас": "Талас", "Баткен": "Баткен", "Кара-Балта": "Чуй"}
STAGE_OF = {"candidate": "new_application", "trainee": "training", "franchisee": "activated",
            "suspended": "activated", "manager": "manager", "closed": "activated"}
RECRUITED = {"BSK-01": "barsbek", "BSK-02": "mirlan", "KAR-01": "barsbek", "OSH-01": "mirlan", "JAL-01": "barsbek",
             "TOK-01": "mirlan", "KBL-01": "barsbek", "OSH-02": "mirlan", "NAR-01": "mirlan"}
# name, code, city, status, этап воронки, дней на этапе, рекрутёр, источник, e-mail пользователя-менеджера
EXTRA_PARTNERS = [
    ("Асылбек Кадыров", "CND-01", "Бишкек", "candidate", "new_application", 1, "barsbek", "instagram", None),
    ("Гульзат Рахимова", "CND-02", "Ош", "candidate", "interview", 4, "mirlan", "referral", None),
    ("Эмиль Турдубеков", "CND-03", "Каракол", "trainee", "training", 9, "barsbek", "site", None),
    ("Айжан Бекова", "CND-04", "Бишкек", "trainee", "exam", 18, "mirlan", "event", None),
    ("Нурбек Манапов", "MGR-01", "Бишкек", "manager", "manager", 5, "barsbek", "instagram", "manager@ozok.kg"),
    ("Салидин Омуров", "MGR-02", "Ош", "manager", "franchise_contract", 3, "mirlan", "referral", "manager2@ozok.kg"),
    ("Рустам Закиров", "CLS-01", "Нарын", "closed", "activated", 40, "mirlan", "site", None),
]

# name, code, legal, city, cities, address, phone, owner, status, curator, joined_months_ago, [employees], slug
FRANCHISEES = [
    ("OZOK Бишкек Центр", "BSK-01", "ОсОО «Озок Бишкек Центр»", "Бишкек", ["Бишкек"], "пр. Чуй 155", "0555 100 101",
     "Эрлан Садыков", "franchisee", "curator", 9, ["Эрлан Садыков:franchisee", "Асель Жунушова:franchisee_staff",
                                                 "Бакыт Мамытов:franchisee_staff"], "bishkek1"),
    ("OZOK Бишкек Юг", "BSK-02", "ОсОО «Юг Систем»", "Бишкек", ["Бишкек", "Кант"], "ул. Ахунбаева 98",
     "0700 200 202", "Мирлан Алиев", "franchisee", "curator", 8, ["Мирлан Алиев:franchisee", "Динара Исакова:franchisee_staff"],
     "bishkek2"),
    ("OZOK Бишкек Восток", "BSK-03", "ОсОО «Восток Софт»", "Бишкек", ["Бишкек"], "ул. Токтогула 12",
     "0772 300 303", "Тимур Касымов", "trainee", "curator", 1, [], "bishkek3"),
    ("OZOK Ош", "OSH-01", "ОсОО «Озок Ош»", "Ош", ["Ош", "Узген"], "ул. Курманжан Датка 41", "0555 400 404",
     "Нурлан Эсенов", "franchisee", "admin", 10, ["Нурлан Эсенов:franchisee", "Айгерим Токтосунова:franchisee_staff",
                                               "Руслан Орозов:franchisee_staff"], "osh"),
    ("OZOK Ош Бизнес", "OSH-02", "ИП Абдыраимов", "Ош", ["Ош"], "ул. Ленина 220", "0770 500 505",
     "Эрмек Абдыраимов", "franchisee", "curator", 7, ["Эрмек Абдыраимов:franchisee", "Чолпон Турдуева:franchisee_staff"],
     "osh2"),
    ("OZOK Джалал-Абад", "JAL-01", "ОсОО «Жалал-Абад Тех»", "Джалал-Абад", ["Джалал-Абад", "Кара-Куль"],
     "ул. Раззакова 17", "0556 600 606", "Айбек Нурматов", "franchisee", "curator", 7,
     ["Айбек Нурматов:franchisee", "Гульнара Сатарова:franchisee_staff"], "jalal"),
    ("OZOK Каракол", "KAR-01", "ОсОО «Ысык-Көл Digital»", "Каракол", ["Каракол", "Балыкчы"], "ул. Токтогула 50",
     "0709 700 707", "Тилек Бейшенов", "franchisee", "admin", 8, ["Тилек Бейшенов:franchisee", "Назгуль Рысбекова:franchisee_staff",
                                                               "Эмил Касымалиев:franchisee_staff"], "karakol"),
    ("OZOK Токмок", "TOK-01", "ИП Джумабаев", "Токмок", ["Токмок"], "ул. Куттубаева 8", "0705 800 808",
     "Бермет Джумабаева", "franchisee", "curator", 6, ["Бермет Джумабаева:franchisee"], "tokmok"),
    ("OZOK Нарын", "NAR-01", "ИП Сыдыков", "Нарын", ["Нарын"], "ул. Ленина 33", "0777 900 909",
     "Улан Сыдыков", "trainee", "curator", 1, [], "naryn"),
    ("OZOK Талас", "TAL-01", "", "Талас", ["Талас"], "", "0550 111 212", "Айзада Темирова", "candidate",
     "curator", 0, [], "talas"),
    ("OZOK Баткен", "BAT-01", "ИП Мамасадыков", "Баткен", ["Баткен", "Исфана"], "ул. Мира 5", "0501 313 414",
     "Рустам Мамасадыков", "suspended", "curator", 7, ["Рустам Мамасадыков:franchisee"], "batken"),
    ("OZOK Кара-Балта", "KBL-01", "ОсОО «Кара-Балта Сервис»", "Кара-Балта", ["Кара-Балта"], "ул. Фрунзе 21",
     "0703 515 616", "Эльнура Осмонова", "franchisee", "curator", 5, ["Эльнура Осмонова:franchisee",
                                                                   "Азамат Токтогулов:franchisee_staff"], "karabalta"),
]

# (франчайзи -> договоры): (продукт-код или "package", схема, месяцев назад). hybrid: 30% сразу, 30% рассрочка
# на 3 месяца, остаток 40% — удержаниями из дохода
CONTRACTS = {
    "BSK-01": [("package", "prepay", 8)],
    "BSK-02": [("package", "cash", 7)],
    "OSH-01": [("package", "per_sale", 8)],
    "OSH-02": [("nur_retail", "cash", 6), ("kezek", "cash", 6), ("coca", "per_sale", 4)],
    "JAL-01": [("nur_retail", "per_sale", 6), ("nur_wholesale", "hybrid", 5)],
    "KAR-01": [("package", "prepay", 7)],
    "TOK-01": [("kezek", "per_sale", 5), ("nur_retail", "hybrid", 4)],
    "KBL-01": [("package", "cash", 4)],
    "BAT-01": [("nur_retail", "cash", 7)],
    "NAR-01": [("nur_retail", "per_sale", 1)],
}
# скидки на франшизу: (франчайзи, продукт) -> (договорная сумма, основание)
FEE_DISCOUNTS = {("BSK-02", "package"): (82000, "Скидка за оплату всей суммы сразу"),
                 ("OSH-02", "kezek"): (20000, "Партнёрская скидка при покупке трёх сфер")}
PAY_PROFILE = {"BSK-01": "good", "BSK-02": "good", "OSH-01": "good", "OSH-02": "late", "JAL-01": "late",
               "KAR-01": "good", "TOK-01": "good", "KBL-01": "late", "BAT-01": "bad", "NAR-01": "good"}

MALE = ["Азамат", "Нурлан", "Айбек", "Бакыт", "Эрмек", "Руслан", "Тилек", "Мирлан", "Эмил", "Улан", "Данияр",
        "Кубат", "Адилет"]
FEMALE = ["Айгерим", "Айзада", "Гульнара", "Чолпон", "Динара", "Назгуль", "Бермет", "Аида", "Нургуль", "Салтанат"]
SURNAMES = ["Асанов", "Токтогулов", "Сыдыков", "Жумабаев", "Эсенов", "Исаков", "Бекболотов", "Курманов",
            "Мамытов", "Осмонов", "Темиров", "Алиев", "Абдыраев", "Кадыров"]
BIZ = [("shop", "Магазин", ["«Береке»", "«Алтын»", "«Нур»", "«Достук»", "«Ак-Жол»", "«Мээрим»", "«Чолпон»"]),
       ("shop", "Мини-маркет", ["«Тянь-Шань»", "«Ала-Тоо»", "«Азиз»", "«Бекзат»", "«Алма»"]),
       ("wholesale", "Оптовая база", ["«Сузак»", "«Манас-Опт»", "«Кыргыз Трейд»", "«Эсен»"]),
       ("services", "Барбершоп", ["«Усма»", "«Sharp»", "«Каприз»"]),
       ("services", "Салон красоты", ["«Айсулуу»", "«Лила»", "«Нарын-Стиль»"]),
       ("services", "Автомойка", ["«Чистая Волна»", "«Блеск»", "«Тулпар»"]),
       ("services", "Стоматология", ["«Жылдыз»", "«Дента-Плюс»"]),
       ("cafe", "Кафе", ["«Бешбармак»", "«Чайхана Ош»", "«Дастархан»", "«Нават»", "«Ала-Арча»"]),
       ("cafe", "Кофейня", ["«Бодрое утро»", "«Coffee Time»", "«Кара-Кол»"])]
SUBJECTS = ["Не открывается касса", "Ошибка при печати чека", "Не синхронизируется склад", "Нужна настройка отчётов",
            "Пропали данные за вчера", "Как добавить сотрудника", "Медленно работает программа",
            "Не приходят уведомления", "Вопрос по тарифу", "Ошибка при входе"]
LOST = ["Не дозвонились", "Выбрали другое решение", "Дорого", "Бизнес закрылся", "Нет интереса"]
TASKS = ["Сдать отчёт по продажам за месяц", "Провести демо для новых заявок", "Обновить вывеску по брендбуку",
         "Позвонить клиентам с истекающей подпиской", "Пройти аттестацию сотрудников",
         "Подготовить план на следующий месяц", "Собрать отзывы клиентов", "Проверить материалы на точке",
         "Закрыть просроченные заявки", "Согласовать акцию с центром"]
CITY_WEIGHTS = [("Бишкек", 30), ("Ош", 16), ("Джалал-Абад", 8), ("Каракол", 7), ("Балыкчы", 3), ("Токмок", 6),
                ("Кара-Балта", 6), ("Нарын", 4), ("Талас", 4), ("Баткен", 3), ("Чолпон-Ата", 3), ("Кант", 2),
                ("Узген", 3), ("Исфана", 1)]


def phone(rng):
    return f"0{rng.choice([500, 550, 555, 700, 705, 770, 772, 777, 999])} {rng.randint(100, 999)} {rng.randint(100, 999)}"


def person(rng):
    if rng.random() < 0.5:
        return f"{rng.choice(MALE)} {rng.choice(SURNAMES)}"
    return f"{rng.choice(FEMALE)} {rng.choice(SURNAMES)}а"


def at(d, hour=11):
    return timezone.make_aware(datetime.combine(d, time(hour, rng_minute(d))))


def rng_minute(d):
    return (d.toordinal() * 7) % 60


class Command(BaseCommand):
    help = "Демо-данные OZOK Network (--reset пересоздаёт)"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="удалить данные и создать заново")

    # ------------------------------------------------------------------ main
    def handle(self, *args, **opts):
        if opts["reset"]:
            self.wipe()
        elif Product.objects.exists():
            self.stdout.write("Данные уже есть. Для пересоздания: seed_demo --reset")
            return
        self.rng = random.Random(42)
        self._n = 0
        self.today = today()
        with transaction.atomic():
            self.create_catalog()
            self.equipment = list(Equipment.objects.all())
            self.create_people()
            self.create_contracts()
            self.create_leads_and_clients()
            self.create_integration_history()
            self.create_payments()
            self.create_implementations()
            self.create_training()
            self.create_quality()
            self.create_plans_and_tasks()
            self.create_payouts()
            self.create_legacy_clients()
            self.create_manager_sales()
            self.create_rewards()
            self.create_deals_and_ledger()
            self.create_change_requests_and_settings()
            from core.seed_v3 import V3Seeder
            V3Seeder(self).run()
            from django.apps import apps
            from core import reference_ky
            reference_ky.fill(apps.get_model)  # *_ky для справочников
        self.report()

    def wipe(self):
        from billing.models import PartnerLedgerEntry, RecruiterReward, RecruiterRewardAllocation
        from clients.models import Deal, DealLine
        from core.models import AuditLog, ChangeRequest
        from leads.models import LeadActivity
        from billing.models import (CashTransaction, CommissionAccrual, NbsExpense, Payment, PaymentImport, Payout)
        from clients.models import (Branch, ChecklistTemplate, ContractTemplate, ImplementerAccrual,
                                    InstallationComment)
        from accounts.models import AccessRole
        from network.models import SetupSplitRule
        from django.db import DEFAULT_DB_ALIAS
        NbsExpense.objects.all().delete()
        CashTransaction.objects.all()._raw_delete(DEFAULT_DB_ALIAS)
        CommissionAccrual.all_objects.all()._raw_delete(DEFAULT_DB_ALIAS)
        for model in (Payout, Payment, PaymentImport):
            model.all_objects.all().delete() if hasattr(model, "all_objects") else model.objects.all().delete()
        ImplementerAccrual.objects.all().delete()
        InstallationComment.objects.all().delete()
        for model in (SetupSplitRule, ChecklistTemplate, ContractTemplate):
            model.objects.all().delete()
        for model in (IntegrationEvent, TaskComment, Task, Ticket, Audit, SalesPlan, Enrollment, Course,
                      RecruiterRewardAllocation, RecruiterReward, PartnerLedgerEntry, ChangeRequest, DealLine):
            model.objects.all().delete()
        Deal.objects.all().delete()
        for model in (CommissionPayout, FranchiseePayment, ClientSale, Charge):
            model.all_objects.all().delete()
        for model in (Contract, Implementation, ClientProductAccount, LeadActivity, Lead, Client, CommissionRule,
                      Equipment, Tariff, PartnerStatusHistory):
            model.objects.all().delete()
        User.objects.filter(is_superuser=False).delete()
        User.objects.all().update(franchisee=None)
        Franchisee.objects.all().delete()
        Package.objects.all().delete()
        Product.objects.all().delete()
        AuditLog.objects.all().delete()
        AccessRole.objects.filter(is_system=False).delete()
        from billing.models import CashAccount
        CashAccount.objects.update(opening_balance=0)

    # --------------------------------------------------------------- catalog
    def create_catalog(self):
        self.products = {}
        for i, (code, name, sphere, icon, cash, per_sale, desc) in enumerate(PRODUCTS, start=1):
            self.products[code] = Product.objects.create(
                code=code, name=name, sphere=sphere, icon=icon, description=desc, price_cash=cash,
                price_per_sale=per_sale, setup_price=SETUP_PRICE[code], default_commission_percent=10
                if code in ("el_bilim", "kechikpe") else 20, training_price=3000, max_discount_percent=15,
                min_setup_price=int(SETUP_PRICE[code] * 0.6), sort_order=i, integration_enabled=True)
        pkg = Package.objects.create(name="Пакет из четырёх продуктов", price_cash=87000, price_prepay=89000,
                                     price_per_sale=127000, prepay_percent=40)
        pkg.products.set([self.products[c] for c in ("nur_retail", "nur_wholesale", "kezek", "coca")])
        self.package = pkg
        # тарифы с историей цен: прежняя цена действовала до смены, новая — с PRICE_CHANGE_DAYS_AGO дней назад
        self.tariffs = {}
        change = self.today - timedelta(days=PRICE_CHANGE_DAYS_AGO)
        for code, items in TARIFFS.items():
            self.tariffs[code] = []
            for i, (tcode, name, new, old) in enumerate(items, start=1):
                t = create_tariff(product=self.products[code], code=tcode, name=name, monthly_price=old,
                                  valid_from=self.today - timedelta(days=330), sort_order=i,
                                  min_price=int(min(new, old) * 0.8))
                if new != old:
                    set_price(t, new, change)
                self.tariffs[code].append(t)
        for i, (name, price, product_code) in enumerate(EQUIPMENT, start=1):
            Equipment.objects.create(name=name, price=price, sort_order=i,
                                     product=self.products[product_code] if product_code else None)
        start = date(2020, 1, 1)
        for name, kind in (("Абонплата — 20%", "subscription"), ("Установка и обучение — 100%", "setup"),
                           ("Обучение — 100%", "training"), ("Оборудование — 100%", "equipment"),
                           ("Прочее — 100%", "other")):
            CommissionRule.objects.create(name=name, sale_kind=kind, percent=20 if kind == "subscription" else 100,
                                          valid_from=start, note="Правило по умолчанию")
        basis = "ТЗ OZOK §2.1"
        for tier, pct in (("growth", 30),):
            CommissionRule.objects.create(name="Абонплата Growth — 30%", sale_kind="subscription", partner_tier=tier,
                                          percent=pct, valid_from=start, basis=basis)
        for code in ("el_bilim", "kechikpe"):
            for tier in ("standard", "growth"):
                CommissionRule.objects.create(
                    name=f"{self.products[code].name} — 10% ({tier})", product=self.products[code],
                    sale_kind="subscription", partner_tier=tier, percent=10, valid_from=start, basis=basis)
        # лиды OZOK (решение OZOK №1): 15% с абонплаты, франчайзи оставляет 70% дохода (OZOK — 30%)
        CommissionRule.objects.create(
            name="Лид OZOK — абонплата", sale_kind="subscription", lead_source="nbs_ads",
            percent=get_decimal("nbs_lead.subscription_percent"),
            valid_from=start, basis="Решение OZOK №1: лиды из рекламы OZOK")
        keep = 100 - get_decimal("nbs_lead.nbs_share_percent")
        for name, kind in (("установка и обучение", "setup"), ("оборудование", "equipment"), ("прочее", "other")):
            CommissionRule.objects.create(
                name=f"Лид OZOK — {name} — доля франчайзи", sale_kind=kind, lead_source="nbs_ads",
                percent=keep, valid_from=start, basis="Решение OZOK №1: лиды из рекламы OZOK")
        CommissionRule.objects.create(
            name="COCA тариф Сеть — 25% + 200 сом/мес", product=self.products["coca"], tariff=self.tariffs["coca"][2],
            sale_kind="subscription", percent=25, fixed_amount=200, fixed_per="month", base="paid", valid_from=start,
            note="Пример гибридного правила: процент плюс фикс за каждый оплаченный месяц")

    # ----------------------------------------------------------------- people
    def user(self, email, first, last, role, franchisee=None, phone_=""):
        return User.objects.create_user(email=email, password=PASSWORD, first_name=first, last_name=last,
                                        role=role, franchisee=franchisee, phone=phone_,
                                        is_staff=role == "super_admin")

    def create_people(self):
        self.admin = self.user("admin@ozok.kg", "Азамат", "Жолдошев", "super_admin", phone_="0555 000 001")
        self.curator = self.user("curator@ozok.kg", "Айнура", "Кадырова", "network_head", phone_="0555 000 002")
        self.accountant = self.user("buh@ozok.kg", "Гульмира", "Сыдыкова", "finance", phone_="0555 000 003")
        self.network = self.user("network@ozok.kg", "Руслан", "Эсенов", "network_head", phone_="0555 000 004")
        self.finance = self.user("finance@ozok.kg", "Нургуль", "Исаева", "finance", phone_="0555 000 005")
        self.quality_u = self.user("quality@ozok.kg", "Эльвира", "Омурбекова", "quality", phone_="0555 000 006")
        self.dev = self.user("dev@ozok.kg", "Тимур", "Алымбеков", "implementer", phone_="0555 000 007")
        self.support_osh = self.user("support.osh@ozok.kg", "Бекзат", "Мамбетов", "regional_support",
                                     phone_="0555 000 008")
        self.support_osh.region = "Ош"
        self.support_osh.save()
        self.recruiters = {
            "barsbek": self.user("barsbek@ozok.kg", "Барсбек", "Сатыбалдиев", "recruiter", phone_="0555 000 009"),
            "mirlan": self.user("mirlan@ozok.kg", "Мирлан", "Касымов", "recruiter", phone_="0555 000 010")}
        self.frs, self.owners, self.employees = {}, {}, {}
        self.intended = {}
        for (name, code, legal, city, cities, address, ph, owner, status, cur, months, staff, slug) in FRANCHISEES:
            fr = Franchisee.objects.create(
                name=name, partner_code=code, legal_name=legal, inn=f"0{self.rng.randint(1000000000000, 9999999999999)}"
                if legal else "", city=city, region=REGION.get(city, city), cities=cities, address=address, phone=ph,
                owner_name=owner, full_name=owner, passport=f"AN{self.rng.randint(1000000, 9999999)}",
                email=f"{slug}@ozok.kg", status=status, curator=self.curator if cur == "curator" else self.admin,
                responsible=self.network, recruited_by=self.recruiters.get(RECRUITED.get(code)),
                candidate_source=self.rng.choice(["instagram", "referral", "site", "event"]),
                funnel_stage=STAGE_OF[status], tier="standard",
                joined_at=add_months(self.today, -months) if status != "candidate" else None)
            self.frs[code] = fr
            self.intended[code] = (status, STAGE_OF[status])
            self.employees[code] = []
            for j, entry in enumerate(staff):
                full, role = entry.split(":")
                first, last = full.split(" ", 1)
                if slug == "osh":
                    email = "osh.manager@ozok.kg" if (role == "franchisee_staff" and j == 1) else (
                        "osh@ozok.kg" if role == "franchisee" else f"osh.manager{j}@ozok.kg")
                elif slug == "bishkek1":
                    email = "bishkek@ozok.kg" if role == "franchisee" else f"{slug}.manager{j}@ozok.kg"
                else:
                    email = f"{slug}@ozok.kg" if role == "franchisee" else f"{slug}.manager{j}@ozok.kg"
                u = self.user(email, first, last, role, fr, phone(self.rng))
                self.employees[code].append(u)
                if role == "franchisee":
                    self.owners[code] = u
        # уровни Standard/Growth с датами действия (история)
        for code, months_ago in GROWTH_SINCE.items():
            change_partner_field(self.frs[code], "tier", "growth", user=self.admin, reason="Growth подтверждён",
                                 effective_from=self.today - timedelta(days=30 * months_ago), allow_backdate=True)
        # понижение BSK-02 запланировано на 1-е число следующего месяца
        change_partner_field(self.frs["BSK-02"], "tier", "standard", user=self.network,
                             reason="Маркетинговые условия не выполнены (проверка качества)")
        # кандидаты, стажёры, менеджеры, закрытый партнёр
        for (name, code, city, status, stage, days, rec, source, user_email) in EXTRA_PARTNERS:
            fr = Franchisee.objects.create(
                name=name, partner_code=code, full_name=name, city=city, region=REGION.get(city, city), cities=[city],
                phone=phone(self.rng), email=f"{code.lower()}@ozok.kg", status=status, funnel_stage=stage,
                recruited_by=self.recruiters[rec], responsible=self.network, candidate_source=source,
                curator=self.network, funnel_stage_since=self.today - timedelta(days=days),
                certification_result="passed" if stage in ("manager", "first_sale", "second_sale", "third_sale",
                                                           "franchise_contract") else "",
                training_date=self.today - timedelta(days=days + 20) if status != "candidate" else None,
                closed_at=self.today - timedelta(days=40) if status == "closed" else None,
                status_reason="Расторжение по инициативе партнёра" if status == "closed" else "")
            self.frs[code] = fr
            self.employees[code] = []
            self.intended[code] = (status, stage)
            order = Franchisee.STAGE_ORDER
            for i, st in enumerate(order[1:order.index(stage) + 1], start=1):
                PartnerStatusHistory.objects.create(
                    partner=fr, field="funnel_stage", old=order[i - 1], new=st, decided_by=self.network,
                    effective_from=self.today - timedelta(days=max(days + (order.index(stage) - i) * 6, 0)),
                    reason="Продвижение по воронке")
            if user_email:
                u = self.user(user_email, name.split()[0], name.split()[-1], "manager", fr, phone(self.rng))
                self.owners[code] = u
                self.employees[code].append(u)

    # -------------------------------------------------------------- contracts
    def make_contract(self, fr, what, scheme, months_ago, code=None):
        product = self.products.get(what)
        package = self.package if what == "package" else None
        signed = self.today - timedelta(days=30 * months_ago + 3)
        quote = price_quote(product=product, package=package, scheme=scheme)
        fee, note = FEE_DISCOUNTS.get((code, what), (quote["list_price"], ""))
        fee = q2(fee)
        if scheme == "hybrid":
            parts = {"upfront_amount": q2(fee * Decimal("0.3")), "installments_total": q2(fee * Decimal("0.3")),
                     "installments_count": 3, "installment_step_months": 1}
        else:
            parts = preset_parts(scheme, fee, quote.get("prepay_percent", 40), 3)
        c = Contract.objects.create(
            franchisee=fr, signed_at=signed, product=product, package=package, payment_scheme=scheme,
            list_price=quote["list_price"], fee_total=fee, discount_note=note,
            prepay_percent=quote.get("prepay_percent", 40), withhold_percent=30, **parts)
        c.products.set(contract_products(c))
        return c

    def create_contracts(self):
        self.fr_products = {}
        for code, items in CONTRACTS.items():
            fr = self.frs[code]
            for what, scheme, months_ago in items:
                activate_contract(self.make_contract(fr, what, scheme, months_ago, code))
        # черновик договора: «Бишкек Восток» ещё оформляется
        self.make_contract(self.frs["BSK-03"], "nur_retail", "cash", 0)
        # менеджер на этапе «Договор франшизы»: черновик по предложению «Рассрочка до»
        offer = FranchiseOffer.objects.filter(name="Рассрочка до").first()
        if offer is not None:
            c = Contract.objects.create(
                franchisee=self.frs["MGR-02"], product=self.products["coca"], payment_scheme="hybrid",
                list_price=offer.total, fee_total=offer.total, upfront_amount=offer.upfront,
                installments_total=offer.total - offer.upfront, installments_count=offer.installments_count,
                installment_step_months=offer.step_months, offer=offer, withhold_percent=30,
                signed_at=self.today, discount_note="Предложение «Рассрочка до»")
            c.products.set(contract_products(c))
        # активация договора переводит партнёра в «Франчайзи»: возвращаем задуманные статусы демо-данных
        for code, (status, stage) in self.intended.items():
            fr = self.frs[code]
            if (fr.status, fr.funnel_stage) != (status, stage) and code in {f[1] for f in FRANCHISEES}:
                PartnerStatusHistory.objects.filter(partner=fr, field__in=("status", "funnel_stage")).delete()
                Franchisee.objects.filter(pk=fr.pk).update(status=status, funnel_stage=stage)
                if status == "suspended":
                    Franchisee.objects.filter(pk=fr.pk).update(
                        suspended_at=self.today - timedelta(days=20), status_reason="Проверка качества: жалобы клиентов")
        for fr in self.frs.values():
            fr.refresh_from_db()
        for code in self.frs:
            ids = set(Contract.objects.filter(franchisee=self.frs[code], status="active")
                      .values_list("products", flat=True))
            self.fr_products[code] = [p for p in self.products.values() if p.id in ids]

    # ------------------------------------------------------ leads and clients
    def pick_city(self):
        cities, weights = zip(*CITY_WEIGHTS)
        return self.rng.choices(cities, weights)[0]

    def biz(self):
        btype, prefix, names = self.rng.choice(BIZ)
        return btype, f"{prefix} {self.rng.choice(names)}"

    def mk_client_time(self, client, dt):
        Client.objects.filter(pk=client.pk).update(created_at=dt)
        client.created_at = dt

    def create_leads_and_clients(self):
        rng, now = self.rng, timezone.now()
        self.clients = []
        for i in range(120):
            age = rng.choices(range(1, 181), weights=[3] * 30 + [2] * 60 + [1] * 90)[0]
            dt = now - timedelta(days=age, hours=rng.randint(0, 10), minutes=rng.randint(0, 59))
            interest = rng.choices(["program", "consult", "franchise"], [65, 20, 15])[0]
            product = rng.choice([p for p in self.products] + ["help", "", "package:%d" % self.package.id])
            if interest == "franchise":
                product = rng.choice(list(self.products) + [""])
            lead = lead_services.create_lead(
                source=rng.choices(["nbs_ads", "partner_ads", "instagram", "whatsapp", "referral", "site", "inbound_call",
                                    "offline", "other"], [22, 14, 14, 8, 10, 12, 8, 6, 6])[0],
                interest=interest, product=product, name=person(rng), phone=phone(rng), city=self.pick_city(),
                comment=rng.choice(["", "", "Хочу узнать цену", "Перезвоните после 15:00", "Нужна демонстрация"]),
                now=dt)
            Lead.objects.filter(pk=lead.pk).update(created_at=dt)
            if lead.franchisee_id is None:
                if interest == "franchise" and age > 20 and rng.random() < 0.35:
                    lead_services.lose_lead(lead, rng.choice(LOST), now=dt + timedelta(days=3))
                continue
            self.progress_lead(lead, age, dt, now)

    def progress_lead(self, lead, age, dt, now):
        rng = self.rng
        roll = rng.random()
        if age <= 1:
            return  # свежая назначенная
        if age <= 4 and roll < 0.55:
            return  # назначена и не взята: просрочена >24 ч
        cap = lambda d: min(d, now - timedelta(minutes=5))  # noqa: E731
        lead_services.take_lead(lead, now=cap(dt + timedelta(hours=rng.randint(1, 20))))
        if age <= 6 and roll < 0.75:
            return  # в работе
        if roll < 0.9 or age > 15:
            lead_services.demo_lead(lead, now=cap(dt + timedelta(days=1)))
        outcome = rng.random()
        if age > 12:
            win_p, lose_p = 0.42, 0.33
        else:
            win_p, lose_p = 0.25, 0.15
        if outcome < win_p and lead.status == "demo":
            btype, name = self.biz()
            when = cap(dt + timedelta(days=rng.randint(2, 10)))
            lead_services.win_lead(lead, business_name=name, business_type=btype, now=when)
            self.mk_client_time(lead.client, when)
            self.clients.append(lead.client)
        elif outcome < win_p + lose_p:
            lead_services.lose_lead(lead, rng.choice(LOST), now=cap(dt + timedelta(days=rng.randint(2, 12))))

    # ----------------------------------------------- integration (sales/accounts)
    def event(self, product, data, when):
        self._n = getattr(self, "_n", 0) + 1
        data = {"event_id": f"seed-{self._n:05d}", **data}
        ev = process_event(product, data)
        IntegrationEvent.objects.filter(pk=ev.pk).update(received_at=when)
        return ev

    def create_integration_history(self):
        rng, today_ = self.rng, self.today
        active = [c for c in self.frs if self.frs[c].status in ("franchisee", "suspended") and self.fr_products[c]]
        # прямые клиенты франчайзи (не из заявок)
        weights = {"OSH-01": 6, "BSK-01": 5, "BSK-02": 4, "KAR-01": 4}
        for _ in range(38):
            code = rng.choices(active, [weights.get(c, 2) for c in active])[0]
            fr = self.frs[code]
            btype, name = self.biz()
            created = timezone.now() - timedelta(days=rng.randint(3, 175), hours=rng.randint(0, 8))
            client = Client.objects.create(franchisee=fr, business_name=name, business_type=btype,
                                           contact_name=person(rng), phone=phone(rng), city=fr.city)
            self.mk_client_time(client, created)
            self.clients.append(client)
        # клиенты, пришедшие из самих продуктов (client.upsert)
        for _ in range(14):
            code = rng.choice(active)
            fr = self.frs[code]
            product = rng.choice(self.fr_products[code])
            btype, name = self.biz()
            created = timezone.now() - timedelta(days=rng.randint(5, 170))
            ext = f"{product.code}-up-{self._n + 1:04d}"
            self.event(product, {"type": "client.upsert", "external_client_id": ext, "partner_code": code,
                                 "business_name": name, "business_type": btype, "contact_name": person(rng),
                                 "phone": phone(rng), "city": fr.city, "connected_at": created.date().isoformat()},
                       created)
            acc = ClientProductAccount.objects.get(product=product, external_id=ext)
            self.mk_client_time(acc.client, created)
            self.clients.append(acc.client)
        self.sale_events = []
        for client in self.clients:
            code = next(c for c, f in self.frs.items() if f.id == client.franchisee_id)
            products = self.fr_products[code] or list(self.products.values())
            start = timezone.localtime(client.created_at).date()
            # установка/оборудование/прочее: франчайзи вносит вручную (через те же сервисы, что и API)
            for _ in range(rng.choices([0, 1, 2], [30, 50, 20])[0]):
                p = rng.choice(products)
                kind = rng.choices(["setup", "equipment", "other"], [50, 35, 15])[0]
                when = start + timedelta(days=rng.randint(0, 10))
                extra = {}
                if kind == "setup":
                    amount = round(SETUP_PRICE[p.code] * rng.choice([1, 1, 0.8, 1.2]) / 500) * 500
                    comment = "Установка и обучение"
                elif kind == "equipment":
                    eq = rng.choice(self.equipment)
                    qty = rng.choice([1, 1, 2])
                    amount = round(float(eq.price) * qty * rng.choice([1, 1, 1.05]) / 100) * 100
                    extra = {"equipment": eq, "quantity": qty}
                    comment = eq.name
                else:
                    amount, comment = rng.choice([2000, 3500, 5000]), "Доп. работы"
                create_client_sale(client=client, product=p, kind=kind, amount=amount,
                                   date=min(when, today_), comment=comment,
                                   created_by=self.owners.get(code), **extra)
            # подписки: аккаунты в продуктах и оплаты через события
            if rng.random() > 0.9:
                continue
            for p in rng.sample(products, k=min(len(products), rng.choices([1, 2], [75, 25])[0])):
                self.simulate_account(client, p, start)

    def simulate_account(self, client, product, start):
        rng, today_ = self.rng, self.today
        ext = f"{product.code}-{client.id:05d}"
        tariff = rng.choices(self.tariffs[product.code], [3, 4, 1, 1][:len(self.tariffs[product.code])])[0]
        discount = rng.choice([0, 0, 0, 10, 15])  # персональная скидка клиента, %
        if not ClientProductAccount.objects.filter(product=product, external_id=ext).exists():
            ClientProductAccount.objects.create(client=client, product=product, external_id=ext,
                                                connected_at=start, tariff=tariff, discount_percent=discount)
        behavior = rng.choices(["active", "lapsed"], [82, 18])[0]
        stop = today_ if behavior == "active" else today_ - timedelta(days=rng.randint(5, 40))
        paid_until, pay_date = None, start + timedelta(days=rng.randint(0, 6))
        while pay_date <= stop:
            months = rng.choices([1, 3, 6, 12], [50, 30, 15, 5])[0]
            base = pay_date if paid_until is None else max(paid_until + timedelta(days=1), pay_date)
            new_until = add_months(base, months) - timedelta(days=1)
            if paid_until is not None and base > today_:
                break
            pid = f"{product.code}-pay-{self._n + 1:05d}"
            # цена тарифа на дату оплаты (история цен) за вычетом скидки клиента
            price = price_on(tariff, pay_date) or tariff.prices.last().monthly_price
            amount = q2(price * months * Decimal(100 - discount) / 100)
            ev = self.event(product, {
                "type": "payment.succeeded", "external_payment_id": pid, "external_client_id": ext,
                "tariff_code": tariff.code, "amount": str(amount), "months": months,
                "paid_until": new_until.isoformat(), "paid_at": at(pay_date).isoformat()}, at(pay_date))
            if ev.status == "processed":
                self.sale_events.append((product, pid, ev, amount))
            paid_until = new_until
            pay_date = new_until - timedelta(days=rng.randint(0, 4))
            if pay_date <= base:
                pay_date = base + timedelta(days=1)
        # дубликат повторной отправки и один возврат
        if self.sale_events and rng.random() < 0.05:
            product_, pid, ev, amount = self.sale_events[-1]
            process_event(product_, ev.payload)

    # --------------------------------------------------- payments and payouts
    def create_payments(self):
        rng = self.rng
        # неопознанные события в журнале + возврат
        any_product = self.products["nur_retail"]
        now = timezone.now()
        self.event(any_product, {"type": "client.upsert", "external_client_id": "retail-new-777",
                                 "partner_code": "XXX-99", "business_name": "Магазин «Айя»", "city": "Кызыл-Кия"},
                   now - timedelta(days=2))
        self.event(self.products["coca"], {"type": "client.upsert", "external_client_id": "coca-new-778",
                                           "partner_code": "", "business_name": "Кафе «Бакай»"},
                   now - timedelta(days=1))
        self.event(self.products["nur_wholesale"], {
            "type": "payment.succeeded", "external_payment_id": "opt-orphan-1", "external_client_id": "opt-99999",
            "amount": "13500", "months": 3, "paid_until": (self.today + timedelta(days=80)).isoformat(),
            "paid_at": at(self.today - timedelta(days=3)).isoformat()}, now - timedelta(days=3))
        self.event(self.products["kezek"], {"type": "payment.refunded", "external_payment_id": "kezek-orphan-2",
                                            "amount": "3000"}, now - timedelta(hours=7))
        if self.sale_events:
            product, pid, ev, amount = rng.choice(self.sale_events[len(self.sale_events) // 2:])
            self.event(product, {"type": "payment.refunded", "external_payment_id": pid,
                                 "amount": str(q2(amount / 2))}, now - timedelta(days=1))
        # платежи франчайзи центру по графику начислений
        for code, profile in PAY_PROFILE.items():
            fr = self.frs[code]
            charges = list(Charge.objects.filter(franchisee=fr, due_date__isnull=False, due_date__lte=self.today)
                           .order_by("due_date", "id"))
            if profile == "late":
                charges = charges[:-1]
            elif profile == "bad":
                charges = []
            for ch in charges:
                d = min(ch.due_date + timedelta(days=rng.randint(0, 4)), self.today)
                FranchiseePayment.objects.create(
                    franchisee=fr, contract=ch.contract, amount=ch.amount, date=d,
                    method=rng.choice(["bank", "bank", "cash"]), created_by=self.accountant,
                    comment=f"Оплата: {ch.description}")

    def create_payouts(self):
        """Выплаты комиссий — только через пакеты Payout (draft → verified → paid, проверяет admin, платит бухгалтер);
        ручные CommissionPayout не создаются (D1)."""
        from billing import payouts as payout_svc
        from core.models import month_first, month_last
        last_month = month_last(month_first(self.today) - timedelta(days=1))
        for code in ("BSK-01", "BSK-02", "KAR-01", "OSH-01", "TOK-01", "KBL-01"):
            fr = self.frs[code]
            try:
                payout = payout_svc.create(fr, last_month, self.accountant, "Выплата комиссий за абонплату клиентов")
            except BusinessError:
                continue
            payout_svc.verify(payout, self.admin)
            payout_svc.pay(payout, self.accountant, date=self.today - timedelta(days=5), payment_ref="Демо-выплата")

    def create_legacy_clients(self):
        """~25 существующих клиентов OZOK (без партнёра, абонплата 100% OZOK) по сферам; часть — просрочена."""
        from clients import legacy
        rng = random.Random(7)
        names = ["Кафе «Алтын»", "Магазин «Береке»", "Аптека «Шифа»", "Опт «Азия-Трейд»", "Барбершоп «Борода»",
                 "Пекарня «Нан»", "Салон «Нур»", "Автомойка «Чистый путь»", "Бутик «Ак-Куу»", "Столовая «Дастархан»",
                 "Магазин «Ыйык»", "Кофейня «Бакай»", "Студия «Сезим»", "Мини-маркет «Дос»", "Фитнес «Атлет»",
                 "Пиццерия «Арзу»", "Цветы «Гүл»", "Книги «Манас»", "Кафе «Чайка»", "Опт «Ош-Плюс»",
                 "Магазин «Таза»", "Стоматология «Дент»", "Клиника «Саламат»", "Зоомагазин «Дос-Пет»",
                 "Ресторан «Ала-Тоо»"]
        cities = ["Бишкек", "Ош", "Каракол", "Джалал-Абад", "Токмок", "Кант"]
        codes = list(self.products)
        for i, name in enumerate(names):
            code = codes[i % len(codes)]
            product, tariffs = self.products[code], self.tariffs.get(code) or []
            started = self.today - timedelta(days=rng.randint(60, 700))
            bucket = i % 5  # 0 — оплачено давно (просрочка), 1 — grace, остальные — активные
            paid_until = (self.today - timedelta(days=rng.randint(60, 120)) if bucket == 0
                          else self.today - timedelta(days=2) if bucket == 1
                          else self.today + timedelta(days=rng.randint(5, 90)))
            tariff = tariffs[0] if tariffs and i % 2 == 0 else None
            sphere = {"product": product, "tariff": tariff,
                      "monthly_price": None if tariff else Decimal(rng.choice([1200, 1800, 2500, 3200])),
                      "started_at": started, "paid_until": paid_until, "external_id": f"LEG-{code}-{1000 + i}",
                      "elqr_account": f"7700{i:04d}" if i % 4 == 0 else ""}
            company = {"business_name": name, "inn": f"0{rng.randint(10**12, 10**13 - 1)}",
                       "contact_name": "Контакт " + str(i + 1), "phone": f"0700 {100000 + i * 137:06d}"[:11],
                       "city": cities[i % len(cities)], "region": cities[i % len(cities)],
                       "address": f"ул. Демо, {i + 1}", "branch_address": ""}
            legacy._create(company, [sphere], self.admin)
        # один клиент с двумя сферами
        first = Client.objects.filter(origin="ozok_legacy").first()
        extra = next(c for c in codes if not first.accounts.filter(product=self.products[c]).exists())
        legacy._create({"business_name": first.business_name}, [{
            "product": self.products[extra], "tariff": None, "monthly_price": Decimal("2000"),
            "started_at": self.today - timedelta(days=90), "paid_until": self.today + timedelta(days=30),
            "external_id": f"LEG-{extra}-2000", "elqr_account": ""}], self.admin, merge_into=first)

    # ------------------------------------------------------- implementations
    def create_implementations(self):
        rng = self.rng
        accounts = {}
        for a in ClientProductAccount.objects.all():
            accounts.setdefault(a.client_id, []).append(a.product)
        for client in self.clients:
            code = next(c for c, f in self.frs.items() if f.id == client.franchisee_id)
            prods = list(accounts.get(client.id, []))
            if not prods and rng.random() < 0.5:
                prods = [rng.choice(self.fr_products[code] or list(self.products.values()))]
            employees = self.employees.get(code) or [None]
            for p in prods:
                started = timezone.localtime(client.created_at).date()
                has_account = p in accounts.get(client.id, [])
                stage = "launched" if has_account and rng.random() < 0.85 else rng.choices(
                    ["contract", "setup", "training", "launched", "cancelled"], [15, 30, 25, 20, 10])[0]
                planned = started + timedelta(days=rng.randint(7, 30))
                if stage not in ("launched", "cancelled") and rng.random() < 0.5:
                    planned = self.today + timedelta(days=rng.randint(-12, 25))
                Implementation.objects.create(
                    franchisee_id=client.franchisee_id, client=client, product=p, stage=stage, started_at=started,
                    planned_launch=planned, launched_at=min(planned, self.today) if stage == "launched" else None,
                    responsible=rng.choice(employees))

    # --------------------------------------------------------------- training
    def create_training(self):
        rng, now = self.rng, timezone.now()
        courses = [
            Course.objects.create(title="Стандарты сервиса OZOK", description="Брендбук, работа с клиентом, SLA.",
                                  materials_url="https://academy.ozok.kg/standards", pass_score=80),
            Course.objects.create(title="NUR CRM Market: сертификация", product=self.products["nur_retail"],
                                  materials_url="https://academy.ozok.kg/market", pass_score=75),
            Course.objects.create(title="NUR CRM Опт: сертификация", product=self.products["nur_wholesale"],
                                  materials_url="https://academy.ozok.kg/opt", pass_score=75),
            Course.objects.create(title="КЕЗЕК и COCA: внедрение", product=self.products["kezek"],
                                  materials_url="https://academy.ozok.kg/kezek", pass_score=80, validity_months=6),
        ]
        for code, staff in self.employees.items():
            for u in staff:
                for course in courses[:1] + rng.sample(courses[1:], k=rng.randint(0, 2)):
                    assigned = now - timedelta(days=rng.randint(30, 420))
                    en = training_services.enroll(course, u, now=assigned)
                    roll = rng.random()
                    if roll < 0.70:
                        done = assigned + timedelta(days=rng.randint(2, 20))
                        score = rng.choice([92, 88, 85, 95, 100, 81, 77, 55, 62, 40])
                        training_services.complete(en, score, now=min(done, now))
                    elif roll < 0.82:
                        training_services.start(en)

    # ---------------------------------------------------------------- quality
    def create_quality(self):
        rng, now = self.rng, timezone.now()
        client_pool = [c for c in self.clients
                       if self.frs_by_id[c.franchisee_id].status == "franchisee"] if hasattr(self, "frs_by_id") else None
        self.frs_by_id = {f.id: f for f in self.frs.values()}
        client_pool = [c for c in self.clients if self.frs_by_id[c.franchisee_id].status == "franchisee"]
        for i in range(42):
            client = rng.choice(client_pool)
            opened = now - timedelta(days=rng.choices(range(0, 130), weights=[6] * 7 + [2] * 123)[0],
                                     hours=rng.randint(0, 9))
            priority = rng.choices(["low", "normal", "high", "critical"], [20, 50, 22, 8])[0]
            t = quality_services.create_ticket(
                franchisee=client.franchisee, client=client, subject=rng.choice(SUBJECTS),
                description="Клиент сообщил о проблеме, подробности по телефону.", priority=priority, now=opened,
                assignee=rng.choice(self.employees[next(c for c, f in self.frs.items()
                                                        if f.id == client.franchisee_id)] or [None]))
            sla = t.sla_hours
            if (now - opened) < timedelta(hours=sla + 1) and rng.random() < 0.6:
                continue  # свежее обращение, ещё не отвечали
            if (now - opened).days < 2 and rng.random() < 0.5:
                continue
            delay = timedelta(hours=rng.choice([0.3, 0.5, 1, 2, 3, 6, 9, 20, 30, 50]) * (1 if sla < 24 else 0.7))
            first = min(opened + delay, now - timedelta(minutes=1))
            quality_services.change_status(t, "in_progress", now=first)
            if rng.random() < 0.8:
                resolved = min(first + timedelta(hours=rng.randint(1, 48)), now - timedelta(minutes=1))
                quality_services.change_status(t, "resolved", now=resolved)
                if rng.random() < 0.75:
                    quality_services.rate_ticket(t, rng.choices([5, 4, 3, 2, 1], [45, 30, 12, 8, 5])[0])
                if rng.random() < 0.3:
                    quality_services.change_status(t, "closed", now=resolved + timedelta(days=1)
                                                   if resolved + timedelta(days=1) < now else resolved)
        for code, fr in self.frs.items():
            if fr.status in ("candidate",):
                continue
            for days in rng.sample([150, 95, 60, 25], k=rng.randint(1, 2)):
                items = [{"criterion": c, "ok": rng.random() < 0.8, "comment": ""} for c in
                         quality_services.DEFAULT_CHECKLIST]
                Audit.objects.create(franchisee=fr, date=self.today - timedelta(days=days), inspector=self.curator,
                                     items=items, score=quality_services.audit_score(items),
                                     summary="Плановая проверка по стандартам OZOK.")

    # ------------------------------------------------------------ plans, tasks
    def create_plans_and_tasks(self):
        rng, now = self.rng, timezone.now()
        first = month_first(self.today)
        for code, fr in self.frs.items():
            if fr.status != "franchisee":
                continue
            scale = 2 if code in ("BSK-01", "BSK-02") else 1
            for back in (0, 1, 2):
                SalesPlan.objects.create(franchisee=fr, month=add_months(first, -back),
                                         target_clients=rng.randint(3, 6) * scale,
                                         target_revenue=rng.choice([60000, 80000, 100000, 120000]) * scale)
        creators = [self.curator, self.admin]
        for i in range(30):
            code = rng.choice([c for c, f in self.frs.items() if f.status in ("franchisee", "trainee")])
            fr = self.frs[code]
            staff = self.employees[code]
            age = rng.randint(1, 60)
            created = now - timedelta(days=age)
            due = self.today + timedelta(days=rng.randint(-10, 20))
            task = Task.objects.create(franchisee=fr, title=rng.choice(TASKS), description="Срок согласован с куратором.",
                                       priority=rng.choice(["low", "normal", "normal", "high"]), due_date=due,
                                       created_by=rng.choice(creators), assignee=rng.choice(staff) if staff else None)
            Task.objects.filter(pk=task.pk).update(created_at=created)
            actor = staff[0] if staff else self.curator
            roll = rng.random()
            when = min(created + timedelta(days=rng.randint(1, 8)), now - timedelta(minutes=1))
            if roll < 0.25:
                continue
            task_services.start_task(task, actor, now=when)
            if roll < 0.45:
                continue
            if roll < 0.8:
                task_services.done_task(task, actor, comment=rng.choice(["Готово", "Сделано, отчёт отправлен", ""]),
                                        now=when + timedelta(days=1) if when + timedelta(days=1) < now else when)
            elif roll < 0.9:
                task_services.reject_task(task, actor, "Нет возможности выполнить в срок", now=when)
            else:
                task_services.cancel_task(task, self.curator, now=when)
            if rng.random() < 0.3:
                task_services.add_comment(task, self.curator, "Принято, спасибо.", now=when)

    # ----------------------------------------------------------------- report
    # ------------------------------------------------ P2/P3/P0+: воронка, рекрутёры, продажи
    def create_manager_sales(self):
        """Менеджер MGR-01 делает первые продажи: этап воронки следует за оплаченными продажами."""
        mgr = self.frs["MGR-01"]
        user = self.owners["MGR-01"]
        for i, (name, amount) in enumerate((("Магазин «Нур-Сити»", 15000), ("Кафе «Береке»", 18000)), start=1):
            client = Client.objects.create(franchisee=mgr, business_name=name, business_type="shop",
                                           phone=phone(self.rng), city="Бишкек")
            create_client_sale(client=client, product=self.products["nur_retail"], kind="setup", amount=amount,
                               date=self.today - timedelta(days=10 - 3 * i), created_by=user)
            self.clients.append(client)
        mgr.refresh_from_db()

    def create_rewards(self):
        """Вознаграждение рекрутёров: прогресс считается по реальным платежам; один — проверен, один — выплачен."""
        rewards = list(RecruiterReward.objects.order_by("id"))
        for r in rewards:
            recruiter_svc.recompute(r.partner)
        accrued = list(RecruiterReward.objects.filter(status="accrued").order_by("id"))
        if accrued:
            recruiter_svc.verify(accrued[0], self.finance)
            recruiter_svc.pay(RecruiterReward.objects.get(pk=accrued[0].pk), self.accountant,
                              date=self.today - timedelta(days=5), payment_ref="ПП-REW-001")
        if len(accrued) > 1:
            recruiter_svc.verify(accrued[1], self.finance)

    def create_deals_and_ledger(self):
        rng = self.rng
        cats = {c.name: c for c in LedgerCategory.objects.all()}
        # доходы и расходы партнёров за 3 месяца
        for code in ("OSH-01", "BSK-01", "BSK-02", "KAR-01", "TOK-01", "KBL-01"):
            fr = self.frs[code]
            user = self.owners[code]
            for back in (0, 1, 2):
                base = add_months(month_first(self.today), -back)
                for name, lo, hi in (("Аренда", 15000, 40000), ("Зарплата", 30000, 90000),
                                     ("Реклама/маркетинг", 5000, 30000), ("Транспорт", 2000, 8000),
                                     ("Связь и интернет", 1500, 3500)):
                    PartnerLedgerEntry.objects.create(
                        partner=fr, date=min(base + timedelta(days=rng.randint(1, 26)), self.today),
                        type="expense", category=cats[name], amount=rng.randint(lo, hi), created_by=user,
                        description=f"{name}, {base:%m.%Y}")
                if rng.random() < 0.6:
                    PartnerLedgerEntry.objects.create(
                        partner=fr, date=min(base + timedelta(days=10), self.today), type="income",
                        category=cats["Прочий доход"], amount=rng.randint(2000, 12000), created_by=user,
                        description="Консультации по настройке")
        # продажи одной формой
        prod = self.products
        eq = {e.name: e for e in self.equipment}

        def deal(code, product, lines, *, days_ago=0, confirm=True, client=None, discount=None, lead=None):
            fr = self.frs[code]
            client = client or rng.choice([c for c in self.clients if c.franchisee_id == fr.id] or [None])
            if client is None:
                return None
            d = Deal(client=client, partner=fr, product=prod[product], date=self.today - timedelta(days=days_ago),
                     created_by=self.owners[code], lead=lead, **(discount or {}))
            d, _ = deal_svc.save_deal(d, lines)
            if confirm:
                d = deal_svc.confirm_deal(d, self.owners[code])
            return d

        def tariff(product, i=0):
            return self.tariffs[product][i]

        pos = eq.get("POS-терминал")
        for code, product, back in (("OSH-01", "nur_retail", 12), ("BSK-01", "coca", 9), ("KAR-01", "kezek", 6),
                                    ("TOK-01", "nur_retail", 4), ("BSK-02", "nur_wholesale", 15),
                                    ("KBL-01", "nur_retail", 2), ("OSH-01", "coca", 1)):
            lines = [{"kind": "setup", "discount_percent": 5}, {"kind": "subscription", "tariff": tariff(product),
                                                                  "months": 6}]
            if pos is not None:
                lines.insert(1, {"kind": "equipment", "equipment": pos, "quantity": 1})
            deal(code, product, lines, days_ago=back)
        # скидка выше допустимой -> ожидает согласования OZOK (ChangeRequest)
        pending = deal("OSH-01", "nur_retail", [{"kind": "setup", "discount_percent": 40},
                                                 {"kind": "subscription", "tariff": tariff("nur_retail"),
                                                  "months": 3}], days_ago=1)
        self.pending_deal = pending
        # черновик и отменённая продажа
        deal("BSK-01", "coca", [{"kind": "setup"}, {"kind": "training"}], confirm=False)
        cancelled = deal("TOK-01", "kezek", [{"kind": "setup"}], days_ago=8)
        if cancelled is not None:
            deal_svc.cancel_deal(cancelled, self.admin, "Клиент отказался от установки")
        # продажа из лида OZOK: доля OZOK 30% оформляется начислением
        nbs_lead = Lead.objects.filter(source="nbs_ads", status="paid", client__isnull=False).first()
        if nbs_lead is not None:
            code = next(c for c, f in self.frs.items() if f.id == nbs_lead.franchisee_id)
            deal(code, "nur_retail", [{"kind": "setup"}, {"kind": "subscription", "tariff": tariff("nur_retail"),
                                                           "months": 12}], days_ago=3, client=nbs_lead.client,
                 lead=nbs_lead)
        # прямые продажи OZOK (partner = null)
        for product, name, days in (("nur_retail", "ТЦ «Дордой Плаза» (прямая продажа OZOK)", 20),
                                    ("coca", "Ресторан «Нават» (прямая продажа OZOK)", 11),
                                    ("onai", "ОсОО «Алтын Групп» (прямая продажа OZOK)", 7),
                                    ("el_bilim", "Школа №12 (прямая продажа OZOK)", 5)):
            d = Deal(client_name=name, partner=None, product=prod[product], date=self.today - timedelta(days=days),
                     created_by=self.admin)
            d, _ = deal_svc.save_deal(d, [{"kind": "setup"}, {"kind": "subscription",
                                                               "tariff": self.tariffs[product][0], "months": 12}])
            deal_svc.confirm_deal(d, self.admin)

    def create_change_requests_and_settings(self):
        from leads.services import request_change
        lead = Lead.objects.filter(franchisee__isnull=False, source="nbs_ads").first()
        if lead is not None:
            owner = self.owners[next(c for c, f in self.frs.items() if f.id == lead.franchisee_id)]
            request_change(lead, owner, "source", "partner_ads", "Клиент пришёл по моей рекламе в Instagram")
        lead = Lead.objects.filter(franchisee__isnull=False, source="inbound_call").first()
        if lead is not None:
            owner = self.owners[next(c for c, f in self.frs.items() if f.id == lead.franchisee_id)]
            cr = request_change(lead, owner, "source", "referral", "Клиента порекомендовал знакомый")
            change_requests.decide(cr, self.network, True, "Подтверждено звонком клиенту")
        for lead in Lead.objects.filter(source="partner_ads", franchisee__isnull=False)[:4]:
            Lead.objects.filter(pk=lead.pk).update(owner_confirmed=False, owner_confirmed_by=None)
        set_setting("nbs_lead.share_due_days", 30, user=self.admin, reason="Подтверждено OZOK: срок оплаты доли 30 дней")

    def report(self):
        s = self.stdout.write
        s("Демо-данные созданы:")
        s(f"  продукты {Product.objects.count()}, пакеты {Package.objects.count()}, франчайзи "
          f"{Franchisee.objects.count()}, пользователи {User.objects.count()}")
        s(f"  договоры {Contract.objects.count()}, начисления {Charge.objects.count()}, платежи "
          f"{FranchiseePayment.objects.count()}, выплаты {CommissionPayout.objects.count()}")
        s(f"  заявки {Lead.objects.count()}, клиенты {Client.objects.count()}, аккаунты в продуктах "
          f"{ClientProductAccount.objects.count()}, внедрения {Implementation.objects.count()}")
        s(f"  продажи {ClientSale.objects.count()} (из интеграции "
          f"{ClientSale.objects.filter(source='integration').count()}), события интеграции "
          f"{IntegrationEvent.objects.count()} (нераспознано "
          f"{IntegrationEvent.objects.filter(status='unmatched').count()})")
        from billing.models import CashTransaction, CommissionAccrual, Payment, Payout
        from clients.models import Branch, ImplementerAccrual, Subscription
        s(f"  филиалы {Branch.objects.count()}, подписки {Subscription.objects.count()} "
          f"({', '.join(f'{k}: {v}' for k, v in sorted(dict(Subscription.objects.values_list('status').annotate(n=__import__('django.db.models', fromlist=['Count']).Count('id'))).items()))}), "
          f"оплаты {Payment.objects.count()}, начисления {CommissionAccrual.all_objects.count()}, выплаты "
          f"{Payout.all_objects.count()}, начисления внедренцам {ImplementerAccrual.objects.count()}, "
          f"проводки кассы {CashTransaction.objects.count()}")
        s(f"  тарифы {Tariff.objects.count()}, оборудование {Equipment.objects.count()}, правила вознаграждения "
          f"{CommissionRule.objects.count()}, удержаний {FranchiseePayment.objects.filter(method='offset', amount__gt=0).count()}")
        s(f"  курсы {Course.objects.count()}, назначения {Enrollment.objects.count()}, обращения "
          f"{Ticket.objects.count()}, проверки {Audit.objects.count()}, планы {SalesPlan.objects.count()}, "
          f"задачи {Task.objects.count()}")
        s(f"  вход: admin@, network@, curator@, finance@, buh@, quality@, barsbek@, mirlan@, support.osh@, dev@, osh@ (Standard), "
          f"bishkek@ (Growth), manager@ (менеджер, 2-я продажа), osh.manager@ozok.kg "
          f"(пароль {PASSWORD})")
        for p in Product.objects.all():
            s(f"  ключ интеграции {p.code}: {p.api_key}")






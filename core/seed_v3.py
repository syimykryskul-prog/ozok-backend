"""Демо-данные v3 (план «Демо-данные»): филиалы, второй филиал, подписки во всех статусах, оплаты из разных
источников, начисления и выплаты во всех статусах, установки OnAI, оборудование с серийниками, Касса, доступы."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from accounts.models import AccessRole, User
from billing import cash as cash_svc
from billing import accruals as accrual_svc
from billing import payments as pay_svc
from billing import payouts as payout_svc
from billing.models import (CashAccount, CashCategory, CashTransaction, CommissionAccrual, FranchiseePayment, Payment, Payout)
from clients import branches as branch_svc
from clients import deals as deal_svc
from clients import installations as inst_svc
from clients import subscriptions as sub_svc
from clients.models import (Branch, ChecklistTemplate, Client, ClientContact, ClientSale, ContractTemplate, Deal,
                            Implementation, Subscription)
from clients.services import refund_sale
from core.models import add_months, month_first, q2, today
from integrations.services import process_elqr_event
from network.models import Equipment, Franchisee, SetupSplitRule
from network.services import activate_contract

CHANNELS = ["whatsapp", "instagram", "facebook", "api_telephony"]


def at(d, hour=11):
    return timezone.make_aware(datetime.combine(d, time(hour, 0)))


class V3Seeder:
    def __init__(self, cmd):
        self.c = cmd
        self.rng = cmd.rng
        self.today = cmd.today
        self.n = 0
        self.fin = cmd.accountant
        self.fin2 = cmd.finance

    # --------------------------------------------------------------- helpers
    def uid(self):
        self.n += 1
        return self.n

    def new_client(self, code, name, btype="shop"):
        fr = self.c.frs[code]
        cl = Client.objects.create(franchisee=fr, business_name=name, business_type=btype, city=fr.city,
                                   region=fr.region, phone="0555 " + str(100000 + self.uid()),
                                   legal_name=f"ОсОО «{name.strip('«»')}»", inn=f"0{1000000000000 + self.uid()}")
        ClientContact.objects.create(client=cl, full_name=f"Контакт {name}", position="Директор",
                                     phone=cl.phone, email=f"c{self.n}@client.kg", is_primary=True)
        self.c.clients.append(cl)
        return cl

    def branch(self, client, product, tariff=None, elqr=None, **kw):
        main = branch_svc.ensure_main_branch(client)
        if main.product_id is None:
            main.product, main.tariff = product, tariff
            main.elqr_account = elqr or f"ELQR-{3000 + self.uid()}"
            main.save()
            return main
        b = Branch(client=client, name=kw.pop("name", f"Филиал {client.branches.count() + 1}"), city=client.city,
                   region=client.region, product=product, tariff=tariff, elqr_account=elqr or f"ELQR-{3000 + self.uid()}",
                   **kw)
        return b

    def pay(self, branch, amount, d, source="manual", months=1, ext=None, user=None, sub=None):
        p, _ = pay_svc.register_payment(
            branch=branch, subscription=sub, amount=amount, paid_at=at(d), source=source,
            external_id=ext or f"{source[:3]}-{self.uid():05d}", months=months, user=user or self.fin,
            check_authority=False, purpose="Оплата абонплаты (демо)")
        return p

    def deal(self, code, product, lines, *, days_ago=0, client=None, confirm=True):
        fr = self.c.frs[code]
        d = Deal(client=client, partner=fr, product=self.c.products[product], date=self.today - timedelta(days=days_ago),
                 created_by=self.c.owners[code], branch=branch_svc.ensure_main_branch(client))
        d, _ = deal_svc.save_deal(d, lines)
        return deal_svc.confirm_deal(d, self.c.owners[code]) if confirm else d

    # ------------------------------------------------------------------ run
    def run(self):
        self.catalog_setup()
        self.contracts()
        self.branches_and_second_branch()
        self.subscription_statuses()
        self.payments_all_sources()
        self.holds_and_payouts()
        self.installations()
        self.equipment()
        self.cash()
        self.access()
        sub_svc.update_all()

    def catalog_setup(self):
        c = self.c
        SetupSplitRule.objects.create(product=c.products["onai"], total=20000, partner_amount=12000,
                                      implementer_amount=8000, technical_channels=CHANNELS,
                                      valid_from=self.today - timedelta(days=400))
        for code, items in (("onai", ["Создать аккаунт OnAI", "Подключить WhatsApp", "Подключить Instagram",
                                       "Подключить Facebook", "Настроить API-телефонию", "Обучить сотрудников"]),
                            ("nur_retail", ["Установить программу", "Загрузить товары", "Настроить кассу",
                                            "Обучить кассиров"]),
                            ("coca", ["Установить программу", "Настроить меню", "Обучить официантов"])):
            ChecklistTemplate.objects.create(product=c.products[code], items=items)
        for kind, name in (("manager", "Договор менеджера (шаблон)"), ("franchisee", "Договор франчайзи (шаблон)"),
                           ("client", "Договор с клиентом (шаблон)")):
            ContractTemplate.objects.create(kind=kind, name=name)
        for eq in Equipment.objects.all():
            eq.purchase_price = (eq.price * Decimal("0.7")).quantize(Decimal("1"))
            eq.warranty_months = 12 if eq.price > 5000 else 6
            eq.min_price = eq.min_price or (eq.price * Decimal("0.85")).quantize(Decimal("1"))
            eq.save()
        for p in ("el_bilim", "kechikpe"):
            pr = c.products[p]
            pr.payment_source = "elqr"
            pr.save()
        onai = c.products["onai"]
        onai.payment_source, onai.grace_days, onai.restrict_after_days = "manual", 5, 30
        onai.save()
        for code in list(c.tariffs):
            for t in c.tariffs[code]:
                if code == "onai" and t.code == "pro":
                    t.grace_days = 10
                    t.save()
        for e in Implementation.objects.filter(deal__isnull=True):
            e.status = inst_svc.STAGE_STATUS.get(e.stage, "assigned")
            e.deadline = e.planned_launch
            e.save()

    def contracts(self):
        c = self.c
        for code in ("OSH-01", "BSK-01"):
            for what in ("onai", "el_bilim"):
                ct = c.make_contract(c.frs[code], what, "cash", 6, code)
                activate_contract(ct)
                for ch in ct.charges.all():
                    FranchiseePayment.objects.create(franchisee=c.frs[code], contract=ct, amount=ch.amount,
                                                     date=ch.due_date or self.today, method="bank",
                                                     created_by=self.fin, comment="Оплата: " + ch.description)
        for code in ("OSH-01", "BSK-01"):
            c.fr_products[code] = list(c.fr_products.get(code, [])) + [c.products["onai"], c.products["el_bilim"]]

    def branches_and_second_branch(self):
        c, rng = self.c, self.rng
        osh, bsk = c.frs["OSH-01"], c.frs["BSK-01"]
        # у части клиентов — лицевые счета Элкюар на основных филиалах и контакты
        n = 0
        for client in [x for x in c.clients if x.franchisee_id in (osh.id, bsk.id)][:14]:
            main = branch_svc.ensure_main_branch(client)
            if not main.elqr_account:
                main.elqr_account = f"ELQR-{1000 + client.id}"
                main.save()
            if not client.contacts.exists():
                ClientContact.objects.create(client=client, full_name=f"Контакт {client.business_name}",
                                             position="Владелец", phone=client.phone, is_primary=True)
            n += 1
        # случай 1: второй филиал сети переназначен (нет ответа партнёра 3 дня) -> BSK-01
        net = self.new_client("OSH-01", "Сеть кафе «Нур»", "cafe")
        b1 = self.branch(net, c.products["coca"], c.tariffs["coca"][0], "ELQR-NET-1")
        b1.save()
        self.pay(b1, 3500, self.today - timedelta(days=40), "manual", 1)
        b2 = Branch(client=net, name="Нур — Ош Север", city="Ош", region="Ош", product=c.products["coca"],
                    tariff=c.tariffs["coca"][0], elqr_account="ELQR-NET-2")
        branch_svc.setup_new_branch(b2, user=c.admin)
        b2.save()
        branch_svc.reassign(b2, bsk, reason_code="no_response", reason="Партнёр не ответил в течение 3 дней",
                            user=c.network, effective_from=self.today - timedelta(days=25))
        self.pay(Branch.objects.get(pk=b2.pk), 3500, self.today - timedelta(days=12), "manual", 1)
        # случай 2: третий филиал ждёт ответа приоритетного партнёра (ещё в пределах срока)
        b3 = Branch(client=net, name="Нур — Ош Центр", city="Ош", region="Ош", product=c.products["coca"],
                    tariff=c.tariffs["coca"][0], elqr_account="ELQR-NET-3")
        branch_svc.setup_new_branch(b3, user=c.admin)
        b3.save()
        # случай 3: филиал, который партнёр принял сам
        net2 = self.new_client("BSK-01", "Барбершоп «Усма» (сеть)", "services")
        n1 = self.branch(net2, c.products["kezek"], c.tariffs["kezek"][0], "ELQR-USMA-1")
        n1.save()
        n2 = Branch(client=net2, name="Усма — Восток", city="Бишкек", region="Бишкек", product=c.products["kezek"],
                    tariff=c.tariffs["kezek"][0], elqr_account="ELQR-USMA-2")
        branch_svc.setup_new_branch(n2, user=c.admin)
        n2.save()
        branch_svc.accept(n2, c.owners["BSK-01"])
        # лид OZOK у основного филиала
        from leads.models import Lead
        lead = Lead.objects.filter(source="nbs_ads", client__isnull=False).first()
        if lead is not None:
            Branch.objects.filter(client=lead.client, is_main=True).update(lead=lead, lead_source=lead.source)

    def subscription_statuses(self):
        c = self.c
        osh = c.frs["OSH-01"]
        prod, tariff = c.products["onai"], c.tariffs["onai"][0]
        cases = (("Статус: ожидает оплаты", None), ("Статус: активна", 5), ("Статус: льготный период", 33),
                 ("Статус: просрочена", 46), ("Статус: ограничена", 100), ("Статус: закрыта", 80))
        for name, back in cases:
            cl = self.new_client("OSH-01", f"«{name}»")
            b = self.branch(cl, prod, tariff)
            b.save()
            sub = Subscription.objects.create(client=cl, branch=b, product=prod, tariff=tariff, monthly_fee=3000,
                                              status="awaiting_payment")
            if back is not None:
                self.pay(b, 3000, self.today - timedelta(days=back), "manual", 1, sub=sub)
            if "закрыта" in name:
                sub.refresh_from_db()
                sub.status, sub.closed_at = "closed", self.today - timedelta(days=5)
                sub.save()
        # подписка по продаже, ожидающая оплаты (создана подтверждением продажи)
        cl = self.new_client("BSK-01", "Кофейня «Бодрое утро» (онлайн-продажа)", "cafe")
        self.deal("BSK-01", "coca", [{"kind": "subscription", "tariff": c.tariffs["coca"][0], "months": 3}],
                  client=cl, days_ago=2)

    def payments_all_sources(self):
        c = self.c
        osh = c.frs["OSH-01"]
        # Элкюар: el_bilim — источник elqr
        cl = self.new_client("OSH-01", "Школа №7", "services")
        b = self.branch(cl, c.products["el_bilim"], c.tariffs["el_bilim"][0], "ELQR-SCH-7")
        b.save()
        for i, back in enumerate((95, 65, 34, 4)):
            ev = {"event_id": f"elqr-seed-{i}", "type": "payment.succeeded", "account": "ELQR-SCH-7",
                  "operation_id": f"EQ-{9000 + i}", "amount": "2000", "paid_at": at(self.today - timedelta(days=back)).isoformat(),
                  "months": 1}
            process_elqr_event(ev)
        process_elqr_event({"event_id": "elqr-seed-1", "type": "payment.succeeded", "account": "ELQR-SCH-7",
                            "operation_id": "EQ-9001", "amount": "2000"})          # дубль
        process_elqr_event({"event_id": "elqr-seed-x", "type": "payment.succeeded", "account": "UNKNOWN-1",
                            "operation_id": "EQ-9100", "amount": "1500", "paid_at": at(self.today).isoformat()})
        # продукт (kechikpe — elqr, поэтому событие продукта только сверяет)
        cl2 = self.new_client("BSK-01", "ИП Ташиева (отсрочка)", "services")
        b2 = self.branch(cl2, c.products["kechikpe"], c.tariffs["kechikpe"][0], "ELQR-KCH-1")
        b2.save()
        process_elqr_event({"event_id": "elqr-seed-k", "type": "payment.succeeded", "account": "ELQR-KCH-1",
                            "operation_id": "EQ-9200", "amount": "1500", "paid_at": at(self.today - timedelta(days=9)).isoformat(),
                            "months": 1})
        sub2 = Subscription.objects.get(branch=b2)
        from integrations.services import process_event
        process_event(c.products["kechikpe"], {"event_id": "seed-recon-1", "type": "payment.succeeded",
                                               "external_payment_id": "EQ-9200", "external_client_id": "kch-ext-1",
                                               "amount": "1500", "months": 1,
                                               "paid_until": (self.today + timedelta(days=20)).isoformat()}) \
            if False else None
        # ручной ввод финансиста и импорт банковской выписки (onai — источник manual)
        cl3 = self.new_client("OSH-01", "Компания «Алтын Групп»", "wholesale")
        b3 = self.branch(cl3, c.products["onai"], c.tariffs["onai"][1], "ELQR-ALT-1")
        b3.save()
        self.pay(b3, 5000, self.today - timedelta(days=70), "manual", 1)
        csv = ("date,amount,purpose,operation_id,account\n"
               f"{self.today - timedelta(days=40)},10000,Абонплата OnAI,BANK-7001,ELQR-ALT-1\n"
               f"{self.today - timedelta(days=10)},5000,Абонплата OnAI,BANK-7002,ELQR-ALT-1\n"
               f"{self.today - timedelta(days=10)},5000,Абонплата OnAI,BANK-7002,ELQR-ALT-1\n"
               f"{self.today - timedelta(days=9)},1000,Неизвестный счёт,BANK-7003,NOPE-1\n")
        pay_svc.import_confirm(csv, self.fin, "vypiska-ноябрь.csv")

    def holds_and_payouts(self):
        c = self.c
        # приостановленный партнёр (BAT-01): удержанные начисления; одно освобождено, одно отменено
        bat = c.frs["BAT-01"]
        held = []
        for i, back in enumerate((60, 33, 5)):
            cl = self.new_client("BAT-01", f"Магазин «Баткен-{i + 1}»")
            b = self.branch(cl, c.products["nur_retail"], c.tariffs["nur_retail"][0])
            b.save()
            held.append(self.pay(b, 1800, self.today - timedelta(days=back), "product", 1))
        a0, a1 = (CommissionAccrual.objects.get(payment=p) for p in held[:2])
        if a0.status == "on_hold":
            accrual_svc.release(a0, c.finance)
        if a1.status == "on_hold":
            accrual_svc.cancel_hold(a1, c.finance, "Партнёр приостановлен: решение OZOK — не выплачивать")
        # выплаты: paid / verified / draft; после paid — возврат (сторно accrued, оплаченное остаётся paid)
        osh, bsk, kar = c.frs["OSH-01"], c.frs["BSK-01"], c.frs["KAR-01"]
        prev = month_first(self.today) - timedelta(days=1)
        for fr, final in ((osh, "paid"), (kar, "verified"), (bsk, "draft")):
            try:
                po = payout_svc.create(fr, prev if final != "draft" else self.today, c.finance if final == "draft"
                                       else c.accountant)
            except Exception:  # noqa: BLE001 — нет начислений к выплате
                continue
            if final in ("verified", "paid"):
                payout_svc.verify(po, c.finance)
            if final == "paid":
                payout_svc.pay(po, c.accountant, date=self.today - timedelta(days=4), payment_ref="ПП-COM-0001")
        paid = CommissionAccrual.objects.filter(status="paid", reversal_of__isnull=True,
                                                payment__source="manual").first()
        if paid is not None and paid.sale_id:
            refund_sale(paid.sale, q2(paid.sale.amount / 2), external_payment_id=f"seed-refund-{paid.id}")

    def installations(self):
        c = self.c
        dev = c.dev
        osh = c.frs["OSH-01"]
        names = ["Колл-центр «Асман»", "Магазин одежды «Стиль»", "Клиника «Дента Лайф»", "Школа английского «Bright»",
                 "Агентство «Тур-Ош»", "Студия «Лаванда»", "Автосалон «Тулпар»", "Фитнес «Атлет»"]
        stages = ["assigned", "in_progress", "training", "awaiting_act", "awaiting_act_channels", "done_accrued",
                  "done_verified", "done_paid"]
        for name, stage in zip(names, stages):
            cl = self.new_client("OSH-01", name, "services")
            d = self.deal("OSH-01", "onai", [{"kind": "setup"}, {"kind": "subscription", "tariff": c.tariffs["onai"][0],
                                                                   "months": 1}], client=cl,
                          days_ago=self.rng.randint(8, 40))
            inst = Implementation.objects.get(deal=d)
            inst.implementer = dev
            inst.save()
            if stage == "assigned":
                inst.deadline = self.today - timedelta(days=3)  # просрочена
                inst.save()
                continue
            inst_svc.set_status(inst, "in_progress")
            inst_svc.update_checklist(inst, [{"item": "Создать аккаунт OnAI", "done": True},
                                            {"item": "Подключить WhatsApp", "done": True}])
            inst_svc.add_comment(inst, dev, "Приступили к настройке каналов.")
            if stage == "in_progress":
                inst_svc.set_channel(inst, "whatsapp", "connected")
                continue
            inst_svc.set_status(inst, "training")
            inst_svc.set_channel(inst, "whatsapp", "connected")
            inst_svc.set_channel(inst, "instagram", "connected")
            if stage == "training":
                continue
            inst_svc.set_training_result(inst, "passed")
            inst_svc.set_status(inst, "awaiting_act")
            if stage == "awaiting_act":
                continue
            inst_svc.confirm_act(inst, confirmed_by="Директор клиента", act_file="https://files.ozok.kg/acts/demo.pdf")
            if stage == "awaiting_act_channels":
                inst_svc.set_channel(inst, "facebook", "failed")
                continue
            for ch in ("facebook", "api_telephony"):
                inst_svc.set_channel(inst, ch, "connected")
            inst_svc.rate(inst, 5)
            sub = Subscription.objects.filter(deal=d).first()
            if sub is not None:
                self.pay(sub.branch, 3000, self.today - timedelta(days=3), "manual", 1, sub=sub)
            inst_svc.set_status(inst, "done")
            acc = inst.implementer_accrual
            if stage in ("done_verified", "done_paid"):
                inst_svc.verify_accrual(acc, c.finance)
            if stage == "done_paid":
                inst_svc.pay_accrual(acc, c.accountant, "ПП-IMPL-001")

    def equipment(self):
        c = self.c
        eqs = list(Equipment.objects.all())
        statuses = ["ordered", "in_stock", "delivered", "installed", "installed"]
        for i, code in enumerate(("OSH-01", "BSK-01", "BSK-02", "KAR-01", "TOK-01")):
            cl = [x for x in c.clients if x.franchisee_id == c.frs[code].id]
            if not cl:
                continue
            eq = eqs[i % len(eqs)]
            qty = 2
            d = self.deal(code, "nur_retail", [{"kind": "equipment", "equipment": eq, "quantity": qty,
                                                 "serial_numbers": [f"SN-{eq.id}{i}{k:03d}" for k in range(qty)]}],
                          client=cl[0], days_ago=15 + i * 4)
            ClientSale.objects.filter(deal=d, kind="equipment").update(
                delivery_status=statuses[i], installed_by=c.dev if statuses[i] == "installed" else None)

    def cash(self):
        """Касса: счета открыты ДО первой проводки, расходы и переводы не превышают остаток (правило M-2)."""
        from django.db.models import Min
        c = self.c
        first = CashTransaction.objects.aggregate(m=Min("date"))["m"]
        start = min(self.today - timedelta(days=210), (first or self.today) - timedelta(days=1))
        for acc, bal in zip(CashAccount.objects.order_by("id"), (150000, 1500000, 100000)):
            acc.opening_balance, acc.opening_date = bal, start
            acc.save()
        accs = {a.kind: a for a in CashAccount.objects.all()}
        cats = {x.code: x for x in CashCategory.objects.all()}
        rng = self.rng
        manual = cash_svc.manual_entry

        def capped(acct, when, amount, share=0.6):
            room = int((cash_svc.balance(acct, on=when) - 1000) * Decimal(str(share)))
            return min(amount, room)

        for back in range(6, -1, -1):
            base = add_months(month_first(self.today), -back)

            def when(day):
                return min(base + timedelta(days=day), self.today)
            for code, lo, hi, acct in (("rent", 45000, 60000, "bank"), ("salary", 100000, 140000, "bank"),
                                       ("nbs_ads", 20000, 45000, "bank"), ("taxes", 10000, 25000, "bank"),
                                       ("equipment_purchase", 10000, 60000, "bank"), ("other_expense", 3000, 10000, "cash")):
                if back == 0 and when(20) > self.today:
                    continue
                d = when(rng.randint(2, 24))
                amount = capped(accs[acct], d, rng.randint(lo, hi), 0.8)
                if amount < 1000:
                    continue
                cash_svc.add_expense(user=c.finance, amount=amount, account=accs[acct],
                                     category_obj=cats[code], date=d,
                                     description=dict(rent="Аренда офиса", salary="Зарплата сотрудников",
                                                      nbs_ads="Реклама OZOK (Instagram, 2GIS)", taxes="Налоги",
                                                      equipment_purchase="Закупка оборудования у поставщика",
                                                      other_expense="Хозяйственные расходы")[code],
                                     supplier="Поставщик", attachment=f"https://files.ozok.kg/receipts/{code}-{back}.pdf")
            manual(user=c.finance, direction="in", amount=rng.randint(5000, 30000), account=accs["cash"],
                   category_obj=cats["other_income"], date=when(12), comment="Обучение сотрудников партнёров")
            for src, lo, hi, note, day in (("cash", 50000, 150000, "Инкассация", 15),
                                           ("elqr", 80000, 200000, "Вывод средств Элкюар на расчётный счёт", 18)):
                amount = capped(accs[src], when(day), rng.randint(lo, hi))
                if amount >= 1000:
                    manual(user=c.finance, direction="transfer", amount=amount, account=accs[src],
                           transfer_account=accs["bank"], date=when(day), comment=note)
        # одна ошибочная проводка отменена сторно (сторно — той же датой, что и исходная)
        wrong = manual(user=c.finance, direction="out", amount=1777, account=accs["cash"],
                       category_obj=cats["other_expense"], date=self.today - timedelta(days=3), comment="Ошибочный ввод")
        cash_svc.cancel_entry(wrong, c.finance, "Внесено дважды")

    def access(self):
        c = self.c
        base = AccessRole.objects.get(code="finance")
        role, _ = AccessRole.objects.get_or_create(
            code="custom-accountant-read", defaults=dict(
                name="Бухгалтер (только просмотр)", description="Касса и отчёты без права менять", is_system=False,
                base_role="finance", scope="all",
                permissions={k: ("read" if v == "write" else v) for k, v in base.permissions.items()}, actions=[]))
        u = User.objects.create_user(email="buh.read@ozok.kg", password="demo12345", first_name="Айдай",
                                     last_name="Султанова", role="finance", access_role=role)
        u.permission_overrides = {"permissions": {"audit_log": "read"}}
        u.save()
        c.stdout.write("  доступ: buh.read@ozok.kg (роль «Бухгалтер (только просмотр)» + доступ к журналу аудита)")
        User.objects.create_user(email="new.staff@ozok.kg", password="Temp-pass-123", first_name="Новый",
                                 last_name="Сотрудник", role="regional_support", region="Бишкек",
                                 must_change_password=True, invited_by=c.admin)
        c.stdout.write("  приглашён: new.staff@ozok.kg (временный пароль Temp-pass-123, нужна смена при входе)")

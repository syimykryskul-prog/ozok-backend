# OZOK Network — backend

Внутренняя система OZOK для управления сетью франчайзи. Django 5.2 + DRF + JWT.
Контракт API — `docs/TZ.md` (разделы 5, 5А, 5Б, 5В, 6, 10, 11).

## Установка

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo      # демо-данные; --reset пересоздаёт
.\.venv\Scripts\python.exe manage.py runserver      # http://127.0.0.1:8000
```

## Команды

- `manage.py test` — тесты (изоляция франчайзи, сценарий 11.3, договоры и цены, вознаграждение и зачёт per_sale,
  интеграция продуктов, SLA/просрочки, рейтинг, дашборды, права ролей)
- `manage.py seed_demo [--reset]` — 4 сферы + пакет, 12 франчайзи, ~120 заявок, ~80 клиентов, ~200 продаж, обращения, задачи…
- `manage.py simulate_integration --product market --count 10` — события `payment.succeeded` от имени продукта
- Swagger: `/api/docs/`, схема: `/api/schema/`, админка: `/admin/`

## Демо-входы (пароль `demo12345`)

`admin@ozok.kg` (hq_admin) · `curator@ozok.kg` (hq_curator) · `buh@ozok.kg` (hq_accountant) ·
`osh@ozok.kg` (fr_owner «OZOK Ош») · `osh.manager@ozok.kg` (fr_manager). Остальные франчайзи: `bishkek1@ozok.kg`,
`bishkek2@ozok.kg`, `osh2@ozok.kg`, `jalal@ozok.kg`, `karakol@ozok.kg`, `tokmok@ozok.kg`, `karabalta@ozok.kg`,
`batken@ozok.kg`. API-ключи интеграции продуктов сидер печатает в конце; также их видит hq_admin в `products/`.

## Структура

| Приложение | Что внутри |
|---|---|
| `core` | базовая модель, деньги/даты, пагинация, **`permissions.py` — единая таблица роль → ресурс**, `ScopedViewSet` (изоляция по франчайзи), `ScopedPK` (проверка видимости FK), фабрика фильтров, `seed_demo` |
| `accounts` | пользователь (email, роли hq_admin / hq_curator / hq_accountant / fr_owner / fr_manager, `franchisee`), auth, `users/` |
| `network` | Product (сфера, каталог, цены, ключ интеграции), Package, Franchisee (`partner_code`), Contract; `services.py` — прайс, активация договора |
| `leads` | Lead, публичный `leads/public/` (10/мин), автораспределение, воронка |
| `clients` | Client, ClientProductAccount, Implementation, ClientSale; `services.py` — вознаграждение и зачёт, `reports.py` — отчёт по продажам |
| `billing` | Charge, FranchiseePayment, CommissionPayout; `services.py` — баланс, просрочка, `commission_due` |
| `integrations` | IntegrationEvent, приём событий продуктов, привязка нераспознанных, симулятор |
| `training`, `quality`, `planning`, `tasks` | курсы и сертификаты; обращения (SLA) и проверки; план и рейтинг; задачи |
| `dashboard` | дашборды центра и франчайзи |

Все бизнес-правила — в `services.py` приложений (используются и API, и сидер, и тесты).

## Модель денег (5А, 5Г, 5Д)

- **Доход франчайзи** считают правила `CommissionRule` (`commission-rules/`): область (франчайзи / тариф / сфера / вид),
  процент и/или фикс (за продажу или за оплаченный месяц), база (оплачено или прайс), срок действия. Выбирается самое
  конкретное правило (franchisee +4, tariff +2, product +1, sale_kind +1; при равенстве — позднее `valid_from`, затем
  больший id). Продажа хранит снимок (`commission_rule/percent/fixed/base_amount`, `franchisee_income`); правки правил
  прошлое не меняют — пересчёт `client-sales/{id}/recalculate/`, ручной доход — `franchisee_income` (флаг
  `income_overridden`). `commission-rules/preview/` — «сколько получит франчайзи». По умолчанию: абонплата 20%,
  установка/оборудование/прочее 100%.
- Виды продаж: `setup`, `equipment`, `subscription`, `renewal`, `other`. Франчайзи вносит вручную setup/equipment/other
  (оборудование — из справочника `equipment/`, сумма = цена × количество); абонплата приходит из продуктов.
- **Тарифы** `tariffs/` с историей цен (`TariffPrice`, `tariffs/{id}/set-price/`): цена на дату оплаты не переписывается.
- **Франшиза — составная оплата**: сразу (`upfront_amount`) + рассрочка (`installments_total/count/step_months`) + остаток,
  который гасится **удержанием** из дохода (`withhold_percent`, по умолчанию 30%). `payment_scheme` — пресет
  (cash / prepay / per_sale / hybrid). Начисления строятся из частей; остаток — начисление без срока.
- **Удержание**: с каждой продажи по сфере договора `withheld = franchisee_income × withhold_percent / 100`, не больше
  остатка. Зачёт `FranchiseePayment(offset)`; для абонплаты ещё `CommissionPayout(offset)`, для остальных видов —
  `Charge(kind=withhold, срок +30 дней)`. Возврат и удаление продажи снимают удержание пропорционально.
- Два баланса: `balance` = Σ начислений − Σ платежей; `commission_due` = Σ commission_amount (абонплата) − Σ выплат.
- Отчёт `reports/sales/`: income / withheld / net_income, `by_kind`; дашборды — `income_month`, `withheld_month`
  (франчайзи), `network_income_month`, `withheld_month` (центр).
## Интеграция продуктов (5Б)

Продукт шлёт события на `POST /api/v1/integrations/events/` с заголовком `X-OZOK-Key: <api_key>` (401 при неверном
ключе или выключенной интеграции). Тело — одно событие или массив до 100. Формат события (плоский JSON):

```json
{"event_id": "e-1", "type": "client.upsert", "external_client_id": "42", "partner_code": "OSH-01",
 "business_name": "Магазин «Береке»", "business_type": "shop", "phone": "0555 123 456", "city": "Ош"}
{"event_id": "e-2", "type": "payment.succeeded", "external_payment_id": "p-1", "external_client_id": "42",
 "amount": "7500", "months": 3, "paid_until": "2027-02-12", "paid_at": "2026-11-12T10:00:00+06:00"}
{"event_id": "e-3", "type": "payment.refunded", "external_payment_id": "p-1", "amount": "2500"}
```

Ответ: `{"results": [{"event_id", "status": "processed|duplicate|unmatched|error", "error"}]}`. Поле типа — `type`
(допустим и `event_type`). Идемпотентность: повтор `event_id` или `external_payment_id` — `duplicate`. Неизвестный
партнёр/клиент/платёж — `unmatched`; центр привязывает: `POST integration-events/{id}/resolve/ {franchisee, client?}`.
`GET integrations/ping/` — проверка ключа. `POST integrations/test-event/ {product}` (hq_admin) — тестовая оплата.
`products/{id}/rotate-key/` — перевыпуск ключа. Вебхуков и онлайн-оплаты в системе нет: клиенты платят внутри продуктов.

## Допущения (где ТЗ неоднозначно)

См. итоговый отчёт разработки; кратко: рейтинг без плана считает вклад плана 0; «открытые» обращения для
`sla_breached` на дашборде — только `open/in_progress`; `reports/rating/` — только центр; у франчайзи `month` в
`sales-plans` отдаётся как `YYYY-MM-01`, принимается `YYYY-MM`.


# OZOK Network — Техническое задание v2.0 (MVP)

Дата: 11.11.2026 · Статус: черновик для правок заказчика
Заменяет ТЗ v1.0 (ERP для магазина) — направление изменено заказчиком.

## 1. Назначение

OZOK — IT-центр с партнёрской сетью франчайзи в Кыргызстане. Франчайзи продают и внедряют четыре продукта OZOK (NUR CRM Market, NUR CRM Опт, КЕЗЕК, COCA) малому и среднему бизнесу в своих городах.

**OZOK Network** — внутренняя система, в которой:
- **центральный офис** видит всю сеть: франчайзи, договоры, заявки, клиентов, деньги, обучение, качество, планы и рейтинг; ставит задачи и контролирует их выполнение;
- **каждый франчайзи** работает в своём кабинете: получает заявки, ведёт клиентов и внедрения, видит свой долг перед центром, план, задачи, обучение сотрудников и обращения клиентов.

Принцип брендбука: «Один технологический центр. Сильные партнёры на местах. Общий стандарт сервиса».

## 2. Состав системы

| Часть | Стек | Папка | Порт (dev) |
|---|---|---|---|
| Backend API | Python 3.13, Django 5.2, DRF, simplejwt, django-filter, django-cors-headers, drf-spectacular, SQLite (dev) / PostgreSQL (prod) | `backend/` | 8000 |
| Веб-кабинет | React 18 + TypeScript, Vite, Redux Toolkit + RTK Query, React Router 6, Recharts, lucide-react | `web/` | 5173 |
| Мобильное приложение | Flutter 3, flutter_riverpod, dio, go_router, flutter_secure_storage | `mobile/` | 8080 (web-server при отладке) |

## 3. Пользователи, роли и видимость данных

Пользователь либо сотрудник **центра** (`franchisee = null`), либо сотрудник **франчайзи** (`franchisee = <id>`).

| Роль | Код | Кто | Доступ |
|---|---|---|---|
| Администратор центра | `hq_admin` | руководство OZOK | всё, включая пользователей, продукты, настройки |
| Куратор | `hq_curator` | менеджер центра, ведёт группу франчайзи | всё по сети на чтение; запись: заявки, задачи, проверки качества, планы, обучение; по «своим» франчайзи (где он куратор) — и по всем, если `hq_admin` |
| Бухгалтер центра | `hq_accountant` | финансы центра | договоры (чтение), начисления и платежи франчайзи (запись), отчёты |
| Владелец франчайзи | `fr_owner` | партнёр | всё в пределах своего франчайзи: заявки, клиенты, продажи, свои сотрудники, обращения, задачи (выполнение), обучение (прохождение), свой баланс (чтение) |
| Менеджер франчайзи | `fr_manager` | сотрудник партнёра | заявки, клиенты, внедрения, продажи, обращения, задачи (выполнение), обучение (прохождение) своего франчайзи |

**Правило изоляции:** пользователь франчайзи видит и меняет только объекты своего франчайзи; `franchisee` для его записей проставляется сервером автоматически. Сотрудники центра видят всё; при создании объектов указывают `franchisee` явно.

Куратор в MVP читает всю сеть, а пишет — по всем франчайзи (ограничение «только свои» — фильтр `?curator=me` в списках, не право). Это упрощение фиксируем.

## 4. Модули MVP

1. **Авторизация** — email + пароль, JWT (access 30 мин, refresh 7 дней), профиль.
2. **Дашборд** — разный для центра и франчайзи (раздел 6.3).
3. **Франчайзи** — партнёры, их города, статус, куратор, сотрудники.
4. **Договоры и продукты** — какие продукты куплены, схема оплаты, сумма взноса, процент отчислений.
5. **Заявки** — заявки с сайта и вручную, автоматическое распределение по городу, воронка.
6. **Клиенты и внедрения** — бизнесы, которым франчайзи продал продукт; этапы внедрения; подписки и продления; продажи клиентам.
7. **Расчёты с франчайзи** — начисления (взнос, рассрочка, отчисления с продаж, прочее) и платежи; баланс и просрочка.
8. **Обучение и аттестация** — курсы по продуктам, назначение сотрудникам, результат, срок действия сертификата.
9. **Качество сервиса** — обращения клиентов (SLA, оценка) и проверки франчайзи по чек-листу стандартов.
10. **План и рейтинг** — месячный план для каждого франчайзи, факт, рейтинг сети.
11. **Задачи от центра** — постановка, сроки, комментарии, контроль выполнения.
12. **Настройки** — пользователи, продукты и цены (только `hq_admin`).

Вне MVP: подписание договоров онлайн, загрузка файлов, интеграция с продуктами (автоматический учёт продаж), уведомления в Telegram/WhatsApp, кыргызский язык интерфейса, карта сети.

## 5. Модель данных

Все модели: `created_at`, `updated_at`. Деньги — `Decimal(14,2)`, в JSON строкой (`"35000.00"`). Валюта — сом (KGS), одна на систему. Часовой пояс `Asia/Bishkek`.

- **Product**: code (`market`|`opt`|`kezek`|`coca`, unique), name, franchise_fee (взнос), description, is_active. Сидер: NUR CRM Market 35 000, NUR CRM Опт 26 000, КЕЗЕК 22 000, COCA 44 000.
- **Franchisee**: name (бренд/название партнёра, напр. «OZOK Ош»), legal_name, inn, city (основной), cities (JSON-список городов покрытия, по ним распределяются заявки), address, phone, email, owner_name, status (`candidate`|`onboarding`|`active`|`suspended`|`terminated`), curator (FK User центра, nullable), joined_at (date, nullable), note.
- **User** (кастомный, логин по email): email, first_name, last_name, phone, role, franchisee (nullable FK), is_active.
- **Contract**: franchisee, number (авто `D-2026-001`), signed_at, products (M2M Product), package (`single`|`bundle`), payment_scheme (`cash`|`prepay`|`per_sale`), fee_total (сумма взноса по договору), prepay_percent (для `prepay`, по умолчанию 40), installments (кол-во платежей рассрочки остатка, для `prepay`), deduction_percent (для `per_sale`: % с каждой продажи клиенту, пока взнос не погашен), royalty_percent (постоянные отчисления с продаж клиентам после погашения взноса, по умолчанию 0), status (`draft`|`active`|`closed`), valid_until (nullable), note.
  - Ценовые ориентиры из прототипа: пакет за наличные 87 000; предоплата 40% со скидкой 30% — 89 000; оплата с каждой продажи — 127 000. Система не навязывает цену: fee_total вводится в договоре, интерфейс предлагает расчёт по выбранным продуктам и схеме.
- **Lead** (заявка): source (`site`|`phone`|`referral`|`other`), interest (`program`|`franchise`|`consult`), product (`market`|`opt`|`kezek`|`coca`|`bundle`|`help`|`` ), name, phone, city, comment, status (`new`|`assigned`|`in_progress`|`demo`|`won`|`lost`), franchisee (nullable), assigned_at, taken_at (когда франчайзи взял в работу), closed_at, lost_reason, client (nullable FK Client, при `won`).
- **Client**: franchisee, business_name, business_type (`shop`|`wholesale`|`services`|`cafe`|`other`), contact_name, phone, city, address, lead (nullable), note, is_active.
- **Implementation** (внедрение): client, product, stage (`contract`|`setup`|`training`|`launched`|`cancelled`), started_at, planned_launch, launched_at, responsible (FK User франчайзи, nullable), note. `franchisee` берётся из клиента.
- **ClientSale** (продажа клиенту): client, product, kind (`license`|`subscription`|`renewal`|`service`), date, amount, paid_until (для подписки/продления, nullable), comment. При создании автоматически формирует **начисление** франчайзи (см. правила).
- **Charge** (начисление франчайзи): franchisee, contract (nullable), kind (`fee`|`installment`|`deduction`|`royalty`|`other`), amount, due_date, description, client_sale (nullable).
- **FranchiseePayment**: franchisee, amount, date, method (`cash`|`bank`), comment, created_by.
- **Course**: title, product (nullable — общий курс), description, materials_url, pass_score (по умолчанию 80), validity_months (по умолчанию 12), is_active.
- **Enrollment**: course, user (сотрудник франчайзи), status (`assigned`|`in_progress`|`passed`|`failed`), score (0–100, nullable), assigned_at, completed_at, expires_at (= completed_at + validity, если passed). Уникально course+user.
- **Ticket** (обращение клиента): franchisee (из клиента), client, subject, description, priority (`low`|`normal`|`high`|`critical`), status (`open`|`in_progress`|`resolved`|`closed`), opened_at, first_response_at, resolved_at, rating (1–5, nullable), assignee (nullable User). Вычисляемое: sla_breached.
- **Audit** (проверка по стандартам): franchisee, date, inspector (User центра), items (JSON: `[{"criterion": "...", "ok": true, "comment": ""}]`), score (% выполненных критериев, вычисляется), summary.
- **SalesPlan**: franchisee, month (date, 1-е число), target_clients (новые клиенты), target_revenue (сумма продаж клиентам). Уникально franchisee+month.
- **Task**: franchisee, title, description, priority (`low`|`normal`|`high`), status (`new`|`in_progress`|`done`|`rejected`|`cancelled`), due_date, created_by (центр), assignee (nullable User франчайзи), done_at. **TaskComment**: task, author, text, created_at.

### Бизнес-правила

**Заявки**
- Публичная заявка с сайта (`POST leads/public/`, без авторизации, ограничение 10/мин с IP) создаётся со `source=site`.
- Автораспределение при создании: если `interest = franchise` — заявка остаётся в центре (`franchisee = null`, статус `new`), это кандидат во франчайзи. Иначе ищется франчайзи со статусом `active`, у которого город заявки есть в `cities` (без учёта регистра); если найден один — назначается (`assigned`, `assigned_at`); если несколько — тот, у кого меньше открытых заявок; если ни одного — остаётся в центре `new`.
- Центр может переназначить заявку любому франчайзи (`assign`).
- Франчайзи: `take` (assigned → in_progress, taken_at), `demo`, `win` (создаёт Client из заявки, заявка → won), `lose` (обязателен lost_reason).
- Просроченная заявка: `assigned` и не взята в работу дольше 24 часов — флаг `overdue` в ответе.

**Клиенты и продажи**
- Продажа клиенту (`ClientSale`) формирует начисление по активному договору франчайзи, содержащему этот продукт:
  - схема `per_sale` и взнос ещё не погашен: начисление `deduction` = amount × deduction_percent / 100, но не больше остатка непогашенного взноса;
  - иначе, если royalty_percent > 0: начисление `royalty` = amount × royalty_percent / 100;
  - нет активного договора с продуктом — продажа сохраняется, начисления нет, в ответе предупреждение `warning`.
  - «Погашенный взнос» для `per_sale` = Σ начислений `deduction` по договору ≥ fee_total.
- Подписка просрочена, если `paid_until < сегодня`; клиент «на продление», если `paid_until` в ближайшие 14 дней.

**Договоры и расчёты**
- Активация договора (`activate`, draft → active) формирует начисления:
  - `cash`: одно `fee` на fee_total, срок = signed_at;
  - `prepay`: `fee` на fee_total × prepay_percent/100 (срок signed_at) и `installments` равных `installment` на остаток с шагом 1 месяц (последний платёж забирает копейки округления);
  - `per_sale`: начислений нет (возникают из продаж).
- Баланс франчайзи = Σ начислений − Σ платежей (положительный — франчайзи должен центру).
- Просрочка = Σ начислений с `due_date < сегодня` − Σ платежей, если > 0.
- У франчайзи может быть только один `active` договор на каждый продукт.

**Обучение**
- Результат (`complete` с score): score ≥ pass_score → passed, expires_at = completed_at + validity_months; иначе failed (можно назначить повторно → assigned).
- Сертификат истёк, если expires_at < сегодня.
- Доля обученных франчайзи = сотрудники с действующим passed хотя бы по одному курсу / все активные сотрудники франчайзи.

**Качество**
- SLA первого ответа: critical 1 ч, high 4 ч, normal 24 ч, low 72 ч. `sla_breached` = first_response_at (или «сейчас», если ответа нет) − opened_at > норматива.
- Перевод в `in_progress` впервые проставляет first_response_at; `resolved` — resolved_at. Оценку (rating) можно поставить только у resolved/closed.
- score проверки = round(100 × ok / всего критериев). Стандартный чек-лист (сидер, редактируемый на клиенте): «Вывеска и материалы по брендбуку», «Ответ на заявку в течение 24 часов», «Демо по стандарту OZOK», «Сотрудники прошли обучение», «Клиенты получают сопровождение после запуска», «Отчётность сдаётся вовремя».

**План и рейтинг** (за месяц, по каждому активному франчайзи)
- Факт: new_clients = клиенты, созданные в месяце; revenue = Σ ClientSale.amount в месяце.
- plan_percent = среднее из (new_clients/target_clients, revenue/target_revenue), каждая часть ограничена 1.2; если плана нет — null.
- Рейтинг (0–100) = 40% plan_percent (ограничен 1.0) + 20% средняя оценка обращений за 90 дней (rating/5; нет оценок — 0.8) + 15% последняя проверка (score/100; нет — 0.7) + 15% доля обученных + 10% платёжная дисциплина (нет просрочки — 1, иначе max(0, 1 − просрочка/fee_total активных договоров)). Округление до целого. Место в сети — по убыванию рейтинга.

**Задачи**
- Создаёт центр; франчайзи переводит new → in_progress → done (или rejected с комментарием). Центр может cancel. `overdue` = due_date < сегодня и статус не done/cancelled/rejected.

## 5А. Вознаграждение франчайзи и продажи по сферам (изменение 12.11.2026 — приоритетнее раздела 5)

Заказчик уточнил денежную модель: **франчайзи получает процент (20% или 30%) от абонентской платы** своих клиентов.

**Сферы** — это продукты: `market` → «Магазины» (NUR CRM Market), `opt` → «Оптовый бизнес» (NUR CRM Опт), `kezek` → «Сфера услуг» (КЕЗЕК), `coca` → «Кафе» (COCA). У Product добавить поле `sphere` (строка-подпись).

**Изменения модели:**
- `Contract`: удалить `deduction_percent` и `royalty_percent`; добавить `commission_percent` (Decimal(5,2), по умолчанию 20; допустимые значения в MVP — 20 или 30; меняют hq_admin/hq_accountant).
- `ClientSale`: добавить `commission_percent` (снимок ставки на момент продажи) и `commission_amount` (= amount × commission_percent / 100, округление до 0.01 half-up). Вознаграждение начисляется только для kind `subscription` и `renewal` (абонентская плата); для `license` и `service` — 0. Ставка берётся из активного договора франчайзи с этим продуктом; нет договора — 0 и `warning`. hq_admin/hq_accountant могут изменить `commission_percent` у конкретной продажи (пересчитывается commission_amount) — это место под «определённые условия» до их формализации.
- Продажа клиенту больше **не создаёт** Charge (`deduction`/`royalty` удалены из kind начисления; остаются `fee`, `installment`, `other`).
- Новая модель **CommissionPayout** (выплата вознаграждения центром франчайзи): franchisee, amount, date, method (`cash`|`bank`|`offset`), comment, created_by. Ресурс `commission-payouts/`, фильтры date_from/date_to/method, пишут hq_admin, hq_accountant; франчайзи — чтение своих.
- Схема `per_sale` («Оплата с каждой продажи»): при активации создаётся одно начисление `fee` на fee_total **без срока** (due_date = null, не считается просрочкой). Каждая продажа с вознаграждением, пока взнос не погашен, автоматически создаёт пару записей: `CommissionPayout(method=offset)` и `FranchiseePayment(method=offset)` на сумму min(commission_amount, остаток взноса) — вознаграждение идёт в зачёт взноса. `FranchiseePayment.method` расширить значением `offset`.
- Два баланса франчайзи:
  - `balance` (долг франчайзи центру) = Σ Charge − Σ FranchiseePayment;
  - `commission_due` (долг центра франчайзи) = Σ commission_amount − Σ CommissionPayout.

**Видимость продаж:** франчайзи видит все свои продажи и разбивку по сферам. Центр: выбран франчайзи → только его продажи; выбрана сфера без франчайзи → вся сеть по этой сфере; ничего не выбрано → вся сеть с разбивкой по сферам и по франчайзи.

**API (дополнения к разделу 6):**
- `client-sales/`: в ответе ещё `product_code`, `sphere`, `commission_percent`, `commission_amount`; фильтры `franchisee` (центр), `product` (код сферы), `kind`, `date_from`, `date_to`.
- `GET reports/sales/?date_from=&date_to=&franchisee=&product=` (по умолчанию текущий месяц; франчайзи — всегда только свои):
```json
{
  "period": {"date_from": "2026-11-01", "date_to": "2026-11-30"},
  "filters": {"franchisee": null, "product": null},
  "totals": {"count": 54, "amount": "412000.00", "subscription_amount": "300000.00", "commission": "72000.00"},
  "by_product": [{"product": "market", "sphere": "Магазины", "name": "NUR CRM Market", "count": 20, "amount": "150000.00", "subscription_amount": "110000.00", "commission": "26000.00"}],
  "by_franchisee": [{"franchisee_id": 1, "name": "OZOK Ош", "count": 9, "amount": "64000.00", "commission": "12800.00"}],
  "by_month": [{"month": "2026-06", "amount": "50000.00", "commission": "11000.00"}]
}
```
`by_product` — всегда все 4 сферы (нули, если продаж нет); `by_franchisee` — только для центра (у франчайзи пустой список); `by_month` — 6 месяцев, заканчивая месяцем `date_to`.
- `franchisees/` и `franchisees/{id}/summary/`: добавить `commission_due`, `commission_month`, `sales_by_product` (как by_product за текущий месяц).
- `dashboard/` франчайзи: добавить `commission_month`, `commission_due`, `sales_by_product`. Дашборд центра: добавить `commission_month` (сумма по сети) и `sales_by_product`.

**Веб/мобилка:**
- Раздел **«Продажи»** для всех ролей: вкладки сфер (Все · Магазины · Опт · Услуги · Кафе), выбор периода; у центра — ещё выбор франчайзи («Вся сеть» по умолчанию). Карточки по сферам (сумма, абонплата, вознаграждение, кол-во), график по месяцам, у центра — таблица по франчайзи, ниже список продаж с колонками ставки и вознаграждения.
- Раздел **«Расчёты»** показывает оба баланса: «Вы должны центру» / «Центр должен вам» (у центра — по каждому франчайзи), начисления, платежи, выплаты вознаграждения.

## 5Б. Интеграция с продуктами (изменение 12.11.2026 — приоритетнее разделов 5 и 5А; заменяет прежний «Кабинет клиента»)

**OZOK Network — только внутренняя ERP центра и франчайзи.** Клиенты в неё не входят и не платят через неё. Клиенты платят абонентскую плату **внутри самих продуктов** (NUR CRM Market, NUR CRM Опт, КЕЗЕК, COCA и будущие сферы). Каждый продукт интегрируется с OZOK Network и передаёт: каких клиентов подключил франчайзи, кто на сколько месяцев и на какую сумму оплатил, до какой даты оплачено. По этим данным система считает продажи, вознаграждение франчайзи (20/30%), продления и рейтинг.

**Модели:**
- `Franchisee.partner_code` — уникальный код партнёра (напр. `OSH-01`), его франчайзи указывает в админке продукта при подключении клиента; по нему продукт сообщает, чей это клиент.
- `Product`: добавить `integration_enabled` (bool), `api_key` (генерируется системой, показывается hq_admin один раз/с кнопкой «Перевыпустить»), `last_event_at` (только чтение).
- `Client`: добавить `external_ids` — через модель **ClientProductAccount**: client, product, external_id (id клиента в продукте), status (`active`|`blocked`|`closed`), paid_until (date, nullable), monthly_fee (nullable), connected_at. Уникально product+external_id. «На продление»/«просрочено» у клиента считаются по `paid_until` этих аккаунтов.
- `ClientSale`: добавить `source` (`integration`|`manual`), `external_payment_id` (уникально в рамках продукта, nullable), `months` (nullable), `paid_until` (nullable). Продажи из интеграции создаются только системой; вручную франчайзи вносит только `license`/`service`, центр — любые корректировки.
- **IntegrationEvent** (журнал): product, event_type, external_id события, payload (JSON), status (`processed`|`duplicate`|`unmatched`|`error`), error, client (nullable), franchisee (nullable), received_at, processed_at.

**API для продуктов** (вне JWT; заголовок `X-OZOK-Key: <api_key продукта>`; неверный ключ или выключенная интеграция — 401):
- `POST integrations/events/` — одно событие или массив до 100. Типы:
  - `client.upsert`: `{event_id, external_client_id, partner_code, business_name, business_type?, contact_name?, phone?, city?, status?, connected_at?}` — создаёт/обновляет Client (франчайзи по partner_code) и ClientProductAccount;
  - `payment.succeeded`: `{event_id, external_payment_id, external_client_id, amount, months, paid_until, paid_at}` — создаёт ClientSale (kind = `subscription` для первой оплаты этого аккаунта, иначе `renewal`, source=`integration`), обновляет paid_until аккаунта, начисляет вознаграждение по 5А; при схеме per_sale — зачёт в погашение взноса;
  - `payment.refunded`: `{event_id, external_payment_id, amount}` — создаёт отрицательную ClientSale-корректировку (kind=`renewal`, amount<0, вознаграждение тоже отрицательное).
  - Ответ: `{"results": [{"event_id": "...", "status": "processed|duplicate|unmatched|error", "error": null}]}`.
- **Идемпотентность**: повтор с тем же event_id или external_payment_id → `duplicate`, без изменений.
- **Не распознано**: partner_code не найден или клиент неизвестен — событие сохраняется со статусом `unmatched`; центр в разделе «Интеграции» привязывает его к франчайзи/клиенту и нажимает «Обработать» (`POST integration-events/{id}/resolve/ {franchisee, client?}`), после чего событие обрабатывается как обычно.
- `GET integrations/ping/` — проверка ключа: `{product: code, ok: true}`.

**API для кабинета:** `integration-events/` (только центр; фильтры product, status, event_type, date_from, date_to), `client-accounts/` (ClientProductAccount; фильтры product, status, renewal_soon, expired, client; франчайзи — свои), `products/{id}/rotate-key/` (hq_admin). В ответе `clients/` — `accounts[]` (product, product_name, external_id, status, paid_until).

**Экраны:**
- «Настройки → Интеграции» (hq_admin): по каждой сфере — вкл/выкл, API-ключ (показать/перевыпустить), время последнего события, счётчики за 24 ч (обработано/дубликаты/нераспознано/ошибки), документация формата событий прямо на странице, кнопка «Отправить тестовое событие» (симулирует оплату демо-клиента — для проверки без реального продукта).
- «Интеграции → Журнал»: таблица событий, фильтр «Нераспознанные», привязка и обработка.
- У франчайзи: в карточке клиента — аккаунты по продуктам с «Оплачено до», история оплат из продуктов; в «Продажах» — видно, что пришло из продукта (иконка «из NUR CRM Market»).
- Демо-данные: все продажи-абонплаты в сидере создаются через тот же обработчик событий (`payment.succeeded`), плюс несколько `unmatched` событий в журнале. Management-команда `simulate_integration --product <code> --count N` генерирует события для проверки.

**Дашборды:** центр — `subscriptions: {active, renewal_soon, expired}`, `unmatched_events`; франчайзи — `subscriptions` по своим клиентам.

Роль `client`, модели Subscription/ClientInvoice/ClientPayment, кнопка «Оплатить» и демо-оплата — **отменены**.

## 5В. Каталог сфер и покупка франшизы (изменение 12.11.2026 — приоритетнее разделов 5, 5А, 5Б)

**Сферы/продукты полностью управляются центром через систему** — никаких зашитых в код списков `market|opt|kezek|coca` ни на backend, ни в web/mobile. Вкладки сфер, фильтры, карточки, прайс строятся из `products/`.

- **Product** (= сфера): code (slug, unique, задаётся при создании, латиница), name («NUR CRM Market»), sphere («Магазины»), description, icon (имя иконки lucide из фиксированного набора: `shopping-cart`, `truck`, `calendar-check`, `coffee`, `store`, `briefcase`, `scissors`, `stethoscope`, `graduation-cap`, `car`, `box`), color (hex, для графиков; по умолчанию из палитры), **price_cash** (цена франшизы при оплате сразу), **price_per_sale** (цена при оплате постепенно из продаж — обычно выше), **default_commission_percent** (по умолчанию 20), sort_order, is_active. Удалить продукт с договорами/продажами нельзя — только деактивировать. CRUD — только hq_admin; экран «Настройки → Сферы и цены».
- **Package** (пакет нескольких сфер): name («Пакет из четырёх продуктов»), products (M2M), price_cash, price_prepay (с предоплатой), price_per_sale, prepay_percent (по умолчанию 40), is_active. CRUD — hq_admin. Ресурс `packages/`.
- Сидер: 4 сферы (NUR CRM Market — Магазины, cash 35 000 / per_sale 50 000; NUR CRM Опт — Оптовый бизнес, 26 000 / 37 000; КЕЗЕК — Сфера услуг, 22 000 / 32 000; COCA — Кафе, 44 000 / 63 000) и пакет «Пакет из четырёх продуктов» (cash 87 000, prepay 89 000, per_sale 127 000).

**Покупка франшизы франчайзи** — через договор (`Contract`), каждый договор — либо **одна сфера**, либо **пакет**:
- `Contract`: добавить `product` (nullable FK — одна сфера) и `package` (nullable FK); ровно одно из двух обязательно. Поле `products` (M2M) заполняется автоматически (сфера или все сферы пакета) — оно используется для проверки «одна активная франшиза на сферу» и поиска ставки вознаграждения. Поле `package` (single/bundle) из раздела 5 удалить.
- `fee_total` по умолчанию подставляется из прайса по схеме: `cash` → price_cash, `prepay` → package.price_prepay (для одной сферы prepay недоступен, если не задан), `per_sale` → price_per_sale. Центр может изменить сумму (скидка) — в договоре хранится итоговая сумма и `list_price` (прайсовая, для истории).
- `commission_percent` по умолчанию = product.default_commission_percent (для пакета — 20), центр может изменить.
- **Схемы:** «Наличными сразу» (`cash`) — одно начисление на всю сумму; «Предоплата + рассрочка» (`prepay`); «Постепенно из продаж» (`per_sale`) — франчайзи начинает работать без денег, вознаграждение с продаж идёт в зачёт взноса до полного погашения (раздел 5А). Франчайзи видит в Расчётах прогресс погашения: «Погашено 42 000 из 127 000 сом» по каждому договору.
- Франчайзи может **докупать сферы по одной в любой момент** — новый договор на новую сферу (с любой схемой). Если сфера уже есть в активном договоре — 400.
- Франчайзи в своём кабинете видит **витрину сфер** («Сферы OZOK»): все активные сферы и пакеты с ценами по схемам; у уже купленных — метка «Подключено». Кнопка «Хочу подключить» создаёт заявку в центр (`Lead` с interest=`franchise`, product = code сферы, franchisee = он сам, source=`other`) — центр оформляет договор.
- Валюта всех цен — сом. (Если цены нужно вести в долларах — открытый вопрос.)

**API:** `products/` (поля выше; все авторизованные читают, пишет hq_admin), `packages/` (то же), `contracts/` — в ответе `product`, `product_name`, `package`, `package_name`, `list_price`, `paid_off_amount`, `remaining_amount`; `GET contracts/price-quote/?product=|package=&scheme=` → `{list_price, prepay_percent?, installments_hint?}`; `POST products/{id}/request/` или `packages/{id}/request/` (франчайзи) → создаёт Lead, ответ `{lead_id}`. `Lead.product` — строка с code сферы или `package:<id>`, `help`, пусто.

## 5Г. Доходы франчайзи: установка, оборудование, абонплата (изменение 13.11.2026 — приоритетнее 5А/5Б/5В там, где противоречит)

Заказчик уточнил: у каждого продукта центр задаёт в системе **стоимость франшизы, стоимость установки и обучения, абонентскую плату и оборудование**. Франчайзи зарабатывает:
- **установка и обучение** — 100% дохода остаётся франчайзи;
- **оборудование** — 100% дохода остаётся франчайзи;
- **абонентская плата** — франчайзи получает 20% или 30% (ставка в договоре; приходит из интеграции продукта, раздел 5Б).

Франчайзи **сам вносит в систему** продажи установки/обучения и оборудования, чтобы центр видел, сколько он продал и сколько заработал. Если франшиза не оплачена (схема «Постепенно из продаж»), центр **по договорённости удерживает часть дохода франчайзи** в счёт стоимости франшизы.

**Модель (изменения):**
- `Product`: добавить `setup_price` (рекомендуемая стоимость установки и обучения), `subscription_price` (абонплата в месяц, справочно — фактическая приходит из интеграции). Цены франшизы — уже есть (`price_cash`, `price_per_sale`).
- Новая модель **Equipment** (оборудование, справочник центра): name, product (nullable — общее для всех), price (рекомендуемая цена), unit (по умолчанию «шт»), is_active, sort_order. Ресурс `equipment/` (читают все, пишет hq_admin). Экран «Настройки → Сферы и цены» получает вкладку «Оборудование», а у продукта — поля setup_price и subscription_price.
- `ClientSale.kind`: `setup` (установка и обучение), `equipment` (оборудование), `subscription`, `renewal`, `other`. Значения `license`/`service` — удалить (миграция данных: license → setup, service → other).
- `ClientSale`: добавить `equipment` (nullable FK), `quantity` (Decimal, для оборудования, по умолчанию 1), `franchisee_income` (доход франчайзи по продаже: setup/equipment/other — 100% amount; subscription/renewal — commission_amount), `withheld_amount` (сколько удержано в счёт франшизы с этой продажи). `commission_*` остаются только для абонплаты.
- Франчайзи вручную вносит `setup`, `equipment`, `other` (для equipment amount по умолчанию = equipment.price × quantity, можно изменить). `subscription`/`renewal` — только из интеграции (центр — корректировки).
- `Contract`: добавить `withhold_percent` (доля дохода франчайзи, удерживаемая центром в счёт франшизы, %; по умолчанию 30; 0–100; меняют hq_admin/hq_accountant).

**Удержание в счёт франшизы (заменяет зачёт «всё вознаграждение» из 5А):**
- Для договора со схемой `per_sale`, пока стоимость франшизы не погашена: с **каждой** продажи франчайзи по сферам этого договора (setup, equipment, subscription, renewal, other) удерживается `withheld = franchisee_income × withhold_percent / 100`, но не больше остатка непогашенной франшизы. Формируется зачёт: `FranchiseePayment(method=offset, contract)` на эту сумму (уменьшает долг франчайзи).
- Для абонплаты (деньги у центра): удержанное просто не выплачивается — `commission_due` уменьшается на withheld (через `CommissionPayout(method=offset)`, как сейчас).
- Для установки и оборудования (деньги у франчайзи): удержанная сумма становится долгом франчайзи перед центром — создаётся `Charge(kind=withhold, amount=withheld, due_date=дата продажи + 30 дней)` и одновременно зачёт по франшизе. Итог: долг по франшизе уменьшается, а появляется долг «удержание с продаж», который франчайзи оплачивает центру. (`Charge.kind` добавить `withhold`.)
- Возврат/удаление продажи отменяет её удержание пропорционально (как сейчас для возвратов).
- После погашения франшизы удержаний нет — франчайзи забирает весь свой доход.

**Отчёт «Продажи и доход» (`reports/sales/` — расширить):**
- `totals`: count, amount (продано всего), `income` (заработал франчайзи), `withheld` (удержано в счёт франшизы), `net_income` (= income − withheld), разбивка `by_kind`: `[{kind, label, count, amount, income}]` для setup / equipment / subscription+renewal / other.
- `by_product`, `by_franchisee`, `by_month` — добавить поля `income`, `withheld`, `net_income`.
- Франчайзи видит свой отчёт; центр — по выбранному франчайзи / сфере / всей сети (как в 5А).

**Экраны:**
- Франчайзи: кнопка **«Внести продажу»** (клиент → сфера → вид: Установка и обучение / Оборудование / Прочее; для оборудования — выбор из справочника и количество; сумма подставляется из цен центра). Раздел «Продажи и доход»: карточки «Продано», «Заработано», «Удержано в счёт франшизы», «Чистый доход»; разбивка по видам и по сферам; график по месяцам; список продаж с колонками «Вид», «Сумма», «Ваш доход», «Удержано».
- Центр: тот же отчёт по каждому франчайзи и по сети; в договоре per_sale — поле «Удерживать с дохода, %» и прогресс погашения.
- Дашборды: франчайзи — `income_month`, `withheld_month`; центр — `network_income_month` (доход франчайзи по сети), `withheld_month`.

## 5Д. Гибридные цены: тарифы абонплаты, гибкая оплата франшизы, правила вознаграждения (изменение 13.11.2026 — приоритетнее 5А–5Г)

Заказчик: цены меняются, бывают скидки, у каждого продукта свои тарифы; стоимость франшизы и вознаграждение центр должен настраивать гибко («гибридно») прямо в системе.

### 1. Тарифы абонплаты
- **Tariff**: product, code (slug, уникален в продукте — его присылает интеграция), name («Старт», «Бизнес», «Сеть»), monthly_price, description, is_active, sort_order.
- **Изменение цены не переписывает историю**: модель **TariffPrice** (tariff, monthly_price, valid_from, valid_to nullable). Текущая цена = действующая на дату. Экран тарифа показывает историю цен; «Изменить цену с даты…» закрывает текущую запись и открывает новую.
- `Product.subscription_price` из 5Г — заменить тарифами (оставить как вычисляемое «от N сом» = минимальная текущая цена активных тарифов).
- `ClientProductAccount`: добавить `tariff` (nullable FK), `discount_percent` (персональная скидка клиента, справочно).
- `ClientSale` (абонплата): добавить `tariff`, `list_amount` (по прайсу тарифа × месяцы), `discount_amount` (= list_amount − amount, ≥ 0), `amount` — фактически оплачено.
- Интеграция (`payment.succeeded`): новые необязательные поля `tariff_code`, `list_amount`, `discount_amount`. Если tariff_code не найден у продукта — событие обрабатывается без тарифа и с предупреждением в журнале (status `processed`, error = текст предупреждения).
- Ресурсы: `tariffs/` (фильтр product, is_active; поля current_price, prices[]), `tariffs/{id}/set-price/ {monthly_price, valid_from}`. Пишет hq_admin.

### 2. Гибридная оплата франшизы
Вместо жёстких схем cash/prepay/per_sale договор хранит **составную оплату** — любая комбинация трёх частей:
- `upfront_amount` — сразу (одно начисление `fee` со сроком `upfront_due`, по умолчанию signed_at);
- `installments_total` + `installments_count` + `installment_step_months` (по умолчанию 1) — рассрочка равными платежами (`installment`, последний забирает копейки);
- остаток `fee_total − upfront_amount − installments_total` — **погашается удержаниями из дохода** (`withhold_percent`, раздел 5Г). Если остаток > 0, а withhold_percent = 0 — ошибка валидации.
- `payment_scheme` остаётся как подпись-пресет для удобства: `cash` (всё сразу), `prepay` (предоплата + рассрочка), `per_sale` (всё из продаж), `hybrid` (любая комбинация). Форма договора: выбор пресета заполняет части, центр может изменить любую часть; итоговая проверка: upfront + installments ≤ fee_total.
- Скидка на франшизу: `list_price` (по прайсу продукта/пакета) и `fee_total` (договорная) — уже есть; добавить `discount_note`.
- Цены франшизы у продукта/пакета (`price_cash`, `price_per_sale`, `price_prepay`) остаются ориентирами для пресетов.
- Прогресс погашения в договоре показывает три части: «Сразу: оплачено X из Y», «Рассрочка: X из Y», «Из продаж: удержано X из Y».

### 3. Правила вознаграждения (гибридные)
Ставка больше не одно число в договоре. **CommissionRule**:
- область действия (все поля необязательны, пустое = «любой»): `product`, `tariff`, `franchisee`, `sale_kind` (`subscription` — означает subscription и renewal, `setup`, `equipment`, `other`);
- расчёт: `percent` (доля франчайзи, %, nullable) и/или `fixed_amount` (фиксированная сумма франчайзи, nullable) и `fixed_per` (`sale` — за продажу, `month` — за каждый оплаченный месяц абонплаты); можно оба — тогда доход = amount × percent/100 + фикс;
- `base`: `paid` (от фактически оплаченного, по умолчанию) или `list` (от прайсовой суммы — скидка за счёт центра);
- `valid_from`, `valid_to` (nullable), `is_active`, `name`, `note`.
- **Выбор правила** для продажи: среди активных и действующих на дату продажи — самое конкретное: количество заданных полей области (franchisee даёт +4, tariff +2, product +1, sale_kind +1 — выигрывает наибольший вес); при равенстве — более поздний valid_from, затем больший id.
- **Правила по умолчанию** (сидер, редактируемые): «Абонплата — 20%» (sale_kind=subscription, percent=20); «Установка и обучение — 100%»; «Оборудование — 100%»; «Прочее — 100%». Плюс примеры: «OZOK Бишкек Юг — 30% с абонплаты», «COCA тариф Сеть — 25% + 200 сом/мес».
- Продажа хранит **снимок**: `commission_rule` (FK, nullable), `commission_percent`, `commission_fixed`, `commission_base_amount`, `franchisee_income` (итог). Изменение правил не пересчитывает прошлые продажи; hq_admin/hq_accountant могут вручную пересчитать продажу по текущим правилам (`client-sales/{id}/recalculate/`) или задать доход вручную (как сейчас commission_percent — теперь `franchisee_income` с флагом `income_overridden`). Ограничения на refund из ревью сохраняются.
- `Contract.commission_percent` удалить; в форме договора вместо него — блок «Вознаграждение этого франчайзи» со ссылкой на его правила и кнопкой «Добавить правило для франчайзи» (создаёт CommissionRule с franchisee).
- Ресурсы: `commission-rules/` (фильтры product, tariff, franchisee, sale_kind, active_on=YYYY-MM-DD; пишут hq_admin, hq_accountant; франчайзи читают правила, которые к ним применимы), `commission-rules/preview/?franchisee=&product=&tariff=&sale_kind=&amount=&months=&date=` → `{rule: {...} | null, percent, fixed, income}` — для проверки «сколько получит франчайзи».
- Удержание в счёт франшизы (5Г) считается от итогового `franchisee_income`.

### 4. Экраны
- «Настройки → Сферы и цены»: у продукта вкладки «Франшиза» (ориентиры цен), «Установка», «Тарифы» (список тарифов, история цен, «Изменить цену с даты»), «Оборудование».
- «Настройки → Вознаграждение»: таблица правил (область, расчёт, срок действия), фильтры, создание/редактирование, калькулятор-превью «Франчайзи / сфера / тариф / вид / сумма → доход франчайзи и какое правило сработало».
- Договор: конструктор оплаты (пресет + три части + итог и проверка), прогресс по трём частям.
- Продажи: колонки «По прайсу», «Скидка», «Оплачено», «Доход франчайзи», «Правило»; фильтр по тарифу.
- Франчайзи видит действующие для него правила вознаграждения (чтение) в разделе «Мои условия».

## 6. API (контракт, обязателен для web и mobile)

Префикс `/api/v1/`. JSON. `Authorization: Bearer <access>`. Пагинация `?page=N&page_size=20` (до 100), ответ `{"count", "next", "previous", "results"}`. Поиск `?search=`, сортировка `?ordering=`. Ошибки — стандарт DRF, тексты на русском. Swagger: `/api/docs/`.

Для сотрудников центра у всех списков есть фильтр `?franchisee=<id>`, у франчайзи он игнорируется.

### 6.1 Auth
| Метод | URL | |
|---|---|---|
| POST | `auth/login/` | `{email, password}` → `{access, refresh, user}` |
| POST | `auth/refresh/` | `{refresh}` → `{access}` |
| GET | `auth/me/` | → `user` |

`user` = `{id, email, first_name, last_name, full_name, phone, role, is_hq, franchisee: null | {id, name, city, status}}`

### 6.2 Ресурсы (ModelViewSet: list, create, retrieve, update, partial_update, destroy)

| Ресурс | URL | Фильтры | search | Доп. поля в ответе | Кто пишет |
|---|---|---|---|---|---|
| Продукты | `products/` | is_active | name | — | hq_admin |
| Франчайзи | `franchisees/` | status, city, curator (`me` или id) | name, city, owner_name, phone | curator_name, balance, overdue_amount, rating, employees_count, clients_count, products (коды из активных договоров) | hq_admin, hq_curator |
| Договоры | `contracts/` | franchisee, status, payment_scheme | number | franchisee_name, product_codes, paid_off_amount (для per_sale), charges_total | hq_admin, hq_accountant |
| Заявки | `leads/` | status, interest, product, source, city, overdue=true, unassigned=true | name, phone, city | franchisee_name, overdue, client_id | центр — всё; франчайзи — создание (source задаётся), правка comment, действия |
| Клиенты | `clients/` | business_type, city, is_active, renewal_soon=true, expired=true | business_name, contact_name, phone | franchisee_name, products (из продаж/внедрений), last_paid_until, implementations_count | франчайзи; центр — чтение (hq_admin — всё) |
| Внедрения | `implementations/` | stage, product, client | client name | client_name, franchisee_name, product_name, responsible_name, overdue (planned_launch < сегодня и не launched/cancelled) | франчайзи; hq_admin |
| Продажи клиентам | `client-sales/` | product, kind, client, date_from, date_to | client name | client_name, franchisee_name, product_name, charge (`{id, kind, amount}` или null), warning (только в ответе на create) | франчайзи; hq_admin |
| Начисления | `charges/` | kind, contract, overdue=true, date_from, date_to | description | franchisee_name | hq_admin, hq_accountant (франчайзи — чтение своих) |
| Платежи франчайзи | `franchisee-payments/` | method, date_from, date_to | comment | franchisee_name, created_by_name | hq_admin, hq_accountant (франчайзи — чтение своих) |
| Курсы | `courses/` | product, is_active | title | enrolled_count, passed_count | hq_admin, hq_curator (все читают) |
| Назначения обучения | `enrollments/` | course, user, status, expired=true | user name | course_title, user_name, franchisee_name, expired | центр создаёт; fr_owner может назначать своим сотрудникам |
| Обращения | `tickets/` | status, priority, client, sla_breached=true | subject, client name | client_name, franchisee_name, assignee_name, sla_breached, sla_hours | франчайзи; центр |
| Проверки | `audits/` | franchisee, date_from, date_to | summary | franchisee_name, inspector_name, score | hq_admin, hq_curator (франчайзи — чтение своих) |
| Планы | `sales-plans/` | franchisee, month (`YYYY-MM`) | — | franchisee_name, fact_clients, fact_revenue, plan_percent | hq_admin, hq_curator (франчайзи — чтение) |
| Задачи | `tasks/` | status, priority, overdue=true, assignee | title | franchisee_name, created_by_name, assignee_name, overdue, comments_count | центр создаёт/правит; франчайзи — действия |
| Комментарии задачи | `tasks/{id}/comments/` (GET list, POST `{text}`) | — | — | author_name | все, у кого есть доступ к задаче |
| Пользователи | `users/` | role, franchisee, is_active | email, name | franchisee_name (`password` только запись) | hq_admin — все; fr_owner — сотрудники своего франчайзи с ролью fr_owner/fr_manager |

### 6.3 Дашборд и отчёты
`GET dashboard/?month=YYYY-MM` (по умолчанию текущий месяц). Ответ зависит от пользователя.

Центр (`"scope": "hq"`):
```json
{
  "scope": "hq",
  "month": "2026-11",
  "franchisees": {"active": 12, "onboarding": 2, "candidate": 5, "suspended": 1},
  "leads": {"new": 7, "assigned": 10, "in_progress": 14, "demo": 6, "won": 9, "lost": 4, "overdue": 3, "unassigned": 2},
  "new_clients": 9,
  "network_revenue": "412000.00",
  "receivables": "230000.00",
  "overdue_receivables": "54000.00",
  "payments_month": "150000.00",
  "open_tickets": 11, "sla_breached": 2,
  "overdue_tasks": 4,
  "trained_share": 0.78,
  "revenue_by_month": [{"month": "2026-06", "revenue": "250000.00"}],
  "rating": [{"franchisee_id": 1, "name": "OZOK Ош", "city": "Ош", "rating": 86, "place": 1, "plan_percent": 0.95}],
  "attention": [{"type": "overdue_payment|sla|overdue_task|overdue_lead|expired_cert", "franchisee_id": 3, "franchisee_name": "…", "text": "Просрочка 24 000 сом"}]
}
```
`revenue_by_month` — последние 6 месяцев; `rating` — все активные, по месту; `attention` — до 10 пунктов.

Франчайзи (`"scope": "franchisee"`):
```json
{
  "scope": "franchisee",
  "month": "2026-11",
  "plan": {"target_clients": 5, "target_revenue": "100000.00", "fact_clients": 3, "fact_revenue": "64000.00", "plan_percent": 0.62} ,
  "rating": 74, "place": 4, "network_size": 12,
  "leads": {"assigned": 3, "in_progress": 5, "demo": 2, "overdue": 1},
  "clients_total": 38, "renewal_soon": 4, "expired": 2,
  "balance": "45000.00", "overdue_amount": "12000.00",
  "open_tickets": 3, "sla_breached": 1,
  "tasks": {"new": 2, "in_progress": 1, "overdue": 1},
  "trained_share": 0.5,
  "revenue_by_month": [{"month": "2026-06", "revenue": "50000.00"}]
}
```
`plan` = null, если плана нет.

`GET reports/rating/?month=YYYY-MM` — таблица рейтинга с разбивкой: `[{franchisee_id, name, city, rating, place, plan_percent, avg_ticket_rating, last_audit_score, trained_share, payment_discipline, new_clients, revenue}]`.

`GET franchisees/{id}/summary/` — карточка партнёра: `{franchisee, balance, overdue_amount, rating, place, plan, contracts[], employees[], recent_leads[], recent_sales[], open_tasks[], last_audit}`.

### 6.4 Действия (POST; ответ — обновлённый объект; недопустимый переход — 400 `{"detail": "..."}`)

| URL | Тело | Кто |
|---|---|---|
| `leads/public/` | `{name, phone, city?, interest, product?, comment?}` → 201 `{id}` | без авторизации |
| `leads/{id}/assign/` | `{franchisee}` | центр |
| `leads/{id}/take/` · `demo/` | — | франчайзи |
| `leads/{id}/win/` | `{business_name?, business_type?}` → заявка с client_id | франчайзи |
| `leads/{id}/lose/` | `{lost_reason}` | франчайзи, центр |
| `contracts/{id}/activate/` · `close/` | — | hq_admin, hq_accountant |
| `enrollments/{id}/start/` | — | сам сотрудник, fr_owner |
| `enrollments/{id}/complete/` | `{score}` | центр (тренер/куратор) |
| `tickets/{id}/status/` | `{status}` | франчайзи, центр |
| `tickets/{id}/rate/` | `{rating}` | франчайзи, центр |
| `tasks/{id}/start/` · `done/` | `{comment?}` | франчайзи |
| `tasks/{id}/reject/` | `{comment}` (обязателен) | франчайзи |
| `tasks/{id}/cancel/` | — | центр |
| `implementations/{id}/stage/` | `{stage}` | франчайзи, hq_admin |

## 7. Веб-кабинет

Один кабинет, меню зависит от роли.

**Центр:** Дашборд сети · Франчайзи · Договоры · Заявки · Клиенты · Внедрения · Расчёты (начисления, платежи, балансы) · Обучение · Качество (обращения, проверки) · Планы и рейтинг · Задачи · Настройки (пользователи, продукты).

**Франчайзи:** Мой дашборд · Заявки · Клиенты · Внедрения · Продажи · Обращения · Задачи · Обучение · Расчёты с центром (чтение) · Команда (для fr_owner).

Ключевые экраны:
- **Дашборд сети**: тёмный hero с месяцем и главными цифрами (активных франчайзи, выручка сети, долг сети); воронка заявок; график выручки сети за 6 месяцев; таблица рейтинга с местом, планом и цветовой меткой; блок «Требует внимания» (просрочки, SLA, задачи, заявки).
- **Карточка франчайзи**: шапка (название, город, статус, куратор, рейтинг/место), вкладки: Обзор (KPI, план, баланс) · Договоры · Клиенты · Заявки · Расчёты · Обучение · Качество · Задачи · Сотрудники.
- **Заявки**: канбан по статусам + переключатель на таблицу; у центра — фильтр «Не распределены» и кнопка «Назначить»; у франчайзи — «Взять в работу», «Демо», «Успешно», «Отказ».
- **Договор**: форма с выбором продуктов, пакета и схемы; калькулятор подсказывает сумму по прайсу (сумма взносов; пакет ×0.70 для prepay; полный — для per_sale); после активации — график начислений.
- **Расчёты**: таблица балансов по франчайзи (долг, просрочка), начисления, платежи, создание платежа.
- **Планы и рейтинг**: выбор месяца, таблица с планом/фактом/процентом и разбивкой рейтинга; массовое задание плана.
- **Задачи**: список/канбан, карточка с комментариями.
- **Качество**: обращения с метками SLA; проверки с чек-листом.
- Таблицы: поиск, фильтры, сортировка, пагинация, пустые состояния, скелетоны. Статусы — иконка + подпись + цвет.
- Адаптивность от 360 px; навигация с клавиатуры, видимый фокус.

## 8. Мобильное приложение (Flutter)

Основной пользователь — франчайзи «в поле»; центр — быстрый обзор.
- Вход, токены в secure storage, авто-refresh.
- **Франчайзи**: Главная (план месяца, рейтинг/место, долг, задачи), Заявки (новые сверху, звонок по нажатию, действия взять/демо/успешно/отказ), Клиенты (поиск, карточка, продления, добавить продажу), Задачи (выполнить/отклонить, комментарии), Ещё (Обращения, Обучение, Расчёты, Профиль, Выход).
- **Центр**: Главная (KPI сети, рейтинг, «Требует внимания»), Франчайзи (список и карточка), Заявки (нераспределённые, назначить), Задачи, Ещё.
- Нижняя навигация по роли. `API_URL` через `--dart-define`, по умолчанию web `http://127.0.0.1:8000/api/v1`, Android-эмулятор `http://10.0.2.2:8000/api/v1`.

## 9. Дизайн (брендбук OZOK v1.0 + утверждённый прототип «Продукты OZOK»)

Источник истины по визуалу — прототип заказчика:
- Цвета: Navy `#091C35` (основной тёмный, текст, сайдбар, hero), Turquoise `#00BEBA` (основные кнопки, акценты, фокус), `#007F7C` (бирюзовый текст/ссылки на белом), Mist `#F3F7FB` (фон карточек), белый `#FFFFFF` (фон страницы), вторичный текст `#4B5A70`, текст на тёмном `#D5DDE8`, границы `#DCE3EC`, рамка полей `#B9C4D3`. Статусы (всегда с иконкой и подписью): успех `#1E9E6A`, предупреждение `#C98A0B`, ошибка `#D64545`, инфо `#2F6FDE`.
- Кнопки-«таблетки» (radius 999px, высота 46, крупные 54–56, bold 15). Основная: `#00BEBA` + текст `#091C35`. Вторичная: рамка 1.5 px `#091C35`. Тёмная: `#091C35` + белый.
- Карточки radius 16, фон `#F3F7FB` без тени или белые с рамкой `#DCE3EC`. Hero — radius 24, Navy, полупрозрачные бирюзовые круги.
- Иконки контурные (stroke 2), в кругах 44–56 px (Navy + бирюзовая иконка).
- «Бровь» над заголовком: 12 px bold uppercase, letter-spacing .08em, `#4B5A70`.
- Поля: radius 12, высота 50, подпись над полем 14 bold. Фокус: `3px solid #00BEBA`, offset 2px.
- Шрифт Arial, 'Helvetica Neue', Helvetica, sans-serif. H1 32–56, H2 26–40 bold.
- Логотип только на белой подложке (пока — текстовая заглушка «OZOK»).
- Деньги: `35 000 сом`. Даты `dd.MM.yyyy`. Телефоны `0555 000 000`. Тон — на «вы», коротко.

## 10. Демо-данные

`python manage.py seed_demo [--reset]`:
- 4 продукта с ценами; центр: `admin@ozok.kg` (hq_admin), `curator@ozok.kg` (hq_curator), `buh@ozok.kg` (hq_accountant); пароль у всех `demo12345`.
- 12 франчайзи в городах Кыргызстана (Бишкек ×3, Ош ×2, Джалал-Абад, Каракол, Токмок, Нарын, Талас, Баткен, Кара-Балта) в разных статусах (8 active, 2 onboarding, 1 candidate, 1 suspended); у каждого активного — 1–3 сотрудника; демо-вход франчайзи: `osh@ozok.kg` (fr_owner «OZOK Ош»), `osh.manager@ozok.kg` (fr_manager).
- Договоры всех трёх схем, начисления и платежи с просрочками у части партнёров.
- ~120 заявок за 6 месяцев во всех статусах, ~80 клиентов, внедрения на разных этапах, ~200 продаж клиентам за 6 месяцев (с начислениями).
- 4 курса, назначения с результатами (часть просрочена), ~40 обращений (часть с нарушением SLA, с оценками), проверки по 1–2 на франчайзи, планы на последние 3 месяца, ~30 задач.
- Всё создаётся через те же сервисные функции, что и API.

## 11. Критерии приёмки MVP

1. `migrate && seed_demo && runserver` поднимает API; Swagger открывается; `manage.py test` зелёный.
2. Изоляция: пользователь «OZOK Ош» не видит и не может изменить данные других франчайзи (тест на каждый ресурс).
3. Сценарий: публичная заявка из Оша → автоматически у «OZOK Ош» → взята → демо → успешно → клиент → продажа → начисление по договору → платёж франчайзи → баланс уменьшился → факт плана и рейтинг на дашбордах обновились.
4. Договор `prepay` при активации создаёт предоплату и график рассрочки на точную сумму; `per_sale` — отчисления не превышают взнос.
5. SLA и просрочки задач/заявок/платежей считаются верно (тесты).
6. `npm run build` в `web/` без ошибок; вход под центром и под франчайзи показывает разные меню и дашборды на реальном API.
7. Flutter: `flutter analyze` без ошибок, `flutter build web` успешен; вход франчайзи → заявки → взять в работу.

## 12. Открытые вопросы к заказчику

1. Условия, при которых франчайзи получает 30% вместо 20% от абонплаты (в MVP — ставка в договоре + ручная правка у продажи центром).
2. Графики и сроки рассрочки для «Предоплата 40%» (по умолчанию — равными платежами помесячно, количество задаётся в договоре).
3. Цены франшиз вести в сомах или в долларах (с пересчётом по курсу НБКР)? В MVP — в сомах.
4. Какие поля об оплате продукты реально могут отдавать и могут ли они слать события сами (push) или OZOK должен забирать их по расписанию (pull)? В MVP — push-вебхук `integrations/events/`.
# Asian Restaurant

Сервис заказа паназиатской кухни: витрина с меню, серверная корзина, оформление
заказов с валидацией остатков и адреса, история заказов и интерфейс сотрудника.

## Стек

- **Backend:** Django 5.2, Django Ninja, PostgreSQL 16, Redis 7, RabbitMQ 4.3.
- **Frontend:** React 18, TypeScript, Vite, Tailwind CSS, TanStack Query, Zustand.
- **Инфраструктура:** Docker Compose, Prometheus, nginx (раздача SPA и прокси API).

## Архитектура

- **Меню** (`menu`) - каталог, остатки (`stock_quantity`), журнал изменений
  остатков (`StockAdjustment`).
- **Корзина** (`cart`) - серверная, в Redis. Атомарные операции через Lua
  (compare-and-set по версии), гостевая корзина по signed-cookie, слияние в
  пользовательскую при входе, сверка со стоком, fail-closed `503` при недоступности
  Redis.
- **Заказы** (`orders`) - checkout только для авторизованных: телефон из аккаунта,
  идемпотентность (`idempotency_key` и `(cart, version)`), `select_for_update` по
  остаткам, серверная проверка адреса (геокодер с кешем), запись заказа и
  transactional outbox в одной транзакции, стейт-машина статусов.
- **Сотрудник** (`employee`) - управление заказами, инвентарём и пользователями;
  доступ только для суперпользователя или штатной роли `restaurant_operator` / `restaurant_manager`.

Публикация событий заказа в RabbitMQ идёт через transactional outbox
(`OrderOutbox`) с publisher confirms; consumer-runtime — на этапе Stage 5.
Доменное событие никогда не отбрасывается автоматически: строка, которую не
удалось опубликовать, повторяется бесконечно. После порога попыток она
перестаёт учитываться в возрасте бэклога и попадает в отдельный сигнал
(`outbox_stuck_rows`), иначе одна такая строка держала бы критическую тревогу
и прятала за собой настоящее отставание. События одного агрегата уходят строго по порядку,
поэтому такая строка держит и все более поздние события своего агрегата. Разбор - `manage.py outbox --list`,
`--retry <id>` после устранения причины, `--discard <id> --yes --reason ...`
чтобы закрыть строку насовсем.

## Быстрый старт

```bash
# 1. окружение
cp .env.example .env            # заполнить секреты

# 2. поднять стек: миграции выполнят одноразовые storefront-migrate и operations-migrate,
# остальные сервисы стартуют только после того, как те завершатся с кодом 0
docker compose up -d

# 3. каталог и начальные остатки, один раз на новой базе
docker compose exec backend python manage.py seed_menu
docker compose exec backend python manage.py seed_initial_stock --confirm

# 4. администратор и первый сотрудник
docker compose exec backend python manage.py createsuperuser
docker compose exec backend python manage.py grant_employee <телефон>
```

- Витрина: `http://localhost` (nginx) или `http://localhost:5173` (Vite dev).
- API и Swagger: `http://localhost:8000/api/docs`.
- OpenAPI-схема: `http://localhost:8000/api/openapi.json`.

## Переменные окружения

Полный список - в [.env.example](.env.example). Ключевые:

| Переменная | Назначение |
|---|---|
| `DATABASE_URL` | подключение к PostgreSQL |
| `REDIS_URL` | кэш и rate-limit (db 1) |
| `CART_REDIS_URL` | серверная корзина (db 0) |
| `RABBITMQ_URL` | шина событий заказов |
| `RATELIMIT_TRUST_XFF` | доверять `X-Forwarded-For` (только за доверенным прокси) |
| `GEOCODER_*` | серверная проверка адреса доставки |

## API

Все ручки под префиксом `/api`.

| Группа | Назначение |
|---|---|
| `/auth` | регистрация, вход, `me`, CSRF |
| `/menu` | каталог и карточки товаров |
| `/cart` | серверная корзина (гость и пользователь) |
| `/orders` | checkout, история, проверка адреса |
| `/employee` | заказы, инвентарь, пользователи, роли (для сотрудников) |

## Роли и доступ сотрудника

Штатных ролей две. `restaurant_operator` работает с очередью заказов.
`restaurant_manager` дополнительно ведёт склад и клиентские данные —
телефоны, адреса, историю заказов. Суперпользователь имеет всё.

Сотрудник держит ровно одну роль: две группы сразу не расширяют доступ, а
закрывают его целиком — такое членство считается сломанной выдачей. Флаг
`is_staff` сам по себе доступа не даёт. Назначает роль только активный
суперпользователь — через API (`POST /api/employee/users/{id}/role`) или
команду `grant_employee --actor <superuser> --role <role>`; проверка стоит в
доменном сервисе, а не только в декораторе. Каждое изменение пишется в
`EmployeeRoleAudit` вместе с прежней и новой ролью.

## Тесты

```bash
docker compose exec backend pytest
```

Тесты требуют PostgreSQL и Redis (поднимаются в стеке). Внешний геокодер в тестах
отключён; rate-limit и корзина изолированы.

## Структура

```
backend/
  accounts/   учётные записи, роль сотрудника, аудит
  menu/       каталог, остатки, StockAdjustment
  cart/       серверная корзина (Redis, Lua)
  orders/     checkout, заказы, геокодер, outbox, RabbitMQ
  employee/   API интерфейса сотрудника
  common/     rate-limit и общие утилиты
  config/     настройки, сборка API
frontend/
  src/api/    клиентские хуки (TanStack Query)
  src/pages/  экраны (home, menu, product, orders)
  src/components/, src/stores/
```

## Релиз

Порядок деплоя и первая установка каталога - в [DEPLOY.md](DEPLOY.md).

#### - [mailor](https://github.com/mailorq) - fullstack dev
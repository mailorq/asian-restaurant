# Сервис Operations

Независимый сервис для сотрудников ресторана. Хранит свои проекции заказов, покупателей, склада и прав
сотрудников и жизненный цикл команд смены статуса. ORM storefront не импортирует и в базу storefront не ходит,
данные получает версионированными событиями через RabbitMQ. Как он встроен в систему, описано в
[корневом README](../../README.md#архитектура).

## Владение данными

- Storefront - единственный писатель заказа, склада, данных покупателя и инвариантов checkout.
- Operations строит проекции из событий и просит изменить заказ асинхронной командой, вторым писателем статуса
  заказа он не становится.
- Базы и роли раздельные: процессы подключаются как `operations_runtime`, миграции идут под
  `operations_migrator`, а учетные данные Operations база storefront не принимает (`tests/test_isolation.py`).
  Общих Django-сессий, `SECRET_KEY` и учетных данных нет.

## Контракты

Версионированные Pydantic-контракты лежат в `packages/event_contracts` (без Django). Каждое событие и каждую
команду оборачивает один конверт: `event_id`, `event_type`, `schema_version`, `occurred_at`, `producer`,
`aggregate{type,id,version}`, `correlation_id`, `causation_id`, `trace_id`, `data`. Обратно совместимые
добавления старые потребители игнорируют, несовместимое изменение требует новой версии события.

## Процессы

| Процесс | Что делает | Запуск |
|---|---|---|
| `operations-api` | проекции для сотрудников, создание и чтение команд под `/ops-api/` | gunicorn с воркерами uvicorn, в dev-образе `runserver` |
| `operations-consumer` | обновляет проекции и завершает команды по их исходам | `python manage.py run_operations_consumer` |
| `commands-relay` | публикует команды из outbox и периодически закрывает просроченные | `python manage.py publish_commands --loop` |
| `operations-bridge` | переносит события из vhost `storefront` в vhost `operations` | `python manage.py bridge_storefront_events` |

Bridge - временный адаптер: storefront пока публикует события в прежнем формате, bridge переводит их в
версионированные конверты.

Ручные команды:

| Команда | Назначение |
|---|---|
| `resolve_command` | закрывает `timed_out` или `dispatch_failed` по решению оператора ([runbook-0008](docs/runbook-0008-command-plane.md)) |
| `sweep_commands` | тот же проход по просроченным командам, что периодически делает `commands-relay` |
| `reconcile` | сверяет проекции с завершенным снимком в обе стороны |
| `dlq` | просмотр, повтор и удаление сообщений из dead-letter очереди |
| `publish_test_event` | проверочное `order.created` в `operations.events` |

## Изоляция сообщений

Storefront и Operations живут в **разных vhost RabbitMQ** с отдельными пользователями и минимальными правами.
Провижининг и полная матрица прав - в [`ops/rabbitmq/README.md`](../../ops/rabbitmq/README.md). Адрес
брокера storefront Operations не получает.

Bridge надежен: проверяет каждый сконвертированный конверт по контракту до публикации, подтверждает исходную
доставку только после подтвержденной публикации, отправляет ядовитые сообщения в свою DLQ, а временные сбои
откладывает через ограниченную очередь повторов (`operations.bridge.*`), после которой сообщение уходит в DLQ.
Он сохраняет исходный `occurred_at` и `producer=storefront`, отмечая себя в `relayed_by`. Исходы команд на обоих
участках идут своей очередью (`operations.bridge.outcomes`, `operations.outcomes`): у команды есть срок, и она не
должна ждать за догрузкой проекций.

## Версии проекций

Проекция продвигается только при `incoming_version > current_version`. Меньшая версия - безопасный no-op
(`operations_projection_events_total{outcome="stale"}`). Та же версия с другим `event_id` - `ProjectionConflict`:
потребитель отправляет сообщение в DLQ и пишет `outcome="conflict"`, ничего не перезаписывая. Повторная доставка
того же `event_id` идемпотентна за счет inbox.

## Командный контур

Сотрудник, переведенный на команды (`transitions_via_commands` в storefront), меняет статус заказа только через
этот сервис. Панель обращается к BFF storefront, а тот вызывает
`POST /ops-api/orders/{order_id}/transition-commands` с заголовком `Idempotency-Key`. Исход читается из
`GET /ops-api/commands/{command_id}`, и только автором команды. Для остальных сотрудников остается прямая смена
статуса в storefront (`/api/employee/*`). Устройство контура - в
[ADR-0001](../../packages/event_contracts/docs/ADR-0001-order-transition-command-plane.md), разбор команд,
которым нужен оператор, - в [runbook-0008](docs/runbook-0008-command-plane.md).

## Авторизация

Storefront остается единственным владельцем штатной роли сотрудника (`restaurant_operator` или
`restaurant_manager`), Operations не читает `auth_user` и группы. BFF storefront выпускает на каждый вызов
короткоживущий JWT с подписью RS256 (`sub`, `roles`, `authz_version`, `jti`, `exp`, `iss=identity`,
`aud=operations`). Тот же токен вошедший сотрудник может получить сам через `POST /api/auth/employee-token`.
`EmployeeJWTAuth` проверяет подпись по JWKS storefront (`GET /api/auth/jwks`) и требует штатную роль, которую
подтверждает и локальная проекция прав. Автор берется из проверенного токена, а `actor_id` в теле запроса
игнорируется. `/ops-api/*` открыт только сотрудникам, без авторизации доступен лишь `/health`, а OpenAPI-схема
отдается только вне production.

Каждый запрос требует, чтобы проекция `EmployeeAuthorization` совпадала с `authz_version` токена при активных
роли и пользователе. Неизвестная или устаревшая проекция означает отказ, непрочитанная - ошибку сервера, но не
доступ. Отзыв роли действует, когда событие `identity.authz_changed.v1` дошло до проекции; до этого внешняя
граница - срок жизни токена, по умолчанию 600 с ([ops/identity/README.md](../../ops/identity/README.md)). Заказ
в этом окне не изменится: storefront перепроверяет роль и версию сотрудника, когда применяет команду.

## Запуск и тесты

Operations поднимается вместе со всем dev-стеком (см. корневой README):

```bash
# из корня репозитория
docker compose up -d --build

# схема, которую применила миграция
docker compose logs operations-migrate
docker compose exec operations-db psql -U ops_user -d operations -c "\dt"

# проверочное событие из процесса, у которого есть сеть брокера; проекция должна появиться в базе
docker compose exec operations-consumer python manage.py publish_test_event --order-id 555999 --customer-id 88
docker compose exec operations-db psql -U ops_user -d operations -c \
  "SELECT source_order_id, status FROM operations_operationorder WHERE source_order_id=555999;"
```

Тесты идут не в dev-стеке, а в отдельном проекте с уникальным именем: production-образ `operations-api` ставится
без dev-зависимостей. Тестовый оверлей собирает dev-образ с подключенными исходниками, подключает тесты под
bootstrap-пользователем, потому что они создают свою базу, и дает им адрес базы storefront для проверки изоляции.

```bash
P="asian-restaurant-ops-test-$(date +%s)"
docker compose -p "$P" -f compose.yaml -f compose.test.yaml up -d --build operations-api
docker compose -p "$P" -f compose.yaml -f compose.test.yaml exec operations-api pytest
docker compose -p "$P" -f compose.yaml -f compose.test.yaml exec operations-api sh -c "cd /srv/packages/event_contracts && python -m pytest -p no:cacheprovider"
docker compose -p "$P" -f compose.yaml -f compose.test.yaml down -v
```

Последняя команда удаляет тома только проекта, созданного первой.

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `OPERATIONS_DATABASE_URL` | база Operations под runtime-ролью, никогда не база storefront |
| `OPERATIONS_SECRET_KEY` | собственный секрет, не общий со storefront |
| `OPERATIONS_REDIS_URL` | хранилище лимитов новых команд |
| `OPERATIONS_RABBITMQ_URL` | потребитель проекций, только vhost `operations` |
| `OPERATIONS_COMMANDS_RABBITMQ_URL` | издатель команд: vhost `storefront`, запись только в `commands` |
| `OPERATIONS_BRIDGE_CONSUME_URL` | вход bridge: vhost `storefront`, пользователь адаптера |
| `OPERATIONS_BRIDGE_PUBLISH_URL` | выход bridge: vhost `operations`, пользователь адаптера |
| `IDENTITY_JWKS_URL` | публичные ключи storefront для проверки JWT |

## Состояние

Проекции и командный контур работают в dev-стеке и на одноразовых стендах, их прогоняют CI и смоуки. Публичного
развертывания нет.

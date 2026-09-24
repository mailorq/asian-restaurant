# Деплой и релиз

## Порядок релиза (production)

Миграции — шаг развёртывания, а не действие рантайма: их выполняют одноразовые сервисы
`storefront-migrate` и `operations-migrate`, и остальные контейнеры стартуют только после того,
как те завершились с кодом 0. Запускать `manage.py migrate` внутри рабочего контейнера не нужно:
это второй путь миграции мимо развёртывания.

Все команды релиза идут через обёртку. Без `compose.prod.yaml` разворачивается dev-стек с его
монтированиями, а без явного `--env-file` Compose молча возьмёт `.env` из рабочей копии —
то есть подставит значения разработки туда, где нужны секреты production.

```bash
# the checks live inside dc(): a failing top-level `test` neither stops an interactive shell nor trips `set -e` inside an && list, so every call re-checks and refuses on its own
# run from the checkout root: compose.yaml is found there, and it is what counts as inside
dc() {
  : "${PROD_ENV_FILE:?set PROD_ENV_FILE to the production secrets file}"
  test -r "$PROD_ENV_FILE" || { echo "PROD_ENV_FILE is not readable" >&2; return 1; }
  local repo_root env_file
  repo_root="$(realpath .)"
  env_file="$(realpath "$PROD_ENV_FILE")"
  # resolved first, so neither a relative path nor a symlink leading back in passes for external
  case "$env_file" in
    "$repo_root"/*) echo "PROD_ENV_FILE must live outside the checkout" >&2; return 1 ;;
  esac
  # the resolved path, so a symlink swapped after the check cannot redirect compose
  docker compose --env-file "$env_file" -f compose.yaml -f compose.prod.yaml "$@"
}

# 1. поднять брокер и выдать пользователей и права; шаг обязан завершиться с кодом 0
dc up -d rabbitmq
dc --profile provision run --rm rabbitmq-provision

# 2. применить миграции схемы обеим базам, до старта рантайма; роли базы данных провижинятся перед ними сами
dc up --exit-code-from storefront-migrate storefront-migrate
dc up --exit-code-from operations-migrate operations-migrate

# 3. поднять рантайм
dc up -d
```

Каталог и остатки в релиз не входят: они принадлежат работающему магазину. На новой установке их
заводят один раз, см. «Первая установка: каталог и остатки».

Процессы приложения работают под uid/gid 10001 с read-only корневой файловой системой, без
capabilities и с лимитами памяти и процессов. Статика собирается при сборке образа. Писать
можно только в tmpfs `/tmp` и, у backend, в `/tmp/prometheus` и том `media`.

Владельца тома `media` выставляет `media-init` перед стартом backend. Это порядок запуска, а не
восстановление: повторно он не запускается ни политикой перезапуска, ни сам по себе. Если в
том попали файлы root (восстановление из копии, запись с хоста), владельца возвращают явно:

```bash
dc run --rm --no-deps media-init
```

### Файл секретов

`PROD_ENV_FILE` указывает на обычный файл (не symlink) вне рабочей копии, в защищённом
каталоге, например `/etc/asian-restaurant/production.env`. Ни `.env`, ни любой другой файл внутри рабочей копии источником истины не является:
он содержит значения разработки и в production не используется. Значения-заглушки из
`.env.example` приложение отвергает при старте — процесс не поднимется, а не поднимется тихо
с чужим секретом.

`dc()` передаёт Compose уже разрешённый путь, поэтому подмена symlink после проверки ничего не
даёт. От подмены самого файла между проверкой и чтением проверка не защищает: это задача прав.
Каталог и файл доступны только root и deploy-пользователю (здесь его группа `deploy`):

```bash
sudo install -d -o root -g deploy -m 0750 /etc/asian-restaurant
sudo chown root:deploy /etc/asian-restaurant/production.env
sudo chmod 0640 /etc/asian-restaurant/production.env
```

### Ключ подписи

Docker secret из файла монтируется в контейнер с владельцем и правами файла на хосте, а процессы
приложения работают под uid/gid 10001. Ключ читает группа 10001 и больше никто, кроме root:

```bash
sudo chown root:10001 /etc/asian-restaurant/identity_jwt_private_key.pem
sudo chmod 0440 /etc/asian-restaurant/identity_jwt_private_key.pem
```

Без этого backend не поднимется: production-проверка настроек требует читаемый
`IDENTITY_JWT_PRIVATE_KEY_FILE`. Gid 10001 на хосте не должен принадлежать группе с
участниками: они получат чтение ключа.

Operations берёт открытые ключи с `http://backend:8000/api/auth/jwks` внутри сети. Этот путь
исключён из HTTPS-редиректа, так как TLS внутри сети нет, а `DJANGO_ALLOWED_HOSTS` обязан
содержать `backend`: иначе Operations не проверит ни одного токена и ответит `401` на любой запрос.

### Роли PostgreSQL

В каждой базе три роли. Bootstrap-суперпользователь (`POSTGRES_USER`, `OPERATIONS_DB_USER`)
создаёт кластер, и им пользуются только одноразовые `storefront-db-provision` и
`operations-db-provision`. Мигратор (`storefront_migrator`, `operations_migrator`) владеет
таблицами и выполняет миграции. Рабочие процессы подключаются как runtime (`storefront_runtime`,
`operations_runtime`): `CONNECT` только к своей базе, `USAGE` схемы `public`, DML и
последовательности, без `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `TEMPORARY` и владения схемой.

Провижининг запускается перед каждой миграцией как её зависимость и идемпотентен: сводит атрибуты
ролей, пароли, членство, права и владение. На существующем volume он передаёт мигратору таблицы,
которые до разделения создал bootstrap-пользователь. Пароли ролей берутся из файла секретов
(`STOREFRONT_DB_MIGRATOR_PASSWORD`, `STOREFRONT_DB_RUNTIME_PASSWORD`,
`OPERATIONS_DB_MIGRATOR_PASSWORD`, `OPERATIONS_DB_RUNTIME_PASSWORD`): не короче 16 символов из
`[A-Za-z0-9_-]`, потому что они входят в адрес подключения, попарно разные и не заглушки, иначе
провижининг останавливает релиз. `DATABASE_URL` и `OPERATIONS_DATABASE_URL` production не читает:
адреса подключения собираются из имён ролей.

В production провижининг отказывает, пока bootstrap-пароль короче 16 символов, содержит `change-me`
или начинается с `dev-`/`ops-dev`, и пока открыта хотя бы одна сессия bootstrap-пользователя,
кроме его собственной: такой процесс сохранил бы доступ суперпользователя, а отозвать его
провижининг не может.

Образ Postgres применяет `POSTGRES_*` только к пустому volume, поэтому bootstrap-учётка в файле
секретов обязана совпадать с той, с которой кластер создавался. `operations-db` раньше в
production молча поднималась с `ops_user`/`ops_pass`, если `OPERATIONS_DB_*` не были заданы. Такой
пароль меняют при первом выкате, см. ниже.

### Первый выкат с ролями PostgreSQL

Процессы прошлой версии ходят в базы bootstrap-суперпользователем, поэтому первый выкат идёт с
остановкой стека. Тома при этом остаются: `-v` не указывать.

```bash
dc down
```

Если bootstrap-пароль слабый, его меняют до провижининга, через локальный сокет контейнера базы:
старый пароль не нужен, новый не попадает ни в аргументы, ни в историю. Для `operations-db` со
старыми значениями:

```bash
dc up -d operations-db
read -rs NEW_BOOTSTRAP_PASSWORD && export NEW_BOOTSTRAP_PASSWORD
dc exec -T -e NEW_BOOTSTRAP_PASSWORD operations-db psql -X -q -U ops_user -d operations -f - < ops/postgres/rotate_bootstrap.sql
unset NEW_BOOTSTRAP_PASSWORD
```

Тот же пароль записывают в `OPERATIONS_DB_PASSWORD` файла секретов, `OPERATIONS_DB_USER` оставляют
`ops_user`. Для `db` команда та же, с его bootstrap-пользователем и базой. Дальше идёт обычный
порядок релиза с шага 1, а следующие релизы остановки уже не требуют: рабочие процессы ходят
своими ролями.

### Подключения к Postgres

Каждый процесс storefront и Operations берёт соединения из собственного пула: `min_size` 1,
`max_size` из `DJANGO_DB_POOL_MAX_SIZE` или `OPERATIONS_DB_POOL_MAX_SIZE` (по умолчанию 2, у
backend и operations-api 8), ожидание свободного соединения до 5 с.
Persistent-соединения выключены: под ASGI каждый запрос выполняется в своём потоке, и
закреплённое за потоком соединение не возвращается.

Верхняя граница соединений storefront: 4 воркера gunicorn × 8 + relay 2 + commands-consumer 2 +
storefront-migrate 2 + одна команда через `dc exec backend` 8 = 46 из 100 `max_connections`.
Legacy-консьюмер `ops` из профиля `legacy-projection` в production не запускается и сюда не
входит. Гейт считает сумму по production-рендеру, сверяет её с числом выше и не пропускает больше
половины `max_connections`, поэтому число воркеров и размер пула меняют вместе с этим расчётом.
После всплеска пул держит простаивающие соединения до 10 минут (`max_idle` psycopg_pool), но не
больше своей границы.

Верхняя граница соединений operations: 1 воркер gunicorn × 8 (operations-api) + operations-consumer 2 +
operations-bridge 2 + commands-relay 2 + operations-migrate 2 + одна команда через
`dc exec operations-api` 8 = 24 из 100 `max_connections`.

Запрос, не получивший соединение за 5 с, получает `503` с `Retry-After: 1` и без деталей, а
`db_pool_exhausted_total` (в Operations `operations_db_pool_exhausted_total`) растёт. Устойчивый рост этой метрики означает медленную БД или нехватку
пула, а не повод поднимать `max_connections`.

### Граница одновременных запросов

Под ASGI каждый запрос выполняется в своём потоке. Воркер одновременно принимает не больше 64
запросов у backend и 32 у operations-api, сверх этого uvicorn сразу отвечает
`503 Service Unavailable` и потока не заводит. `pids_limit` рассчитан от этой границы: на воркер
2 × граница (поток запроса и поток, который его завершает) + 32 потока исполнителя event loop + 8
своих, то есть не меньше 673 у backend (лимит 768) и 105 у operations-api (лимит 128). Гейт
сверяет лимит с этим расчётом. Если лимит pids исчерпать, asgiref навсегда оставит в воркере потоки,
которые не смог завершить, и воркер перестанет отвечать до перезапуска.

### Если рантайм не стартует

Сервисы ждут `Exited (0)` от своих одноразовых шагов, поэтому упавший провижининг ролей,
миграция или `media-init` выглядят как незапустившийся стек. Провижининг, отказавший из-за
открытых сессий bootstrap-пользователя, называет их адреса, см. «Первый выкат с ролями PostgreSQL»:

```bash
dc ps --all
dc logs storefront-db-provision operations-db-provision storefront-migrate operations-migrate media-init
```

### Первая установка: каталог и остатки

Один раз, после первого выката новой установки:

```bash
# создать отсутствующие товары без остатка и скопировать недостающие изображения
dc exec backend python manage.py seed_menu
# задать начальный остаток всем товарам; откажет, если в базе уже есть заказы или история остатков
dc exec backend python manage.py seed_initial_stock --confirm --quantity 50
```

`seed_menu` только создает товары, которых нет, и копирует изображения, которых нет в `media`.
Существующие товары, их цены, активность, остатки и загруженные изображения он не меняет, поэтому
повторный запуск безопасен, например чтобы добавить новые позиции из `backend/seed/products_data.py`.
Новая позиция появляется с нулевым остатком и недоступна в меню, пока сотрудник не задаст остаток.

`seed_initial_stock` перезаписывает остаток каждого товара тем же путем, что и корректировка
сотрудника: с новой версией, записью в журнал остатков и событием для Operations. Команда требует
`--confirm` и отказывает, как только в базе есть хотя бы один заказ или запись журнала остатков,
поэтому второй запуск и запуск на работающем магазине невозможны.

## TLS и обязательные требования к ingress

Стек не терминирует TLS. Production-рендер публикует nginx **только на loopback**
(`127.0.0.1:80`), а `DJANGO_SSL_REDIRECT` включён по умолчанию. Значит перед стеком обязан стоять
ingress, и он должен удовлетворять пяти условиям. Без первых трех приложение зациклится на редиректе
или потеряет различение клиентов, без двух последних останется без защиты, которую стек дать не может:

1. **Терминировать TLS** и проксировать на `127.0.0.1:80`.
2. **Стирать клиентские `X-Forwarded-*`** и выставлять их сам. nginx внутри стека доверяет тому,
   что пришло от ingress, и не перезаписывает эти заголовки: `X-Forwarded-Proto` он пробрасывает,
   `X-Forwarded-For` дополняет. Доверие здесь безопасно ровно потому, что дотянуться до слушателя,
   кроме ingress, некому. Если ingress не стирает клиентские значения, клиент сможет объявить себя
   пришедшим по https и подделать адрес для rate limit.
3. **Выставлять `X-Forwarded-Proto: https`** для запросов, пришедших по TLS. Без этого Django
   считает запрос незащищённым и отвечает редиректом на тот же URL — бесконечно.
4. **Ограничивать частоту `POST /admin/login/` по адресу клиента**, например не больше 10 попыток
   в минуту с всплеском 5. Django форму входа в админку не ограничивает, а nginx внутри стека сделать
   этого не может: за ingress все соединения приходят с адреса ingress, и лимит по адресу соединения
   стал бы одним общим ведром, которое опустошает любой посетитель. Гейт не дает вернуть такой лимит
   в nginx стека.
5. **Выставлять `Strict-Transport-Security: max-age=31536000; includeSubDomains`** на каждый ответ,
   включая SPA и статику, и заменять значение, которое backend ставит на свои ответы
   (`DJANGO_HSTS_SECONDS`, по умолчанию 3600), чтобы браузер не получал разные сроки с разных путей.
   `includeSubDomains` включать только после того, как каждый поддомен отвечает по HTTPS: браузер
   перестанет открывать их по HTTP на весь срок.

Проверить конфигурацию можно `bash scripts/check_prod_config.sh`: гейт падает, если появится
публикация порта за пределы loopback, если редирект выключат или если nginx снова начнёт
перезаписывать заголовки.

Пункты 4 и 5 проверяются на работающем ingress, а не в репозитории:

```bash
# один и тот же срок HSTS на корне SPA и на API
curl -sI https://<host>/ | grep -i strict-transport-security
curl -sI https://<host>/api/health | grep -i strict-transport-security
# после десятка неверных попыток входа ingress отвечает 429, пока не пройдет минута
for i in $(seq 1 20); do curl -s -o /dev/null -w '%{http_code} ' -X POST https://<host>/admin/login/; done; echo
```

## Порядок выката штатных ролей

Роль едет из storefront в Operations через событие авторизации. Пока Operations не получил
роли, он авторизует по прежнему флагу, поэтому порядок шагов важен: обратный оставит
сотрудников без доступа.

1. Выкатить контракт, мост и Operations — они уже понимают `roles` и `roles_known`, но
   продолжают принимать события без ролей.
2. Выкатить storefront с новыми ролями.
3. `python manage.py emit_authz_state` — переотправит текущее состояние всех штатных
   пользователей. Версия при этом не меняется, и проекция дописывает роли к существующей
   записи; это единственное разрешённое обогащение на одной версии.
4. Дождаться пустых `operations.bridge.*`, затем проверить, что не осталось
   активных записей без ролей:

```bash
dc exec operations-api python manage.py shell -c "from operations.models import EmployeeAuthorization as E; print(E.objects.filter(role_active=True, roles_known=False).count())"
```

   Ноль означает, что каждому активному субъекту роль доставлена.
5. Выдержать максимальный TTL прежних JWT — токены, выпущенные до шага 2, несут старую роль.
6. Отдельным коммитом убрать совместимость: приём `restaurant_employee` в Operations и
   ветку авторизации по флагу при `roles_known=False`.

## Снятие legacy-очереди orders.ops

Relay больше не объявляет и не привязывает `orders.ops`: каждый потребитель сам объявляет свои очереди,
а потребителя этой очереди production не запускает. Брокер, на котором работал прежний релиз, хранит
очередь, две ее привязки к событиям заказа и все, что в ней накопилось, то есть телефоны и адреса
клиентов, которые никто не читает. Один раз, после выката релиза, когда прежних процессов relay уже нет
(иначе прежний relay заново привяжет очередь при переподключении):

```bash
# снимает привязки сразу; удаляет очереди, только если они пусты, иначе называет, сколько в них сообщений
dc exec backend python manage.py retire_legacy_queue
# посмотреть накопленное, прежде чем удалять
dc exec -u rabbitmq rabbitmq rabbitmqctl list_queues -p storefront name messages consumers
# удалить очереди вместе с накопленным, после проверки
dc exec backend python manage.py retire_legacy_queue --discard-backlog
```

Повторный запуск ничего не делает. Первая команда без `--discard-backlog` ничего не удаляет, но
после нее очередь уже не растет.

## Переменные окружения

Все ключи описаны в [.env.example](.env.example). Значимые для этого этапа:

| Переменная | Назначение | Прод |
|---|---|---|
| `CART_REDIS_URL` | серверная корзина (отдельная redis-db от кэша) | `redis://redis:6379/0` |
| `RATELIMIT_TRUST_XFF` | доверять `X-Forwarded-For` в rate-limit | `true` **только** за прокси, который очищает и переустанавливает заголовок; иначе `false` |

## Redis

Корзина требует персистентности и отказа от вытеснения ключей. В
[compose.yaml](compose.yaml) redis запускается с `--appendonly yes
--maxmemory-policy noeviction` и томом `redis_data`. При изменении команды redis
пересоздать контейнер: `dc up -d redis`.

## Образы по digest

Каждый образ стека, одноразовых задач, CI и smoke-скриптов указан как `тег@sha256:...`: пересборка или
повторный `pull` не может подменить то, что прошло проверки. Тег оставлен рядом для чтения, решает digest.
Гейт `scripts/check_prod_config.sh` падает на ссылке без digest и на теге, закрепленном в разных местах
разными digest.

Обновление, например после выхода исправлений безопасности базового образа:

```bash
# digest индекса, общего для всех платформ, а не манифеста одной платформы
docker buildx imagetools inspect python:3.13-slim --format '{{json .Manifest}}'
# заменить digest этого тега во всех местах сразу и убедиться, что гейт зеленый
git grep -n 'python:3.13-slim@sha256'
bash scripts/check_prod_config.sh
```

Затем полный CI и smoke: новый базовый образ меняет то, на чем собираются и работают все процессы.

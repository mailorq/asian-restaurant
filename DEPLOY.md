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
  local repo_root env_file revision context image status
  repo_root="$(realpath .)"
  env_file="$(realpath "$PROD_ENV_FILE")"
  # resolved first, so neither a relative path nor a symlink leading back in passes for external
  case "$env_file" in
    "$repo_root"/*) echo "PROD_ENV_FILE must live outside the checkout" >&2; return 1 ;;
  esac
  # the images carry the commit as their tag, so an uncommitted change would be built under a tag that names other code
  git -C "$repo_root" diff --quiet HEAD -- || { echo "the checkout has uncommitted changes, a release deploys a commit" >&2; return 1; }
  revision="$(git -C "$repo_root" rev-parse HEAD)" || return 1
  if [ "${1-}" = build ]; then
    # the context is the commit itself: an untracked or git-ignored file of the checkout would enter an image whose tag does not name it
    context="$(mktemp -d)" || return 1
    status=0
    git -C "$repo_root" archive "$revision" | tar -x -C "$context" \
      && (cd "$context" && SOURCE_REVISION="$revision" docker compose --env-file "$env_file" -f compose.yaml -f compose.prod.yaml "$@") \
      || status=$?
    rm -rf "$context"
    return "$status"
  fi
  # compose builds a missing image from the checkout, so nothing runs before dc build has made all three
  for image in storefront operations frontend; do
    docker image inspect "asian-restaurant/$image:$revision" >/dev/null 2>&1 \
      || { echo "asian-restaurant/$image:$revision is not built, run dc build first" >&2; return 1; }
  done
  # the resolved path, so a symlink swapped after the check cannot redirect compose
  SOURCE_REVISION="$revision" docker compose --env-file "$env_file" -f compose.yaml -f compose.prod.yaml "$@"
}

# 1. собрать образы приложения этой ревизии; миграции и рантайм запускаются из одного образа на кодовую базу
dc build

# 2. поднять брокер и выдать пользователей и права; шаг обязан завершиться с кодом 0
dc up -d rabbitmq
dc --profile provision run --rm rabbitmq-provision

# 3. применить миграции схемы обеим базам, до старта рантайма; роли базы данных провижинятся перед ними сами
dc up --exit-code-from storefront-migrate storefront-migrate
dc up --exit-code-from operations-migrate operations-migrate

# 4. поднять рантайм
dc up -d
```

Образы приложения называются `asian-restaurant/storefront`, `asian-restaurant/operations` и
`asian-restaurant/frontend`, тег - хеш коммита рабочей копии. Новый коммит дает тег, которого на хосте еще нет,
поэтому ни одна команда релиза не может взять сборку прошлого релиза, а мигратор и процессы одной кодовой
базы запускаются из одного образа. Незакоммиченные изменения `dc()` отвергает: иначе под тегом коммита собрался
бы другой код. `dc build` собирает из выгрузки коммита (`git archive`), поэтому неотслеживаемые и игнорируемые git
файлы рабочей копии в образ не попадают. Остальные команды отказывают, пока образов этой ревизии нет: compose
собрал бы недостающий образ из рабочей копии. Образы прошлых релизов остаются на хосте для отката, удаляют их
вручную.

Каталог и остатки в релиз не входят: они принадлежат работающему магазину. На новой установке их
заводят один раз, см. «Первая установка: каталог и остатки».

Процессы приложения работают под uid/gid 10001 с read-only корневой файловой системой, без
capabilities и с лимитами памяти и процессов. Статика собирается при сборке образа. Писать
можно только в tmpfs `/tmp` и, у backend, в `/tmp/prometheus` и том `media`.

Nginx во frontend работает под uid 101 на порту 8080, на хосте он опубликован как `127.0.0.1:80`. Корень
только на чтение, тела запросов и pid лежат в tmpfs `/tmp`. Тело запроса ограничено 1 МБ, на `/admin/`
6 МБ: фото товара до 5 МБ проверяет форма админки. Адрес backend nginx берет из DNS Docker на каждый
запрос, поэтому пересозданный backend доступен без перезапуска nginx. Prometheus работает под uid 65534
и пишет только в том `promdata`.

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
Обратный путь такой же: backend вызывает команды на `http://operations-api:9000`, поэтому
`OPERATIONS_ALLOWED_HOSTS` обязан содержать `operations-api`, иначе Operations отвечает `400` на
каждый вызов, а панель сотрудника получает `502`.

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
Фото товаров лежат в репозитории (`backend/seed/images/optimized`) вместе с их sha256 в
`backend/seed/images.sha256`. Если какого-то фото нет или оно не совпадает с манифестом, `seed_menu`
отказывает, ничего не создав. После замены фото манифест пересчитывается:
`cd backend/seed/images/optimized && sha256sum *.webp > ../../images.sha256`.

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

## Лимит новых команд в Operations

Operations считает новые команды в `operations-redis`: по умолчанию 30 в минуту на сотрудника
и 5 в минуту на пару сотрудник и заказ (`OPERATIONS_COMMAND_LIMIT_PER_ACTOR`,
`OPERATIONS_COMMAND_LIMIT_PER_ORDER`, `OPERATIONS_COMMAND_LIMIT_WINDOW_SECONDS`). Повтор существующей
команды лимит не тратит. Два одновременных первых запроса с одним ключом или создание, упавшее после
списания, тратят по единице, возврата нет: команд не бывает больше, чем списаний. Без `operations-redis`
новые команды получают `503`, повторы существующих отвечают как обычно. Счетчики не переживают
перезапуск `operations-redis`, после него окно начинается заново.

## Смена статуса заказа командами

Сотрудник с флагом `transitions_via_commands` меняет статус заказа только командой: панель отправляет
ее в backend, backend вызывает operations-api с токеном, который выпускает сам и в браузер не отдает
(ADR-0001, ADR-0002). Прямой переход `POST /api/employee/orders/{id}/transition` и действия перехода в
админке для такого сотрудника закрыты. Остальные работают как раньше.
Backend ограничивает запросы сотрудника к командам: 60 отправок и 240 чтений статуса в минуту,
повторы входят в счет.

```bash
# переключить сотрудников на команды
dc exec backend python manage.py transitions_via_commands +79990000001 +79990000002
# вернуть на прямой переход
dc exec backend python manage.py transitions_via_commands +79990000001 --off
```

Откат идет по сотрудникам и сразу. Команды, которые уже приняты, доезжают и применяются: consumer
сверяет `expected_status` под блокировкой заказа, поэтому команда и прямой переход по одному заказу
не применятся оба. Команды в `timed_out` и `dispatch_failed` сами не закрываются: оператор проверяет
заказ и выносит решение через `resolve_command` (runbook-0008). Запасного пути нет: если Operations
недоступен, переключенный сотрудник получает отказ, а заказ не меняется. Вернуть его на прямой
переход решает оператор.

## Резервная копия и восстановление

Копия состоит из трех частей: `pg_dump -Fc` обеих баз от bootstrap-пользователя и архива тома `media`.
Базы разные, и один дамп не видит транзакций другого. Если между двумя дампами приложение изменит
заказ, копии окажутся из разных моментов: storefront со старым статусом, Operations с новой проекцией
и завершенной командой. `reconcile` этого не заметит: проекцию, опередившую снимок, он считает следом
более позднего живого события. Поэтому копия снимается только в окне обслуживания, без записи в обе базы:

```bash
# 1. закрыть вход: витрина, панель, админка и api команд перестают принимать запросы
dc stop frontend backend operations-api
# 2. дождаться, пока фоновые процессы доставят то, что уже в пути: все значения ниже равны 0
dc exec relay python manage.py shell -c "from orders.models import OrderOutbox as O; print(O.objects.filter(status='pending').count())"
dc exec operations-consumer python manage.py shell -c "from operations.models import OperationCommand as C, OperationsOutbox as O; print(O.objects.filter(status='pending').count(), C.objects.filter(status__in=['pending', 'dispatched']).count())"
# очереди с потребителями и очереди .retry пусты; очереди .dlq только хранят отложенное и базы не меняют
dc exec -u rabbitmq rabbitmq rabbitmqctl -q list_queues -p storefront name messages consumers
dc exec -u rabbitmq rabbitmq rabbitmqctl -q list_queues -p operations name messages consumers
# 3. остановить фоновые процессы и снять копию
dc stop relay commands-consumer commands-relay operations-consumer operations-bridge
dc exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > storefront.dump
dc exec -T operations-db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > operations.dump
dc run --rm --no-deps -T --entrypoint tar media-init -cf - -C /media . > media.tar
# 4. открыть снова
dc up -d
```

Копия с работающего стенда без окна требует отдельного протокола, которого сейчас нет.

Восстановление на пустой установке идет в таком порядке: поднять `db` и `operations-db`, выдать роли
(`storefront-db-provision`, `operations-db-provision`), затем `pg_restore --no-owner --role=<side>_migrator`,
чтобы владельцем объектов стал migrator, а runtime-роль получила свои права. Потом распаковать архив в
том (`dc run --rm --no-deps -T --entrypoint tar media-init -xf - -C /media --no-same-owner < media.tar`),
запустить `media-init` и выполнить обычный релиз. Брокер не копируется. В окне в нем ничего не было в
пути, а снимок `emit_source_state --snapshot` и `reconcile` в Operations после восстановления
показывают, что проекции сходятся.

`scripts/restore_rehearsal.sh` проходит этот путь на двух одноразовых стендах. Между двумя дампами
репетиция пытается подтвердить заказ через приложение, и в окне эту запись сделать некому. После
восстановления она сравнивает с копией канонический хеш строк каждой таблицы обеих баз, значения
последовательностей и байты файлов `media`, проверяет, что каждый заказ и товар стоит в проекциях
Operations там же, где в storefront, что nginx отдает восстановленное фото, и сверяет новый снимок. Это репетиция на локальном стенде. Расписание, шифрование, срок хранения и
хранилище вне хоста выбираются вместе с площадкой.

## Сетевая модель

Нужные потоки, источник → назначение:

| Источник | Назначение | Порт | Зачем |
|---|---|---|---|
| ingress на хосте | frontend | 8080 (на хосте `127.0.0.1:80`), http | весь внешний трафик |
| frontend | backend | 8000, http | `/api/`, `/admin/`, `/static/` |
| backend | db, redis | 5432, 6379 | данные, кэш, корзина, лимиты |
| backend | rabbitmq | 5672, amqp | операторские команды `dlq`, `retire_legacy_queue`, `publish_outbox` |
| backend | geocoder | 443, https, наружу | проверка адреса доставки |
| relay, commands-consumer | db, rabbitmq | 5432, 5672 | outbox и применение команд |
| storefront-migrate, storefront-db-provision | db | 5432 | релиз |
| operations-api | operations-db, operations-redis | 5432, 6379 | данные, лимит новых команд |
| operations-api | backend | 8000, http | JWKS |
| operations-consumer, operations-bridge, commands-relay | operations-db, rabbitmq | 5432, 5672 | проекции, мост, отправка команд |
| operations-migrate, operations-db-provision | operations-db | 5432 | релиз |
| rabbitmq-provision | rabbitmq | 4369, 25672, 15672 | выдача пользователей и прав |
| prometheus | backend 8000, relay 9104, commands-consumer 9102, operations-api 9105, operations-consumer 9101, commands-relay 9103, rabbitmq 15692 | http | сбор метрик |
| оператор на хосте | prometheus | 9090 (на хосте `127.0.0.1:9090`) | просмотр метрик |

Сеть Docker связывает всех своих участников со всеми в обе стороны и на все порты, поэтому сети нарезаны по
потокам. Состав каждой сети задан в [compose.yaml](compose.yaml) и сверяется гейтом `scripts/check_prod_config.sh`:

| Сеть | Участники | Маршрут наружу |
|---|---|---|
| `public_frontend` | frontend | есть |
| `public_prometheus` | prometheus | есть |
| `egress_backend` | backend | есть |
| `edge` | frontend, backend | нет |
| `storefront` | db, backend, relay, commands-consumer, storefront-migrate, storefront-db-provision | нет |
| `cache` | redis, backend | нет |
| `broker_storefront` | rabbitmq, backend, relay, commands-consumer | нет |
| `broker_operations` | rabbitmq, operations-consumer, operations-bridge, commands-relay | нет |
| `broker_admin` | rabbitmq, rabbitmq-provision | нет |
| `operations` | operations-db, operations-api, operations-consumer, operations-bridge, commands-relay, operations-migrate, operations-db-provision | нет |
| `ops_limiter` | operations-api, operations-redis | нет |
| `identity` | backend, operations-api | нет |
| `metrics_storefront` | prometheus, backend, relay, commands-consumer | нет |
| `metrics_operations` | prometheus, operations-api, operations-consumer, commands-relay | нет |
| `metrics_broker` | prometheus, rabbitmq | нет |

Связи, которые дает общая сеть сверх нужных потоков: внутри одного домена процессы видят порты друг друга;
backend видит frontend; prometheus видит все порты rabbitmq, а не только 15692; operations-api видит все порты
backend, а backend видит operations-api. Между доменами storefront и operations связь есть только через rabbitmq
и сеть `identity`.

Исключения для исходящего трафика. Маршрут наружу есть у трех сервисов, и он не ограничен по адресам:
frontend и prometheus получают его вместе с сетью, через которую публикуют порт, backend получает его ради
geocoder. У остальных сервисов маршрута из сетей Docker нет. Сузить выход до адреса geocoder может только
правило на хосте или прокси с белым списком, это условие площадки.

Операторские команды, которым нужен брокер, запускаются там, где он есть: на стороне storefront в backend,
на стороне Operations в operations-consumer (например, `dc exec operations-consumer python manage.py dlq`).

Проверка модели идет на локальном изолированном стенде (`scripts/stack_smoke.sh`): из контейнеров проверяются
нужные и запрещенные соединения и наличие маршрута наружу, geocoder заменен локальной заглушкой. Это проверка
сетей Docker, а не файрвола будущего хоста.

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

## Образы и воспроизводимость сборки

Это три разных свойства, и сейчас выполнены первые два.

1. **Базовые образы закреплены.** Каждый `FROM`, `COPY --from=`, `image:`, образ в командах `docker run`,
   `docker create` и `docker pull` из CI и smoke-скриптов и образ синтаксиса Dockerfile (`# syntax=`) указаны как
   `тег@sha256:...`.
   Повторный `pull` тега не подменит базу. Гейт `scripts/check_prod_config.sh` падает на ссылке без digest, в том
   числе на образе, который больше нигде не упоминается, и на теге, закрепленном в разных местах разными digest.
2. **Зависимости приложения закреплены lock-файлами.** Frontend ставится через `npm ci` по `package-lock.json`
   с хешами целостности. У каждого Python-пакета (backend, operations, event_contracts) зависимости задает только
   `pyproject.toml`, а `uv.lock` рядом закрепляет их все, включая транзитивные, с хешами. Образы и CI ставят их
   через `uv sync --locked`, который отказывает, если lock отстал от `pyproject.toml`. Вне lock остаются пакеты
   `apt-get` в backend (текущее состояние репозиториев Debian) и `setuptools`, которым собирается event_contracts
   (версия закреплена, хеша нет).
3. **Развертывается новая сборка, а не проверенный артефакт.** `dc build` собирает образы приложения на хосте из
   рабочей копии, с тегом ее коммита. Чтобы на хост попал ровно тот образ, который прошел CI и smoke, его
   собирают один раз, публикуют в реестр и указывают в compose по digest образа приложения. Реестр выбирается
   вместе с площадкой, до этого гарантия ограничена пунктом 1.

Обновление digest базового образа, например после выхода исправлений безопасности:

```bash
# digest индекса, общего для всех платформ, а не манифеста одной платформы
docker buildx imagetools inspect python:3.13-slim --format '{{json .Manifest}}'
# заменить digest этого тега во всех местах сразу и убедиться, что гейт зеленый
git grep -n 'python:3.13-slim@sha256'
bash scripts/check_prod_config.sh
```

Затем полный CI и smoke: новый базовый образ меняет то, на чем собираются и работают все процессы.

Изменение Python-зависимости: поправить версию в `pyproject.toml` пакета, выполнить в его каталоге `uv lock`
(`uv lock --upgrade-package <имя>` для обновления одной транзитивной) и закоммитить оба файла вместе.

# DayZ n8n bundle

Это переносимый набор workflow для связки `dayz-log-monitor` → n8n → Telegram / OpenAI-compatible LLM / `dayz-signal`. Целевая версия — n8n 2.6.4 и новее. Все workflow импортируются выключенными; в bundle нет секретов, реальных URL, chat ID или токенов.

Старые `WEBHOOK_URL` и `RAW_WEBHOOK_URL` не заменяются и не меняют формат. Новый контур подключается отдельно через `EVENTS_ENABLED`, `EVENT_WEBHOOK_URL` и `EVENT_WEBHOOK_KEY`.

## Состав

| Workflow | Назначение |
|---|---|
| `00-bootstrap-data-tables.json` | создаёт четыре Data Tables по именам |
| `10-event-gateway.json` | Header Auth, строгая проверка `dayz.event-batch.v1` и durable batch ledger |
| `15-batch-processor.json` | lease пакета, разделение `admin_view` / `public_view`, create-if-absent запись событий |
| `20-digest-llm-publisher.json` | команды игроков, admin-alert, безопасные world-анонсы, хроника и LLM-сводки |
| `30-delivery-retry-cleanup.json` | lease отправки, retry/polling, cooldown/rate limit, Telegram, Signal v1, retention |
| `90-error-alert.json` | обезличенное операционное уведомление |

`manifest.json` — машиночитаемый состав bundle. `config/policy.v1.json` — allowlist типов событий, audience ceiling, каналы и лимиты. JSON Schema лежат в `schemas/`, тестовые данные — в `fixtures/`.

## Импорт и первый запуск

1. Импортируйте JSON из `workflows/` в один n8n project в порядке из `manifest.json`. Не активируйте их.
2. Запустите `DayZ | 00 | Bootstrap Data Tables` вручную из UI работающего n8n один раз. Узлы используют `createIfNotExists` и имена `dayz_routes`, `dayz_events`, `dayz_deliveries`, `dayz_cooldowns`. В `dayz_events` записи с `record_kind=batch` образуют crash-safe batch ledger/queue, а `record_kind=event` хранят проекции событий; их ключи разделены префиксами `batch:` и `event:`.
3. Откройте Data Tables и сверьте колонки с четырьмя `Create dayz_*` узлами bootstrap workflow. `createIfNotExists` не мигрирует старую таблицу с тем же именем; несовпадающую схему нужно исправить до активации. Bootstrap дважды проверен через REST manual run в server-mode n8n 2.6.4 на новом volume. Отдельная команда `n8n execute` не загружает backend context модуля Data Tables и завершается с `module is disabled`; для этого workflow она непригодна. Если Data Tables недоступны и в UI/server execution, создайте ровно эти четыре таблицы и колонки вручную по bootstrap workflow, затем начните с shadow-проверки. Bundle намеренно не содержит вымышленных live table ID.
4. Создайте и привяжите вместо `REPLACE:` четыре credentials:

   - HTTP Header Auth `X-DayZ-Key: <EVENT_WEBHOOK_KEY>`;
   - HTTP Header Auth `Authorization: Bearer <LLM_API_KEY>` для OpenAI-compatible endpoint;
   - HTTP Header Auth `x-api-key: <SIGNAL_API_KEY>`;
   - Telegram API credential с bot token.

5. Вручную добавьте в `dayz_routes` одну строку на server ID. Образец — `config/routes.example.json`. Сначала оставьте `mode=shadow`, `enabled=false` и все `*_enabled=false`. Необязательный `rules_message_ru` задаёт ответ на `!rules`; пустое значение включает честный fallback о ненастроенных правилах. Имена таблиц переносимы; внутренние live ID в bundle не зашиты.
6. В Settings workflow `10`, `15`, `20` и `30` выберите `DayZ | 90 | Sanitized Error Alert` как Error Workflow. Ссылка на error workflow не зашита: n8n присваивает live workflow ID только при импорте.
7. Активируйте `90`, `15`, `20`, `30`, затем `10`. Для monitor укажите production URL вебхука `.../webhook/dayz/events/v1` и тот же `EVENT_WEBHOOK_KEY`, что в Header Auth.
8. Включите `EVENTS_ENABLED=true`. При `shadow` n8n примет и обработает пакеты, но delivery завершатся как `shadowed` без внешней отправки. После проверки включайте нужные каналы, затем `enabled=true` и `mode=active`.

## Входной контракт

`POST /webhook/dayz/events/v1` принимает один `dayz.event-batch.v1`. Обязательны два заголовка:

```http
X-DayZ-Key: <secret>
Idempotency-Key: batch_<32 hex>
```

`Idempotency-Key` должен точно совпадать с `batch_id`. Один запрос содержит 1…100 событий и не более 256 KiB. Точная схема — `schemas/dayz.event-batch.v1.schema.json`; эталонный пакет, созданный текущим `dayz-log-monitor`, — `fixtures/events/valid_monitor_batch.json`.

Новый пакет получает `202`, повтор — `200`; оба ответа считаются доставленными monitor. `400/409/413/422` означают ошибку контракта; `401/403` — ошибку Header Auth. Ответ не возвращает содержимое event.

Граница приватности приходит от monitor: `admin_view` может содержать псевдоним, display name и точное место; `public_view` уже обезличен. Gateway не доверяет проекции вслепую: он заново собирает её по точному allowlist для каждого типа события, запрещает неизвестные поля, raw UID/строки лога, coordinate-like ключи и строки с парами координат. Публичное место допустимо только как точный объект `{sector, size_m: 2000, precision: "coarse"}` с проверенным coarse sector. В n8n нет парсинга raw-логов.

## Каналы и LLM

Админские Telegram-тексты создаются детерминированным Code node из `admin_view`; LLM в этом пути не участвует. В LLM уходит только агрегат из `public_view`: типы, число, грубые секторы и публичные факты. `admin_view`, `facts`, source offsets, display names, event ID и точные координаты в LLM-запрос не попадают.

Интерактивные ответы на `!status`, `!weather` и `!time` строятся только из последнего `world.snapshot`, если он не старше 180 секунд. При старом или отсутствующем snapshot workflow сообщает, что данные временно недоступны. `!rules` использует только `rules_message_ru` из route либо заданный fallback. Команда игрока, его имя и другие поля события в ответ не отражаются и в LLM не передаются. Для `!sos` и `player.admin_message` администратор получает приватное Telegram-сообщение, а игровой чат — короткое анонимное подтверждение.

World lifecycle (`heli_crash`, `military_convoy`, `train`, `police_situation`) сразу даёт администратору Telegram с точным местом из `admin_view`, а публичное сообщение создаётся после 600 секунд только с грубым сектором. `contaminated_area` дополнительно даёт немедленное coarse safety-предупреждение только в игру. Переходы день/ночь и пороги дождя/тумана публикуются только при изменении snapshot, без 10-минутной задержки. Отдельная часовая хроника заражённых, транспорта и животных публикуется лишь при как минимум трёх агрегированных эпизодах; `signal_count` влияет только на статистику. Секторы из monitor-поля `sector_2km` разрешены только для когорты как минимум из трёх разных `actor_ref`; n8n не читает и не вычисляет `x/z`.

`llm_base_url` — полный OpenAI-compatible endpoint, например внутренний `/v1/chat/completions`; `llm_model` — имя модели. Допустимый ответ имеет ровно два поля: `{ "message_ru": string, "safety_flags": [] }`. Пустой/пробельный текст, C0/C1 control characters, timeout, HTTP error, невалидный JSON, непустые safety flags, URL/ID/координаты или превышение лимита включают детерминированный русский fallback; произвольный raw LLM output дальше не проходит. Один и тот же `message_ru` без отдельной английской/ASCII-версии направляется в разрешённые Telegram и game каналы.

Telegram nodes явно используют `parse_mode=HTML`, потому что n8n 2.6.4 подставляет Markdown при пустом `parse_mode`. Текст перед отправкой полностью экранирует `& < > " '` и визуально остаётся обычным текстом; attribution выключен. Error workflow не пересылает raw exception, URL, response body или bearer/token: только allowlisted workflow/execution, категорию, класс и стабильный fingerprint.

Signal вызывается как `POST <signal_base_url>/v1/broadcasts`. Body — полный `dayz.command.v1` из `schemas/dayz.command.v1.schema.json`; `command_id` имеет вид `cmd_<delivery_id>`, а заголовок `Idempotency-Key` в точности ему равен. Wire TTL остаётся в диапазоне 1–300 секунд. Новый POST начинается только при фактическом остатке TTL строго больше 105 секунд: 30 секунд lease + 60 секунд до следующего scan + 15 секунд запаса. Строка с меньшим остатком терминализируется без HTTP POST; дедлайн не продлевается. Новый или идемпотентный POST с `202` считается принятым, но не доставленным: для `queued`/`sending` workflow повторяет `GET /v1/broadcasts/<command_id>` до терминального состояния. Status GET обходит command/auto cooldown и разрешён ниже 105-секундного порога вплоть до wire expiry. В Signal передаётся исходный русский Unicode-текст; транслитерация и проверка результата как printable ASCII длиной не более 160 символов централизованы в Signal. Legacy `POST /broadcast` этот workflow не использует.

## Надёжность и retention

- Batch и delivery захватываются lease-маркером; token ограждает от позднего ответа старой попытки. Просроченный batch lease возвращается в очередь. Просроченный Telegram lease считается неоднозначной доставкой, терминализируется как `delivery_unknown` и никогда автоматически не отправляется снова.
- Game delivery использует 30-секундный lease. Просроченный game lease сначала делает GET по детерминированному `cmd_<delivery_id>`, не проходя cooldown/rate gates. Только точный авторизованный ответ `404/not_found` разрешает следующий идемпотентный POST с тем же `Idempotency-Key`; сетевой или malformed 404 не разрешает новую команду. Для `queued`/`sending` продолжается status GET, пока не истёк wire deadline.
- Delivery делает до трёх попыток: начальная и две повторные с паузами 30 и 120 секунд. Просроченные и исчерпавшие попытки строки терминализируются до выборки, а eligibility повторно проверяется при claim, поэтому старая head-of-line запись не блокирует очередь. То же правило действует для batch queue.
- Ответы на команды ограничены двумя независимыми cooldown: 60 с для игрока и 15 с для всех команд сервера.
- Автоматические игровые сообщения имеют минимальный интервал 120 с и отдельный rolling-limit не более трёх сообщений за 600 с. Значение `game_cooldown_seconds` в route может только увеличить минимальный интервал; rolling-limit остаётся отдельным ограничением.
- `processing_expires_at` сохраняет wire deadline события и запрещает позднюю публикацию; для digest/chronicle delivery берётся минимум из дедлайнов источников и локального часа. Отдельный retention `expires_at` записи не продлевает срок действия события.
- `admin_view` в event очищается через 24 ч; event удаляется через 72 ч. Нормализованный private batch payload очищается сразу после разбора и принудительно не позже 24 ч.
- Delivery metadata хранится 14 дней. Сам текст очищается после терминального исхода или при истечении delivery deadline.
- Workflow не сохраняют success/error execution payload. Для bootstrap ручное execution сохраняется только как служебный setup-результат.

Data Tables не дают переносимого unique index. Gateway сначала проверяет namespaced batch ledger и только затем делает durable insert: crash до записи оставляет retry допустимым, crash после записи превращает retry в duplicate. Event и delivery проходят по одному через `Loop Over Items`: существующий детерминированный ID никогда не обновляется, новый только вставляется; поэтому crash после insert, но до отметки source event, не сбрасывает уже отправленную/терминальную доставку при повторе.

Check-then-insert всё же не является распределённой exactly-once транзакцией. Для этого bundle production executions должны быть сериализованы: на выделенном n8n instance установите `N8N_CONCURRENCY_PRODUCTION_LIMIT=1` (или обеспечьте эквивалентную concurrency=1 сериализацию в queue mode). Без неё два строго одновременных исполнения могут оба увидеть отсутствие строки и вставить дубликат; Signal всё ещё защищён `Idempotency-Key`, но Telegram может получить повтор. При concurrency=1 обычные retry сходятся через stable IDs без повторного side effect.

105-секундный порог предполагает, что минутный delivery scan не задержан очередью других execution. `dayz_deliveries` следует мониторить по возрасту `next_attempt_at`; при backlog GET-reconciliation может не успеть до wire expiry, но новый поздний POST всё равно будет запрещён фактической проверкой remaining TTL.

## Offline-проверка

Из каталога `n8n`:

```powershell
.\tests\run.ps1
```

Или по шагам:

```powershell
node .\tools\build_workflows.js
python .\tests\validate_bundle.py
node .\tests\test_workflow_code.js
```

Эти быстрые проверки не вызывают Telegram, LLM, Signal или n8n. Они повторно генерируют workflow, проверяют JSON/граф/плейсхолдеры и исполняют Code node на fixture от реального monitor-контракта.

Для воспроизводимой runtime-проверки импорта и bootstrap нужен локальный Docker image `n8nio/n8n:2.6.4`:

```powershell
.\tests\validate_runtime.ps1
```

Скрипт использует новый одноразовый volume, запускает n8n с `--network none`, импортирует шесть workflow, выполняет bootstrap дважды через server REST API, проверяет ровно четыре таблицы и их колонки, затем удаляет контейнер и volume. Это всё ещё не заменяет shadow smoke-test после ручной привязки реальных credentials/routes.

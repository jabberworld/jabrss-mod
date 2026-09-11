# SPEC.md — JabRSS-mod

Техническая спецификация проекта, достаточная для воссоздания аналогичного приложения по описанию.

---

## 1. Назначение

JabRSS-mod — XMPP-бот-агрегатор RSS/Atom-новостей. Он мониторит RSS-фиды и доставляет заголовки новых статей подписчикам через XMPP-сообщения. Взаимодействие с пользователем строится на **presence-подписке**: чтобы пользоваться сервисом, пользователь должен подписаться на presence бота и подтвердить ответную подписку от бота. Все операции пользователь выполняет текстовыми командами в XMPP-чате.

Ключевые свойства:

- Многопоточная архитектура: один поток — XMPP-взаимодействие, другой — опрос фидов, третий (опционально, при TTY) — интерактивная консоль.
- Два SQLite-файла: основной (`jabrss.db`, пользователи/подписки/конфигурация) и ресурсный (`jabrss_res.db`, кэш фидов и элементов).
- Адаптивный интервал опроса фидов, зависящий от частоты публикации и «штрафного» коэффициента.
- HTTP-кеширование через conditional GET (ETag / Last-Modified), поддержка gzip/deflate, обработка редиректов, автодетект фида из HTML-страницы.
- Требования: Python 3, библиотеки `feedparser`, `slixmpp`, `requests`.

---

## 2. Общая архитектура

### 2.1 Потоки

| Поток | Роль |
|---|---|
| Главный (main) | Запускает asyncio event loop slixmpp: приём/отправка XMPP-станц, обработка команд, доставка сообщений. |
| Updater (daemon) | `bot.run()`: очередь обновлений фидов, HTTP-опрос, запись новых элементов, доставка заголовков. |
| Console (daemon, только при `stdin.isatty()`) | Интерактивные отладочные команды и `shutdown`. |

### 2.2 Структура модулей

| Модуль | Содержимое |
|---|---|
| `jabrssng.py` | Фасад/точка входа: `main()`, re-exports для тестов, guard `if __name__ == '__main__'`. |
| `jabrssng_stream.py` | `JabRSSStream` (ядро + композиция миксинов), `AUTH_RETRY_LIMIT`. |
| `jabrssng_commands.py` | `ChatCommandMixin` — команды `help/list/set/configuration/statistics/usage/subscribe/unsubscribe/info`. |
| `jabrssng_delivery.py` | `DeliveryMixin` — `message`/`presence`-обработчики, `_send_headlines`, `MAX_MESSAGE_SIZE`. |
| `jabrssng_roster.py` | `RosterMixin` — реконсиляция ростера, `REMOVAL_GRACE_SECS`. |
| `jabrssng_updater.py` | `UpdaterMixin` — очередь опроса фидов и доставка. |
| `jabrssng_user.py` | `JabberUser`, `DummyJabberUser`, `strip_resource`, `get_week_nr`. |
| `jabrssng_storage.py` | `DataStorage` — кэши пользователей/ресурсов. |
| `jabrssng_db.py` | `Cursor`, `FlexibleLocker`, `get_db`, `ensure_databases`. |
| `jabrssng_context.py` | Глобальные синглтоны и `init_context()`. |
| `jabrssng_config.py` | `read_config`, `TEXT_USAGE`, `CONFIG_OPTIONS`/`CONFIG_REQUIRED`. |
| `jabrssng_text.py` | `TEXT_WELCOME`, `TEXT_NEWUSER`, `TEXT_HELP`. |
| `jabrssng_console.py` | `console_handler`. |
| `parserss.py` | Парсинг и опрос фидов, HTTP-клиент, persistence, адаптивный интервал. |

### 2.3 Композиция JabRSSStream

`JabRSSStream` наследуется от миксинов (в указанном порядке) и `slixmpp.ClientXMPP`:

```python
class JabRSSStream(UpdaterMixin, RosterMixin, DeliveryMixin,
                   ChatCommandMixin, ClientXMPP): ...
```

Миксины объявлены **до** `ClientXMPP`, чтобы перенесённые методы (`message`, `presence`, `session_start`, `get_dns_records` и т.д.) перекрывали слixmpp. Методы, вызывающие «соседа» из другого миксина или из `ClientXMPP`, используют `super()`.

### 2.4 Критический дизайн-констрейнт: импорт без побочных эффектов

Импорт любого `jabrssng_*` модуля **не** должен иметь побочных эффектов:

- не парсит аргументы командной строки;
- не сканирует `sys.argv`;
- не создаёт/не открывает базы данных;
- не логирует.

Вся инициализация — в `main()` (`jabrssng.py`), которая вызывает `init_context()`. Все модули обращаются к синглтонам через `import jabrssng_context as ctx` и `ctx.db`-стиль. Запрещено `from jabrssng_context import db` — это захватило бы значение `None` до инициализации.

Это требование необходимо для офлайн-тестов, которые импортируют модули без сервера, конфига и БД.

---

## 3. Схема базы данных

Инициализация выполняется из `db.sqlite` скрипта (idempotent, все `CREATE ... IF NOT EXISTS`) с `ATTACH DATABASE "jabrss_res.db" AS res`, создавая два файла сразу.

### 3.1 Основная БД (`jabrss.db`)

**`user`**

| Столбец | Тип | Смысл |
|---|---|---|
| `uid` | INTEGER PK | ID пользователя. |
| `jid` | TEXT NOT NULL | JID (bare, lowercase). `UNIQUE`. |
| `conf` | INTEGER | Битовые флаги конфигурации (см. ниже). |
| `store_messages` | INTEGER | Максимум хранимых/доставляемых сообщений за раз (по умолч. 16, жёсткий предел 64). |
| `size_limit` | INTEGER | Лимит размера описания; **в БД хранится поделённым на 16**. |
| `since` | INTEGER | Недельный номер первой регистрации (для чистки неактивных). |
| `pending_removal` | INTEGER | UNIX-время первой фиксации «не подписан» (для грейс-периода удаления). |

Битовая маска `conf`:

- биты 0–1: тип сообщения (0 = plaintext, 1 = headline, 2 = chat, 3 = reserved);
- биты 2–4: доставка при away/XA/DND (4/8/16);
- бит 5: флаг миграции;
- биты 6–7: subject-формат (title=0x40, url=0x80);
- биты 8–9: header-формат (title=0x100, url=0x200).

**`user_stat`** — понедельная статистика доставки за последние 8 недель: столбцы `uid`, `start` (недельный номер) и пары `nr_msgsK` / `size_msgsK` для K=0..7. `UNIQUE (uid) ON CONFLICT REPLACE`.

**`user_resource`** — подписки: `uid`, `rid`, `seq_nr` (курсор последнего доставленного элемента). `UNIQUE (uid, rid) ON CONFLICT REPLACE`. Индекс по `rid`.

**Триггер** `user_delete AFTER DELETE ON user` — каскадно удаляет записи из `user_resource` и `user_stat`.

### 3.2 Ресурсная БД (`jabrss_res.db`)

**`resource`**

| Столбец | Тип | Смысл |
|---|---|---|
| `rid` | INTEGER PK | ID ресурса. |
| `url` | TEXT NOT NULL | Канонический URL (после упрощения). `UNIQUE`. |
| `last_updated` | INTEGER | UNIX-время последнего опроса. |
| `last_modified` | INTEGER | из Last-Modified заголовка. |
| `etag` | TEXT | из ETag заголовка. |
| `hash` | BLOB | MD5 тела фида (для детекта изменений). |
| `invalid_since` | INTEGER | с какого времени фид считается ошибочным. |
| `redirect` | INTEGER | rid ресурса-цели редиректа. |
| `redirect_seq` | INTEGER | seq_nr курсора для редиректа. |
| `penalty` | INTEGER | штраф 0..1024 (1024 = 1.0). |
| `err_info` | TEXT | текст последней ошибки. |
| `title`/`description`/`link` | TEXT | метаданные канала. |

**`resource_history`** — кольцевой буфер последних 16 «интервалов»: пары `time_itemsK` / `nr_itemsK` (K=0..15). `UNIQUE (rid) ON CONFLICT REPLACE`.

**`resource_data`** — элементы фида: `rid`, `seq_nr` (монотонно растущий курсор), `published`, `title`, `link`, `descr_plain`, `descr_xhtml`, `guid`. `UNIQUE (rid, seq_nr) ON CONFLICT REPLACE`. Держится не более `NR_ITEMS = 96` последних элементов на фид.

**Триггер** `resource_delete AFTER DELETE ON resource` — каскадно удаляет `resource_history` и `resource_data`.

### 3.3 Транзакционная модель

Обе БД используют ручные транзакции через класс `Cursor`:

- `Cursor.begin()`: захватывает глобальный мьютекс (`ctx.db_sync` для основной БД, `RSS_Resource._db_sync` для ресурсной), затем `BEGIN`.
- `Cursor.commit()`: `COMMIT`, затем освобождает мьютекс.
- Поддерживается вложенность через `parent` (дочерний курсор делегирует родителю и не открывает собственной транзакции).
- Поддержка `FlexibleLocker` — условный/отложенный захват (полезен при уже удержанном lock'е).

`get_db()` открывает `jabrss.db` с `isolation_level=None` и `PRAGMA synchronous=NORMAL`. `RSS_Resource_db()` аналогично для `jabrss_res.db`.

**Правило:** не использовать сырой `sqlite3.Connection` без `Cursor` — иначе ломается состояние lock'а.

---

## 4. Модули и логика

### 4.1 Конфигурация (`jabrssng_config.py`)

`read_config(argv=None)` возвращает dict `{jid, host, port, password, user_agent}`.

Приоритет: **командная строка > конфиг-файл > интерактивный промпт**. Отсутствующие обязательные опции при TTY запрашиваются (`getpass` для пароля) и **сохраняются в конфиг-файл** (создаётся с правами `0600`); при отсутствии обязательных значений и non-TTY — `sys.exit(2)` с сообщением.

Конфиг-файл: INI, секция `[jabrss]`, ключи `jid`, `host`, `password`, `resource`, `user_agent`. `CONFIG_OPTIONS = ('jid','host','password','resource')`, `CONFIG_REQUIRED = ('jid','password')`.

CLI-опции: `-c/--config`, `-f/--password-file` (пароль из первой строки), `-h/--connect-host`, `-p/--password`, `-j/--jid`, `-r/--resource`, `--help`.

Обработка `host`: поддерживается `host:port`; порт отщепляется (`rpartition(':')`, если правая часть — число), иначе по умолчанию `5222`.

Выбор ресурса: CLI/конфиг → resource часть JID → `socket.gethostname()`. Итоговый JID = `bare + '/' + resource`.

### 4.2 Контекст и синглтоны (`jabrssng_context.py`)

Синглтоны на уровне модуля (изначально `None`): `db`, `db_sync`, `main_res_db`, `storage`, `dummy_user`.

`init_context(user_agent=None)` выполняет:

1. `init_parserss(db_fname='jabrss_res.db', dbsync_obj=parserss_dbsync)`; если задан `user_agent` — повторно `init_parserss` с тем же lock-объектом;
2. читает `http_proxy`/`https_proxy` из env (принимает только `http://`-style), пишет в `RSS_Resource.http_proxy`;
3. `sqlite3.enable_shared_cache(True)` (если доступно, non-standard);
4. `ensure_databases()` (создание БД);
5. `db = get_db()`, `db_sync = threading.Lock()`, `main_res_db = RSS_Resource_db()`, `storage = DataStorage()`, `dummy_user = DummyJabberUser()`.

`log_message(*msg)` логирует в INFO через логгер `JabRSS`.

### 4.3 Слой БД (`jabrssng_db.py`)

- `get_db()` — подключение к основной БД.
- `ensure_databases()` — если `jabrss.db` или `jabrss_res.db` отсутствует, выполняет схему из `db.sqlite` (путь рядом с модулем). При отсутствии схемы или ошибке — `sys.exit(2)`. Затем `_ensure_pending_removal_column()`.
- `_ensure_pending_removal_column()` — idempotent `ALTER TABLE user ADD COLUMN pending_removal INTEGER` (для миграции существующих БД; ошибка игнорируется).
- `Cursor` — см. разд. 3.3.
- `FlexibleLocker(lock, active=True)` — контекстный менеджер; `lock()`/`unlock()`/`locked()`/`replace(lock)` (замена lock'а без двойной блокировки). При `active=False` не блокирует (используется как маркер).

### 4.4 Пользователь (`jabrssng_user.py`)

`get_week_nr()` — недельный номер (от 01.01.1970) с учётом UTC и смещения на 84*3600 секунд.

`JabberUser`:

- Атрибуты: `_jid` (bare, lowercase), `_uid`, `_res_ids` (список rid), `_configuration` (bitmask), `_store_messages` (по умолч. 16), `_size_limit` (по умолч. `None` → 0/1024 в геттере), `_jid_resources` (resource → show), `_show`, статистика.
- `__init__(jid, jid_resource, show=None, create=False)`: загружает из БД (`user`, `user_resource`, `user_stat`) или создаёт при `create` (вставка с `since=get_week_nr()`). `size_limit` при загрузке умножается на 16 (обратно к «живому» значению).
- `set_*`/`get_*` сеттеры под каждую часть bitmask (см. разд. 3.1). `set_size_limit` ограничен 3072, `get_size_limit` возвращает 1024 при недопустимом 0; `set_store_messages` clamp 0..64.
- Presence-модель: `set_presence(jid_resource, show)` обновляет `_jid_resources`; `_update_presence()` вычисляет минимальный (наиболее «строгий») show; `presence()` возвращает `_show`. `show` кодируется: 0=online, 1=chat, 2=away, 3=xa, 4=dnd; `None`→-1.
- `get_delivery_state(presence=None)`: доставлять, если show 0/1, либо (для 2/3/4) если соответствующий бит `also_deliver` установлен.
- `add_resource`/`remove_resource` (предусловие: resource already locked): обновляют `_res_ids` и res→uid-маппинг в `DataStorage`; при удалении последнего подписчика ресурс эвиктируется. Бросают `ValueError` при дубликате/отсутствии.
- `headline_id(resource)` → `seq_nr` курсор; `update_headline(resource, headline_id, new_items)` обновляет курсор и, при новых элементах, статистику (добавляет кол-во и суммарный размер `title+link+descr_plain`).
- `_adjust_statistics()` — сдвиг 8-недельного окна вперёд; `_commit_statistics()` пишет в `user_stat`.
- `get_statistics()` → `(stat_start, nr_headlines, size_headlines)`.

`DummyJabberUser` — фейковый пользователь без БД (`_uid=-1`, `_show='xa'`, `get_delivery_state()=False`, no-op `_commit_statistics`/`_update_configuration`). Используется как placeholder для удержания ресурса при обработке редиректа, чтобы он не был эвиктирован.

### 4.5 Хранилище (`jabrssng_storage.py`)

`DataStorage`:

- `_users` — dual-key: jid-строка и uid. `len(_users)//2` = число пользователей.
- `_resources` — dual-key: url-строка и rid. `len(_resources)//2` = число ресурсов.
- `_res_uids` — rid → список uid подписчиков (кэш).
- `_users_sync`, `_resources_sync` — мьютексы; `_redirect_db` — подключение для обработки редиректов.
- `get_resource(url, res_db, lock=True, follow_redirect=True)` → ресурс (при `lock=True` **уже заблокирован**, вызывающий обязан разблокировать). Создаёт `RSS_Resource`, ставит в оба кэша, планирует первый апдейт через `RSS_Resource.schedule_update`.
- `get_cached_resource(url)` → из кэша или `KeyError`.
- `get_resource_by_id(rid)` → из кэша или загрузка по url (`RSS_Resource_id2url`).
- `evict_resource/resource` — удаление из кэшей.
- `get_resource_uids(resource)` (предусловие `resources_sync`) → список uid, инициализируя кэш запросом `user_resource`.
- `get_user(jid)` → `(user, jid_resource)` или `KeyError`; `get_user_by_id(uid)`.
- `load_user(jid, presence_show, create=False)` → `(user, jid_resource)` или `(None, None)`. Кэширует по jid+uid, загружает ресурсы пользователя.
- `evict_user`, `evict_all_users`, `remove_user` (DELETE из `user` + evict).
- `_redirect_cb` — колбэк для редиректов: получает целевой ресурс, держит его dummy-пользователем, вызывает `update`, собирает `redirects`-список.

### 4.6 Команды (`ChatCommandMixin`)

Маршрутизация команд выполняется в `DeliveryMixin.message` по префиксам (см. 4.7). Здесь — реализация `_process_*`:

- `_process_help` — отвечает `TEXT_HELP`.
- `_process_list` — перечисляет подписки пользователя, отсортированные; при ошибке ресурса добавляет ` (Error: ...)` либо ` (error)`. Пусто → «Sorry, you are currently not subscribed to any RSS feeds.»
- `_process_set(stanza, user, argstr)`:
  - `plaintext`/`headline`/`chat` → `set_message_type`.
  - `also_deliver [Away] [XA] [DND] [none]` → `set_delivery_state` (биты 1/2/4).
  - `store_messages <n>` → `set_store_messages`.
  - `size_limit <n>` → `set_size_limit`.
  - `header {Title|URL}` / `subject {Title|URL}` → `_parse_format` (title=1, url=2, `<empty>`=0) → `set_header_format`/`set_subject_format`.
  - неизвестное — «Unknown configuration option»; исключение — «Unknown error setting configuration option».
- `_process_config` — выводит текущие: message type, also_deliver, subject/header format, store_messages, size_limit.
- `_process_statistics` — «Users online/total» (online = `len(_users)//2`) и «RDF feeds used/total» (used = `len(_resources)//2`).
- `_process_usage` — понедельная статистика пользователя (8 недель, формат `дд/мм - дд/мм: N headlines (размер)`, kiB если >11 KiB).
- `_process_subscribe(stanza, user, argstr)` — для каждого URL: `get_resource`, `add_resource`, прочитать заголовки с 0 и сразу прислать новые; обработка `UrlError` («Error (...) subscribing...»), `ValueError` («already subscribed»), прочего («couldn't be subscribed»).
- `_process_unsubscribe` — `get_cached_resource`, `remove_resource`; `UrlError`/`KeyError`/`ValueError`/прочее — отдельные сообщения об ошибках.
- `_process_info(stanza, user, argstr)` — детальная информация о фиде: Last polled / Last updated (из history) / Next poll / Update interval (~мин) / Feed penalty (из 1024) / Error / частота за месяц-неделю-день.

### 4.7 Доставка и presence (`DeliveryMixin`)

`message(stanza)`:

- Если `type` пуст или `body` пуст — игнор. Логирует.
- `sender.user == ''` (доменный адрес) — игнор.
- типы `normal`/`chat`: требуют существующего `get_user` (иначе `KeyError` → traceback). Диспетчер команд по префиксам (`help`/`?`, `list`, `set `, `configuration`/`conf`, `stats`/`statistics`/`show statistics`, `usage`/`show usage`, `subscribe `/`add `/`+ `, `unsubscribe `/`del `/`- `, `info `). Неизвестная команда → не более 2 предупреждений на пользователя (защита от пинг-понга роботов).
- тип `headline` — молча игнор (чтобы не отвечать на собственные headline-сообщения).
- тип `error` / прочие — игнор с логированием.

`presence(stanza)` — диспетчер по `type`:

- available/chat/away/xa/dnd → `presence_available`.
- unavailable/error → `presence_unavailable`.
- subscribe/subscribed/unsubscribe/unsubscribed → соответствующие обработчики.

`presence_available`:

- определяет show (0..4), загружает пользователя; если не найден — игнор.
- если `get_delivery_state` истинно — для каждого ресурса блокирует, читает заголовки с курсора `headline_id`, при новых элементах шлёт `_send_headlines`; при редиректе переносит подписку на целевой ресурс; обновляет курсор `update_headline`.

`presence_unavailable` — ставит show=-1; если суммарный presence<0 — эвиктирует пользователя (`evict_user`).

`presence_subscribe` — отправляет приветствие (`TEXT_WELCOME` + `TEXT_NEWUSER` для нового), создаёт пользователя (`load_user(..., create=True)`), отправляет `subscribed` и `subscribe`.

`presence_subscribed` — `load_user(..., create=True)`.

`presence_unsubscribe` — `_unsubscribe_user` + `_delete_user`.

`presence_unsubscribed` — `_unsubscribe_user(..., send_unsubscribed=False)` + `_remove_user` + `_delete_user`.

`_send_headlines(user, resource, items, not_stored=False)`:

- Тип 0/2 (normal/chat): формирует тело сообщения — необязательная header-строка `[ title: url ]`, при превышении `store_messages` — строка «N headlines suppressed», список элементов `title\nlink\n[descr]` (descr ограничен `size_limit`); при превышении `MAX_MESSAGE_SIZE = 20000` — разбивает на несколько сообщений. Тип `chat`, если `message_type != 0`, иначе `normal`. Subject = `_format_header(subject_format)`.
- Тип 1 (headline): для каждого элемента — отдельное headline-сообщение с OOB (`jabber:x:oob`, url+desc). Subject = subject_format.
- `_format_header(title, url, res_url, format)`: format 1=title, 2=url, 3=`title: url`, иначе пусто.

`_reply(stanza, body)` и `_send_stanza(stanza)` находятся в `jabrssng_stream.py` (см. 4.10).

### 4.8 Ростер-реконсиляция (`RosterMixin`)

`roster_update_event(iq)` — обработчик события `roster_update` от slixmpp (наступает и при полном `result`, и при инкрементальном `set`-push).

**Ветка `iq['type'] == 'result'`** (полный/версионированный ответ):

1. Разбирает `items` из `iq['roster']['items']`; subscriber = `subscription in ('both','from')`, остальные считаются non-subscriber.
2. Читает `total_users` из БД.
3. **Условие-защита**: если `total_users > 0` и `items` пуст — результат недоверенный (иначе удалил бы всех): логирует и выходит. Различает два случая:
   - есть атрибут `ver` → это ответ roster-versioning «ничего не изменилось» → «roster unchanged (ver=...), not reconciling N user(s)»;
   - нет `ver` → действительно пустой роcтер → «roster result is empty but N user(s) are in the database; skipping cleanup».
   Непустой `items` **доверяется**: даже если подписчиков 0, пользователи уходят в грейс-обработку (удалить мгновенно нельзя, см. ниже).
4. Для non-subscriber элементов в ответе — основывает серверный roster: `_unsubscribe_user` + `_remove_user`.
5. Сравнивает БД с подписчиками: помечает `stale_entries` (uid с `/` в jid — старая форма), `not_subscribed` (в БД, но не подписан), `restored_users` (в БД и подписан; очищает `pending_removal`).
6. Удаляет stale-записи.
7. Для каждого `not_subscribed` по грейс-логике: `pending_removal==None` → пометить (mark, на `REMOVAL_GRACE_SECS`); просрочено ≥ grace → удалить (`_unsubscribe_user` + `_delete_user`); иначе → pending. Логирует сводку «roster synchronization finished».
8. Создаёт записи `user` для новых подписчиков (`since=get_week_nr()`, defaults).
9. Удаляет пользователей неактивных > 40 недель (по `since` и `user_stat.start`, query с `week_nr - 3` / `week_nr - 32`).

**Ветка `else` (инкрементальный push, `set`)**: для каждого элемента с `subscription in ('remove','none')` вызывает `_delete_user_after_grace(JID(user))`.

`_delete_user_after_grace(jid)` — по одному пользователю: если `pending_removal` пуст — пометить и отложить; если просрочен grace — `_unsubscribe_user` + `_delete_user`; иначе пере-логировать остаток ожидания.

`_delete_user(jid)` — загружает, удаляет его подписки (через `remove_resource` на каждом ресурсе с lock), затем `storage.remove_user`.

**Ключевые константы/механики:**

- `REMOVAL_GRACE_SECS = 7 * 24 * 60 * 60` (7 дней).
- Удаление никогда не мгновенное: сначала mark (`pending_removal`=timestamp), реально удаляется через grace — это защищает от ложных срабатываний.
- **roster versioning**: в `session_start` (см. 4.10) перед запросом полного ростера ставится `self.client_roster.version = None` — иначе slixmpp хранит версию между реконнектами и сервер отвечает пустым «no changes», из-за чего реконсиляция реально выполнялась бы только на первом подключении. После запроса slixmpp сам перезаписывает `ver`, инкрементальные push'и продолжают работать.

### 4.9 Опрос фидов и доставка (`UpdaterMixin`)

`schedule_update(resource)` — вставляет `(next_update, resource)` в `self._update_queue` (список, поддерживаемый `bisect.insort`), уведомляет condition при вставке в голову.

`run()` — цикл updater-потока:

- после стартовой задержки 20с открывает собственные `db`/`res_db` (в `get_db()` и `RSS_Resource_db()`), ставит `ctx.storage._redirect_db = db`;
- зацикливается, пока `_term_flag` не установлен: ожидает `wait(timeout)` до `next_update` ближайшего; когда наступило — берёт ресурс из головы, вызывает `_update_resource`, планирует следующий раунд;
- при пустой очереди — `wait()` без таймаута;
- при завершении закрывает соединения и выставляет `_term` event.

`_update_resource(resource, db, res_db)`:

1. Пропускает, если фид уже имеет редирект (`redirect_info != None`).
2. Проверяет, используется ли ресурс (есть ли онлайн-пользователь среди `get_resource_uids`); если нет — эвиктирует и не обновляет.
3. Под lock-дисциплиной (`FlexibleLocker` для redir и resource sync, `Cursor` для БД) вызывает `resource.update(...)`.
4. Для новых элементов обновляет курсоры подписчиков (через вложенный `Cursor(parent=cursor)`), собирает `deliver_users` (только у кого `get_delivery_state()`).
5. Разблокирует ресурс **до** отправки (иначе мёртвый замок: main-поток, нужный для отправки, ждал бы ресурс).
6. Доставляет: `_send_headlines(user, resource, new_items, not_stored=True)` — по одному сразу, вне транзакции.
7. Обрабатывает редиректы (перенос подписок) и список `redirects` (доп. ресурсы, для которых тоже нужно доставить).

### 4.10 Поток/протокол (`jabrssng_stream.py`)

`JabRSSStream.__init__(jid, host, password, port=5222)`:

- инициализирует очередь обновлений и condition;
- monkey-patch: `RSS_Resource.schedule_update = self.schedule_update` (патчит глобально — планирование идёт через бота);
- настраивает SCRAM: отключает `*-PLUS` механизмы (`use_mechs` — список, не set; иначе игнорируется; необходимо для строгих серверов, отклоняющих channel binding);
- регистрирует плагины `xep_0030` (disco), `xep_0092` (version), `xep_0199` (ping);
- `auto_authorize = None`, `auto_subscribe = False` — только собственная presence-обработка;
- вешает event-хендлеры: `session_start`, `message`, `presence`, `roster_update`, `session_bind`, `auth_success`, `connected`, `disconnected`, `failed_auth`, `failed_all_auth`, `no_auth`;
- `iq get/set` fallback-хендлеры через `Callback`/`StanzaPath`;
- SRV: `tls_services = {'xmpps-client'}` (direct TLS), `starttls_services = {'xmpp-client'}` (STARTTLS).

`get_dns_records(domain, port)` — разрешение конечных точек: сначала `_xmpps-client` SRV (порт 5223), затем `_xmpp-client`; при отсутствии — прямой A/AAAA. Возвращает список `(service, host, address, port)`.

`session_start(event)` (async):

1. логирует; принудительно сбрасывает `client_roster.version = None` и `await self.get_roster()` (см. 4.8), ловит исключения;
2. `send_presence()`, `_online = True`, `update_presence()`;
3. пере-планирует keepalive: «Ping keepalive» каждые 60с, «Presence keepalive» каждые 900с.

`update_presence()` — статус `%d/%d users, %d/%d feeds` (online/total users, used/total feeds), отправляет presence с этим статусом.

Обработчики соединения:

- `_on_session_bind` — добавляет disco identity `('client','bot')` и features `jabber:iq:last`, `jabber:iq:time`, `urn:xmpp:time`.
- `_on_connected` — логирует peer (host:port); при неаутентифицированных «тихих» реконнектах накапливает `_quiet_reconnects`, по достижению `AUTH_RETRY_LIMIT` → `_auth_giveup`.
- `_on_auth_success` — сбрасывает счётчики.
- `_on_failed_auth` — логирует отклонённый механизм SASL; сбрасывает `_quiet_reconnects`.
- `_on_auth_failed_all` — накапливает `_auth_retries`; если `< AUTH_RETRY_LIMIT` — «retrying in ~60 seconds»; иначе `_auth_giveup`.
- `_auth_giveup(reason)` — логирует, ставит `_term_flag`, будит updater, `disconnect()`.
- `_on_disconnected` — `evict_all_users()`, `_online=False`, «stream closed»; если `terminated()` — стоп loop; иначе reconnect-backoff: базовая задержка 15с, +45с если последний разрыв был <30с назад, затем `_reconnect_once`.

`_ping_keepalive()` / `_ping_done(future)` — пингует бот-домен через `xep_0199` с таймаутом 30с; при `IqTimeout` — «ping timeout» и `transport.close()`.

`_reply(stanza, body)` — отвечает тем же типом и subject; `_send_stanza(stanza)` — отправка из main-потока напрямую, из других потоков — через `call_soon_threadsafe`.

IQ-fallback (`iq_get_fallback`/`iq_set_fallback`):

- пропускает namespaces, обрабатываемые плагинами (disco#info/items, version, ping);
- `jabber:iq:time` → `_reply_legacy_time`; `jabber:iq:last` → `_reply_last` (seconds=0); `urn:xmpp:time` → `_reply_urn_time`;
- `iq set` с `jabber:iq:roster` — пропускает (обрабатывает ClientXMPP);
- иначе `_reply_service_unavailable` (`<service-unavailable/>`, `cancel`, оба типа `get` и `set`).

`terminate(timeout)` — ставит `_term_flag`, будит updater, ждёт `_term`.

### 4.11 Консоль (`jabrssng_console.py`)

`console_handler(bot)` — цикл чтения строк с stdin. Команды:

- `debug locks` — состояние всех мьютексов и заблокированных ресурсов;
- `debug resources` / `debug users` — перечень ключей кэшей;
- `dump user <jid>` — детальная информация пользователя;
- `statistics` — как `_process_statistics`;
- `shutdown` — выход.

По EOF или `shutdown` — `bot.terminate()` и `bot.loop.call_soon_threadsafe(bot.disconnect)`.

### 4.12 UI-тексты (`jabrssng_text.py`)

`TEXT_WELCOME`, `TEXT_NEWUSER`, `TEXT_HELP` — статические сообщения (приветствие, инструкция подписки, список поддерживаемых команд).

---

## 5. Логика фидов и HTTP (`parserss.py`)

### 5.1 Инициализация и конфигурация

Модульные глобалы: `INTERVAL_DIVIDER = 3`, `MIN_INTERVAL = 1*60`, `MAX_INTERVAL = 24*60*60`, `MAX_XML_SIZE = 4 MiB`, `DB_FILENAME = 'parserss.db'`, `USER_AGENT = 'JabRSS (http://jabrss.cmeerw.org)'`.

`init_parserss(db_fname, min_interval, max_interval, interval_div, dbsync_obj, user_agent)` — перезаписывает глобалы и `RSS_Resource._db_sync` (по умолчанию `Null_Synchronizer`). Должна вызываться до создания `RSS_Resource`.

Класс `RSS_Resource` имеет классовые атрибуты `_db_sync`, `http_proxy` (классовый, ставится из env контекстом).

### 5.2 Валидация URL (`split_url`)

Разбирает `urlsplit`. Ограничения:

- протокол только `http`/`https`;
- http порт допускает `None`/`80`/`http`; https — `None`/`443`/`https`; иначе `UrlError`;
- приватные/локальные адреса отклоняются: IPv4 10.x, 172.16-31.x, 192.168.x, >=240.x; для остальных валидность по 4-частному IPv4 или (для не-IP) по домену из ≥2 частей с алфавитным последним сегментом;
- IPv6 — по квадратным скобкам;
- пустой путь → `/`; ведущие `//` схлопываются.

Возвращает `(protocol, host, path)`. Ошибки — `UrlError(ValueError)`.

### 5.3 Тексты/нормализация

- `html2plain(html)` — HTML-фрагмент → plain text через HTMLParser (контрол переносов для `br/p/div/tr/pre`, маркеры ` * ` для `li`, `alt/title` для `img`, обработка char/entity refs; при неудаче возвращает исходную строку).
- `normalize_text` — переводит управляющие символы в пробелы, схлопывает пустые строки/пробелы.
- `normalize_obj` — применяет `normalize_text` ко всем строковым атрибутам объекта.
- `normalize_item` — для элемента: `descr`→`descr_plain`/`descr_xhtml`, обрезает до 4096.

### 5.4 feedparser-адаптер

`FeedParser(base_url)`:

- `feed(data)` накапливает сырые данные.
- `close()`: парсит через `feedparser.parse(raw)`. При `bozo` фиксирует error_log.
- Определяет «это фид»: есть entries ИЛИ у feed есть title/link/id. Если нет — пробует HTML-автодетект (`_try_autodiscovery` + `links` с `rel=alternate` и feed-типом); найденный URL резолвится относительно base и возвращается как `redirect_url`; иначе `FeedError`.
- Извлекает channel info (title/subtitle/description, link, id, published/updated).
- Для каждого entry: title, description (приоритет `content` по рангу типа → `summary`), link (приоритет feedburner/pheedo origlink → `link`), enclosure (если тип `audio/mpeg` — переопределяет link), guid, published.
- Ссылки резолвятся относительно base через `urljoin`.

### 5.5 HTTP-опрос (`RSS_Resource.update`)

Сигнатура: `update(db=RSS_Resource_db, redirect_count=5, redirect_cb=default_redirect_cb)` → `([item], next_item_id, redirect_resource, redirect_seq, [redirects])`.

Алгоритм:

1. Санити-чек: пропускает, если с момента `_last_updated` < 60с.
2. Отмечает `_last_updated = now`; если нет `_invalid_since` — начинает считать сбой.
3. Сессия `requests.Session` с `User-Agent = USER_AGENT`.
4. Обрабатывает до `redirect_count` редиректов (301 постоянный не штрафуется, 302/307 — штраф+1).
5. Формирует заголовки conditional GET: `If-Modified-Since`, `If-None-Match` из кэша.
6. Проверяет redirect cycle (посещённые `(protocol, host, path)`).
7. TLS: сначала `verify=True`; при `SSLError` — фолбэк `verify=False` для этого фида, `_insecure=True`, логирует предупреждение (также подавляет urllib3-предупреждение).
8. По статусу ответа:
   - 304/412 → фид валиден, `_invalid_since = None`;
   - 2xx → скачивает (лимит `MAX_XML_SIZE` в 4 MiB decompressed), обновляет `last_modified`/`etag`, парсит `FeedParser`; при автодетекте-редиректе продолжает цикл; иначе сохраняет MD5-хэш тела, обновляет channel info, обрабатывает новые элементы;
   - 3xx → читает `location`, редирект (через `redirect_cb`) или ошибка;
   - иначе → HTTP-ошибка в `err_info`.
9. Собирает `error_info` для различных типов исключений (timeout/HTTP/connection/socket/IO/feed/assert/encoding/misc).
10. Сохраняет `err_info`, пересчитывает `penalty` (см. 5.6), пишет обновлённые `last_modified, last_updated, etag, invalid_since, penalty`.

`_process_new_items(new_items, cursor)`:

- `get_headlines(0)` → текущие элементы и курсор; `first_item_id = next - len(items)`.
- `_update_items(items, new_items)` → число новых.
- Дедупликация `compare_items(l, r)`: совпадение по title+guid, иначе сравнение scheme/path/query/fragment и домена (со схлопыванием суффиксов для обобщённого сравнения).
- Ограничение: не более `NR_ITEMS = 96` элементов; при превышении удаляет хвост из БД и локально.
- Очищает `_invalid_since`; при наличии новых — обновляет `resource_history` (кольцо из 16) и записывает элементы в `resource_data` с последовательными `seq_nr`.

`_update_items(items, new_items)`: вычищает новые по cutoff (медиана/первый `published`, если записей достаточно), сортирует по `published`, добавляет не найденные.

`get_headlines(first_id, db_cursor, db)` → `([item], last_id)` — элементы с `seq_nr >= first_id`, отсортированные.

### 5.6 Адаптивный интервал и штраф

`_penalty` (0..1024) обновляется в `update`:

- 2xx + новые элементы: `penalty = (5*penalty)//6` (поощрение);
- 2xx без изменений (hash совпал): `penalty = (3*penalty)//4 + 256` (штраф);
- 2xx изменён, но нет новых: `penalty = (15*penalty)//16 + 64`;
- 304: `penalty = (3*penalty)//4` (поощрение);
- временный редирект: `penalty = (7*penalty)//8 + 128`.

`next_update(randomize=True)` — возвращает `_last_updated + interval [+ random]`:

- с историей ≥2 интервалов: `interval = time_span // sum_items // INTERVAL_DIVIDER`, бонус `32*interval // (64 - penalty//28)`; возможно сужение окна на «старом»/«новом» периоде при всплеске/затухании активности;
- история =1: `interval = 30*60 + time_span//3`;
- иначе (shtml-invalid): `interval = 4*60*60 + time_span//4`;
- новый без истории: `interval = 8*60*60`;
- спец-случай slashdot.org: `+150 мин`;
- кламп `[min_interval, max_interval]`;
- при `randomize` — +`int(random.normalvariate(30, 50 + interval//50))`.

### 5.7 Вспомогательные

- `RSS_Resource_db()` — подключение к ресурсной БД.
- `RSS_Resource_id2url(res_id)` — url по rid или `KeyError`.
- `RSS_Resource_simplify(url)` — канонизация URL (валидирует и возвращает).
- CLI (`__main__`): для URL из argv создаёт ресурс, следует редиректам, вызывает `update`, печатает channel info, ошибку и новые элементы.

---

## 6. Команды пользователя (справочник)

| Команда (синонимы) | Действие |
|---|---|
| `help` / `?` | Список команд (`TEXT_HELP`). |
| `subscribe <url>` / `add <url>` / `+ <url>` | Подписка на фид; сразу доставляет текущие новые заголовки. |
| `unsubscribe <url>` / `del <url>` / `- <url>` | Отписка. |
| `list` | Список подписок (с пометками `(error)`/`(Error: ...)`). |
| `info <url>` | Информация о фиде (последний опрос/обновление, следующий опрос, интервал, штраф, частота, ошибка). |
| `set plaintext` | Тип сообщения = normal plaintext. |
| `set chat` | Тип сообщения = chat plaintext. |
| `set headline` | Тип сообщения = headline (OOB). |
| `set also_deliver [Away] [XA] [DND] [none]` | Доставлять при away/XA/DND. |
| `set store_messages <n>` | Скользящее окно (лимит 64, по умолч. 16). |
| `set size_limit <n>` | Лимит описания (до 3072). |
| `set header [Title] [URL]` | Строка-заголовок в сообщении. |
| `set subject [Title] [URL]` | Subject сообщения. |
| `configuration` / `conf` | Текущая конфигурация. |
| `show statistics` / `statistics` / `stats` | Статистика сервера (онлайн/всего юзеров и фидов). |
| `show usage` / `usage` | Понедельная статистика пользователя. |

---

## 7. Тестирование (офлайн-набор)

- `tests/passing` — контракт импортов из `jabrssng`: `Cursor`, `FlexibleLocker`, `JabRSSStream`, `JabberUser`, `get_db`, `ensure_databases` должны быть callable; `hasattr(JabRSSStream, 'send_frontend')` (из `ClientXMPP`). Ключевая проверка — импорт модулей без побочных эффектов (не создаётся БД, не требуется конфиг/argv). Печатает `failures=N`, exit 0/1.
- `tests/smoke_test.py` — импортирует реальные `jabrssng_context`/`jabrssng_stream`, вызывает `init_context`, строит `JabRSSStream`, пытается подключиться к localhost:5222 (ожидаемый чистый отказ), ждёт 2с, печатает `SMOKE OK`. Импортируемый, тонкий стаб.
- `tests/probe.py`, `tests/hstest.py`, `tests/srvprobe.py` — вспомогательные XMPP-клиенты (disco/version/last/ping/time-запросы, SRV-проверка) против живого сервера для ручной проверки.

---

## 8. Сводка ключевых констант

| Константа | Значение | Где |
|---|---|---|
| `AUTH_RETRY_LIMIT` | 3 | jabrssng_stream |
| `MAX_MESSAGE_SIZE` | 20000 | jabrssng_delivery |
| `REMOVAL_GRACE_SECS` | 7*24*60*60 | jabrssng_roster |
| `NR_ITEMS` | 96 | parserss |
| `INTERVAL_DIVIDER` | 3 | parserss |
| `MIN_INTERVAL` | 60 | parserss |
| `MAX_INTERVAL` | 24*60*60 | parserss |
| `MAX_XML_SIZE` | 4 MiB | parserss |
| `size_limit` max | 3072 (default 1024) | jabrssng_user |
| `store_messages` limit | 64 (default 16) | jabrssng_user |
| reconnect delay | 15s (+45s при частых) | jabrssng_stream |
| keepalive ping | 60s | jabrssng_stream |
| presence keepalive | 900s | jabrssng_stream |
| description truncation | 4096 | parserss |
| inactive user removal | > 40 недель | jabrssng_roster |

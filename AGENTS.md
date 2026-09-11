# Project agent instructions
<!-- CODE_BRAIN_MANDATORY -->
## Code Brain MCP - Mandatory when loaded

Use Code Brain MCP first for substantive tasks in this project.

- Start with `start_task(runIntake=true)` or `neural_sync`.
- Use `agent_plan` before `agent_code` for chunk and deep work.
- Use `memory_retrieve` at intake and `memory_store` at task end.
- Use `uncertainty_guard` before storing conclusions.
- Disable duplicate MCPs with `get_superseded_mcps`.
<!-- CODE_BRAIN_MANDATORY -->

---

# AGENTS.md

## Project Overview

JabRSS is an XMPP-based RSS/Atom feed aggregator and notification bot. It monitors RSS/Atom feeds and delivers new headlines to users via XMPP (Jabber) messages. The project is GPL-2 licensed, authored by Christof Meerwald.

## Architecture

### Components

| Entry Point | Purpose |
|---|---|
| `jabrssng.py` | Facade/entry point: parses config, initialises the context, builds `JabRSSStream`, starts the XMPP loop and the updater/console threads, and runs the shutdown sequence. Also re-exports `Cursor`, `FlexibleLocker`, `JabRSSStream`, `JabberUser`, `get_db`, `ensure_databases` for the offline test suite. Importing it is side-effect free (`main()` does all the work). |
| `jabrssng_stream.py` | `JabRSSStream` (composed from the mixins below + `slixmpp.ClientXMPP`): connection/SRV resolution, auth, reconnects, keepalive, IQ fallbacks (`jabber:iq:time`/`last`/`urn:xmpp:time`), `session_start` and presence status. |
| `jabrssng_commands.py` | `ChatCommandMixin`: the user-facing commands (`help`, `list`, `set`, `configuration`, `statistics`, `usage`, `subscribe`, `unsubscribe`, `info`). |
| `jabrssng_delivery.py` | `DeliveryMixin`: the `message`/`presence` stub handlers (message falls back to `load_user` when `get_user` cache-misses), subscription presence flows, `_send_headlines` message formatting (plaintext/chat/headline + OOB). |
| `jabrssng_roster.py` | `RosterMixin`: server-roster reconciliation (`roster_update_event`), user deletion incl. the removal grace period (`_delete_user_after_grace`). |
| `jabrssng_updater.py` | `UpdaterMixin`: the feed-polling update queue (`schedule_update`, `run`, `_update_resource`), running in a separate thread. |
| `jabrssng_user.py` | `JabberUser` / `DummyJabberUser` entity, `strip_resource`, `get_week_nr`. |
| `jabrssng_storage.py` | `DataStorage`: dual-key caches for users and resources, resource eviction, redirect callback. |
| `jabrssng_db.py` | `Cursor`, `FlexibleLocker`, `get_db`, `ensure_databases`, `_ensure_pending_removal_column`. |
| `jabrssng_context.py` | The global singletons (`db`, `db_sync`, `main_res_db`, `storage`, `dummy_user`) and `init_context()`. See "Global State". |
| `jabrssng_config.py` | CLI/config parsing (`read_config`, precedence command line > config file > prompt) and `TEXT_USAGE`. |
| `jabrssng_text.py` | `TEXT_WELCOME`, `TEXT_NEWUSER`, `TEXT_HELP`. |
| `jabrssng_console.py` | Interactive console handler (debug commands, shutdown). |
| `parserss.py` | RSS/Atom feed parser, fetcher, and resource manager. Handles HTTP fetching (`requests`), parsing (`feedparser`), item comparison/caching, adaptive polling intervals, and SQLite storage. |
| `jabrss.conf` / `jabrss.conf.example` | INI configuration of the bot (JID, password, host, resource, user_agent). See "Running". |
| `tests/` | Offline tests that exercise the bot logic without a live XMPP server (`tests/passing` runs the suite). |

### Data Flow

1. **Feed polling**: `RSS_Resource.update()` in `parserss.py` fetches feeds via HTTP (`requests`, with conditional GET support), parses them with the `feedparser`-based adapter, compares items, and stores new headlines in SQLite.
2. **XMPP delivery**: `JabRSSStream` in `jabrssng_stream.py` (with its mixins) handles incoming XMPP stanzas. For message commands, it looks up the `JabberUser`, processes the command, and sends reply messages. When feeds update, the updater thread (`jabrssng_updater.py`) delivers headlines as XMPP messages to subscribed online users.

### Database

- **`jabrss.db`** (main): User data, subscriptions, configuration. Tables: `user`, `user_resource`, `user_stat`.
- **`jabrss_res.db`** (resources): RSS feed cache, item storage, polling state. Configured via `init_parserss()`.
- **`parserss.db`**: Default resource DB for standalone `parserss` usage (overridden by `jabrssng.py` to `jabrss_res.db`).
- **`db.sqlite`**: not a database but the **SQL schema script** for the bot databases (`jabrss.db` + `jabrss_res.db` via `ATTACH`); the bot runs it automatically (`ensure_databases()` in `jabrssng_db.py`, invoked from `jabrssng_context.init_context()`) when `jabrss.db` or `jabrss_res.db` is missing. All `CREATE` statements are `IF NOT EXISTS`, so the script is idempotent.
- SQL schemas are in `subscriptions.sql` and `vacuum.sql` (next to the scripts; originally under `etc/`).

Both `jabrssng` (via `jabrssng_db.py`) and `parserss.py` define their own `Cursor` class for transactional SQLite access with explicit locking (thread-safe). The `parserss.py` `Cursor` uses `RSS_Resource._db_sync` as the lock; `jabrssng`'s `Cursor` uses `jabrssng_context.db_sync` (a `threading.Lock()`).

## Running

The bot is run directly as a Python script (no launcher shell script):

```bash
python3 jabrssng.py [-c <config>] [-j <jid>] [-h <host>] [-p <password> | -f <password-file>]
```

Configuration lives in an INI file `jabrss.conf` (section `[jabrss]`, keys `jid`, `host`, `password`, `resource`, `user_agent`), located next to `jabrssng.py` by default; use `-c`/`--config` for an alternate path. Options are resolved with the precedence **command line > config file > interactive prompt**, and values entered interactively are saved back to the config file (created with mode `0600` if missing). A commented template with all options is provided as `jabrss.conf.example`. `user_agent` (the HTTP User-Agent sent when polling feeds) is read from the config file only and defaults to `JabRSS (http://jabrss.cmeerw.org)`.

- `-c` / `--config`: path to config file (default: `jabrss.conf` next to the script)
- `-j` / `--jid`: XMPP JID (e.g., `jabrss@example.com`)
- `-r` / `--resource`: XMPP resource (default: resource of the JID, else hostname)
- `-h` / `--connect-host`: XMPP server hostname/IP (note: `-h` is **not** help); a port may be appended with a colon, e.g. `linuxoid.in:5222`
- `-p` / `--password`: Password (or use `-f` for a file)
- If an option is absent from both the command line and the config file, it is requested interactively (prompt) when stdin is a TTY; otherwise the bot exits with status 2 with a clear message.
- Environment variable `http_proxy` / `https_proxy` are respected for feed fetching.
- The bot is expected to run from the project directory (DB files `jabrss.db` / `jabrss_res.db` are opened relative to CWD); the provided `jabrss.service` systemd unit sets `WorkingDirectory` accordingly.
- Under a non-TTY (e.g., systemd), the interactive console handler is skipped.

Python dependencies are `feedparser`, `slixmpp`, and `requests`. Install them with `python3 -m pip install feedparser slixmpp requests`.

## Key Patterns & Gotchas

### Python 3

The bot is Python 3 only. Imports of the `jabrssng_*` modules must be side-effect free (formatted text and class/function definitions only) so that the test suite can import them without a live server, a config, or databases.

### SQLite Concurrency Model

Both `Cursor` classes implement manual transaction management (`BEGIN`/`COMMIT`) with explicit locks (`threading.Lock`). This is critical for thread safety:

- In `parserss.py`: `Cursor.begin()` acquires `RSS_Resource._db_sync` before `BEGIN`, releases after `COMMIT`.
- In `jabrssng` (via `jabrssng_context.db_sync`, `jabrssng_db.py`): Same pattern using `threading.Lock()`.
- The `FlexibleLocker` class in `jabrssng_db.py` allows conditional lock acquisition (useful when already holding a lock).
- **Do not** use raw `sqlite3.Connection` without the `Cursor` wrapper — you'll corrupt the lock state.

### RSS_Resource Locking

`RSS_Resource` objects have an explicit lock (`resource.lock()` / `resource.unlock()`). When `DataStorage.get_resource()` returns a resource, it is **already locked** and must be unlocked by the caller. Forgetting to unlock will deadlock.

### Global State

- `parserss.py` uses module-level globals (`DB_FILENAME`, `MIN_INTERVAL`, `MAX_INTERVAL`, `INTERVAL_DIVIDER`, `USER_AGENT`, ...) initialized by `init_parserss()`. These must be set before creating `RSS_Resource` instances.
- `jabrssng_context.py` holds the global singletons: `db` (main DB), `db_sync`, `main_res_db` (resource DB), `storage` (`DataStorage`), `dummy_user`. They are `None` until `init_context()` runs (called from `main()`). **Import the module and reference `ctx.db`**; do **not** do `from jbrssng_context import db` (star binding captures the pre-init `None`). The singletons are never set at import time so importing any `jabrssng_*` module is side-effect free — no config parsing, no DB creation, no argument scanning.
- `init_context(user_agent=None)` (in `jabrssng_context.py`) calls `init_parserss`, applies `http_proxy`/`https_proxy` from the environment, enables the shared cache, runs `ensure_databases`, and constructs `db`, `db_sync`, `main_res_db`, `storage`, `dummy_user`. `init_parserss` is re-invoked with the configured `user_agent` after the config file is parsed (running inside `main()`).
- Everything runs via `main()` (guarded by `if __name__ == '__main__'`), so importing `jabrssng` from tests never requires command-line arguments or creates databases.
- `RSS_Resource.schedule_update` is monkey-patched in `JabRSSStream.__init__` to delegate to the bot's update queue.

### URL Validation

`split_url()` in `parserss.py` restricts URLs to http (port 80) and https (port 443) only. Non-standard ports are rejected with `UrlError`. Private/local IPs are also rejected.

### Configuration Bitflags

`JabberUser._configuration` is a bitmask:
- Bits 0-1: message type (0=plaintext, 1=headline, 2=chat)
- Bits 2-4: deliver when away/XA/DND
- Bit 5: migration flag
- Bits 6-7: subject format
- Bits 8-9: header format

`size_limit` is stored in the DB divided by 16 and multiplied back on load (see `JabberUser.__init__`).

### Roster Synchronization / Removal Grace Period

On every session start (and on reconnect) `JabRSSStream` fetches the server-side roster and reconciles the `user` table with it (`roster_update_event` in `jabrssng_roster.py`). `session_start` forces a **full, unversioned** roster fetch by clearing `client_roster.version` before `get_roster()` (`set_ver(None)` omits the `ver` attribute, RFC 6121) — otherwise slixmpp's roster versioning would answer the request with an empty "no changes" result on every reconnect (the version persists across reconnects), and the reconciliation would effectively only ever run once per process. Users present in the DB but not subscribed (`both`/`from`) in the roster are **not deleted immediately**: they are first marked in the `pending_removal` column (UNIX timestamp) and actually deleted (subscriptions included) only once the missing subscription persists for `REMOVAL_GRACE_SECS` (default 7 days). If the subscription comes back before that, the marker is cleared ("subscription restored, removal cancelled"). The same grace period applies to per-item roster pushes reporting `remove`/`none` (via `_delete_user_after_grace`).

**Safety guard**: only an **empty** roster result (no `<item>` elements at all) while the DB still holds users is treated as untrustworthy — the cleanup is skipped with a log message ("roster result is empty ... skipping cleanup") instead of removing every user. A non-empty result with a `ver` attribute but no items is a roster-versioning "no changes" reply and is logged as "roster unchanged (ver=...)" (also skipped); any result with items is trusted, so the grace-period handling (which cannot delete immediately) also covers the case where the roster lists contacts but none are subscribed.

The `pending_removal` column is part of `db.sqlite`'s `user` table for fresh installs; for pre-existing databases `_ensure_pending_removal_column()` (`ALTER TABLE ... ADD COLUMN`, idempotent) runs inside `ensure_databases()` on every start. Careful when editing this logic: `roster_update_event` runs inside a `Cursor(db)` transaction — do not call `storage.*` or open a nested `Cursor` while that lock is held (deadlock), collect decisions first.

### Feed Parsing

Feed parsing is delegated to the `feedparser` library: `Feed_Parser` (in `parserss.py`) adapts the feedparser results (titles, summaries, links, ids) to the internal item format and converts HTML fragments to plain text via `html2plain()`. If the feed body is not valid RSS/Atom (e.g. an HTML
error page), parsing falls back to HTML link autodiscovery (`RetryAsHtml`). HTTP fetching itself is done with `requests` (not via `feedparser.parse(url)`).

### User-Agent

The HTTP User-Agent used when polling feeds is held in the module-level global `USER_AGENT` (in `parserss.py`). It is set by `init_parserss(user_agent=...)`; `jabrssng_context.init_context()` applies the `user_agent` config key by re-invoking `init_parserss` inside `main()` (after the config file is parsed, reusing the same lock object).

### OpenSSL/TLS

`JabRSSStream` connects to the XMPP server via `slixmpp` (including `starttls_proceed()` with `ssl.create_default_context()`). XMPP stream handling, SASL auth, and SRV resolution are provided by `slixmpp` rather than a custom library.

## Conventions

- **Naming**: Classes use `CamelCase` with underscores within compound names (`JabberUser`, `RSS_Resource`, `Feed_Parser`). Functions use `snake_case`. Private attributes use single underscore prefix (`_jid`, `_show`).
- **Mixin composition**: `JabRSSStream` is built from `ChatCommandMixin`, `DeliveryMixin`, `RosterMixin`, `UpdaterMixin` (declared in that order, before `slixmpp.ClientXMPP`) so the moved handlers override slixmpp defaults. A method that calls a sibling that may come from another mixin or from `ClientXMPP` must use `super()`. Keep the mixins free of module-level state — singletons come from `jabrssng_context`.
- **Dual-key dictionaries**: `DataStorage._users` is indexed by both JID string and uid (integer), so values appear twice. Similarly, `_resources` is indexed by both URL and resource ID. Iterating over `._users` or `._resources` requires dividing length by 2.
- **License headers**: All source files have the GPL-2 license header with the FSF address.
- **No package manifest**: No `requirements.txt`, `setup.py`, or `pyproject.toml` exists. Dependencies are imported directly: `slixmpp`, `feedparser`, `requests`.
- **Tests**: An offline test suite lives in `tests/` (`tests/passing` runs it). `tests/smoke_test.py` imports the real modules (`jabrssng_context.init_context`, `jabrssng_stream.JabRSSStream`), initialises the context, and only then attempts a connection — it stands in for the live XMPP server.
- **No CI/CD**: No CI configuration files exist.
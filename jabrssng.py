#!/usr/bin/python
# Copyright (C) 2001-2011, Christof Meerwald
# http://jabrss.cmeerw.org
#
# 2022 - 2026, rain @ JabberWorld
# https://jabberworld.info
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 dated June, 1991.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307
# USA
#
# Threading overview:
#
#   main thread     - XMPP event loop (slixmpp)
#   console thread  - interactive console_handler (only with a TTY)
#   updater thread  - bot.run(): polls feeds, delivers headlines
#
# Importing this module has no side effects: the configuration is only
# parsed and the databases/singletons are only initialized once main()
# runs. The actual logic lives in the jabrssng_* modules, see
# jabrssng_stream.py (JabRSSStream and its mixins).


import asyncio, logging, os, sys, threading, time


from jabrssng_config import read_config
from jabrssng_console import console_handler
from jabrssng_context import init_context, log_message
from jabrssng_stream import JabRSSStream


# re-exported for the offline test suite (tests/passing)
from jabrssng_db import Cursor, FlexibleLocker, ensure_databases, get_db
from jabrssng_user import JabberUser


def main():
    # configure the logging handler (INFO level) before anything else so
    # that startup messages are visible as well
    logger = logging.getLogger()
    logger.addHandler(logging.StreamHandler())
    #logger.setLevel(logging.DEBUG)
    logger.setLevel(logging.INFO)

    config = read_config()

    # initialise parserss, the databases and the global singletons
    # (db, db_sync, main_res_db, storage, dummy_user)
    init_context(config['user_agent'])

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    bot = JabRSSStream(config['jid'], config['host'], config['password'],
                       config['port'])
    updater_thread = threading.Thread(target=bot.run, daemon=True)
    updater_thread.start()
    if sys.stdin.isatty():
        threading.Thread(target=console_handler, args=(bot,), daemon=True).start()

    if config['host'] is not None:
        log_message('connecting to %s:%d (configured)' %
                    (config['host'], config['port']))
        bot.connect(host=config['host'], port=config['port'])
    else:
        log_message('resolving connection endpoint for %s via DNS/SRV' %
                    (config['jid'].host,))
        bot.connect()

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        log_message('KeyboardInterrupt')
    finally:
        # order an orderly shutdown: tell the updater thread to stop and
        # ask slixmpp to close the stream. The updater thread is a daemon
        # thread, so a Ctrl+C (or a stuck feed fetch) cannot hang the
        # interpreter at exit; we still give it a bounded grace period and
        # ultimately force-exit so a busy slixmpp DNS/executor thread
        # cannot prevent termination either.
        try:
            log_message('shutting down...')
            bot.terminate(timeout=10)
            bot.loop.call_soon_threadsafe(bot.disconnect)
            updater_thread.join(10)
            log_message('exiting...')
            time.sleep(1)
        finally:
            os._exit(0)


if __name__ == '__main__':
    main()
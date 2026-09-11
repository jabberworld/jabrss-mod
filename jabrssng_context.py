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


import logging, os, sqlite3, threading

from parserss import RSS_Resource
from parserss import RSS_Resource_db
from parserss import init_parserss


# global singletons; they stay None until init_context() runs during the
# bot's startup (in main()). Importing any jabrssng_* module therefore has
# no side effects. All modules reference the singletons via this module
# (e.g. "import jabrssng_context as ctx; ctx.db") so the late lifetime is
# respected -- do not use "from jabrssng_context import db", that would
# bind the pre-init None value.
db = None
db_sync = None
main_res_db = None
storage = None
dummy_user = None


logger = logging.getLogger('JabRSS')


def log_message(*msg):
    logger.info(' '.join(msg))


parserss_dbsync = threading.Lock()


# initialise everything the bot needs before it can start polling feeds
# and handling XMPP messages: parserss globals (with an optional custom
# HTTP User-Agent), proxy settings, the databases and the global
# singletons. Called exactly once from the bot's main().
def init_context(user_agent=None):
    global db, db_sync, main_res_db, storage, dummy_user

    from jabrssng_db import ensure_databases, get_db
    from jabrssng_storage import DataStorage
    from jabrssng_user import DummyJabberUser

    init_parserss(db_fname='jabrss_res.db', dbsync_obj=parserss_dbsync)
    if user_agent is not None:
        # re-apply the parserss settings with the same lock object so the
        # global is in place before any feeds are fetched
        init_parserss(db_fname='jabrss_res.db', dbsync_obj=parserss_dbsync,
                      user_agent=user_agent)

    http_proxy = os.getenv('http_proxy')
    if http_proxy and (http_proxy[:7] == 'http://'):
        http_proxy = http_proxy[7:]
        if http_proxy[-1] == '/':
            http_proxy = http_proxy[:-1]
    else:
        http_proxy = None

    https_proxy = os.getenv('https_proxy')
    if https_proxy and (https_proxy[:7] == 'http://'):
        https_proxy = https_proxy[7:]
        if https_proxy[-1] == '/':
            https_proxy = https_proxy[:-1]
    else:
        https_proxy = None

    RSS_Resource.http_proxy = http_proxy
    if hasattr(sqlite3, 'enable_shared_cache'):
        # non-standard, but useful
        sqlite3.enable_shared_cache(True)

    ensure_databases()
    db = get_db()
    db_sync = threading.Lock()

    main_res_db = RSS_Resource_db()

    storage = DataStorage()
    dummy_user = DummyJabberUser()
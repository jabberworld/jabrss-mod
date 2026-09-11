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
# Offline smoke test: instead of duplicating the whole bot (as it used
# to), this imports the real jabrssng_* modules, initialises the bot
# state (databases + singletons) and then exercises the bot object up to
# the network layer -- the XMPP connection attempt to localhost fails
# cleanly, which is exactly what this test verifies.


import asyncio, logging, os, sys

from slixmpp import JID

# allow running from the tests/ directory (the jabrssng modules live in
# the repo root next to this directory)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from jabrssng_context import init_context, log_message
from jabrssng_stream import JabRSSStream


# global singletons + databases (created in the current directory)
init_context(user_agent=None)

logger = logging.getLogger()
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)


# build the bot exactly like the real entry point does, but with test
# credentials and without starting the updater thread; the connection
# attempt to localhost is expected to fail (no XMPP server running)
bot = JabRSSStream(JID('jabrss@localhost/ops'), 'localhost', 'secret', 5222)

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


def _check(future):
    try:
        future.result()
    except Exception as e:
        log_message('smoke connect result:', str(e))


connection = bot.connect(host='localhost', port=5222)
connection.add_done_callback(_check)
loop.call_later(2.0, loop.stop)
loop.run_forever()
log_message('SMOKE OK')
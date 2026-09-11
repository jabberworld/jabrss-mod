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


from slixmpp import JID

from parserss import RSS_Resource

import jabrssng_context as ctx

from jabrssng_context import log_message
from jabrssng_db import Cursor, get_db


def console_handler(bot):
    db = get_db()

    try:
        while True:
            s = input()
            s = ' '.join(map(lambda x: x.strip(), s.split()))

            if s == '':
                pass
            elif s == 'debug locks':
                # show all locked objects
                log_message('db_sync', str(ctx.db_sync.locked()),
                            'users_sync', str(ctx.storage._users_sync.locked()),
                            'resources_sync', str(ctx.storage._resources_sync.locked()),
                            'RSS_Resource._db_sync', str(RSS_Resource._db_sync.locked()))
                for res in ctx.storage._resources.values():
                    if res._lock.locked():
                        log_message('resource %s' % (res._url,))

                log_message('done dumping locked objects')
            elif s == 'debug resources':
                resources = ctx.storage._resources.keys()
                res_ids = list(filter(lambda x: type(x) == type(0), resources))
                res_urls = list(filter(lambda x: type(x) != type(0), resources))
                resources = res_ids + res_urls
                log_message(repr(resources))
            elif s == 'debug users':
                users = list(ctx.storage._users.keys())
                users_ids = list(filter(lambda x: type(x) == type(0), users))
                users_jids = list(filter(lambda x: type(x) != type(0), users))
                users = users_ids + users_jids
                log_message(repr(users))
            elif s.startswith('dump user '):
                try:
                    user, resource = ctx.storage.get_user(JID(s[10:].strip()))

                    log_message('jid: %s, uid: %d' % (repr(user.jid()), user.uid()))
                    log_message('resources: %s' % (repr(user._jid_resources.items()),))
                    log_message('presence: %d' % (user.presence(),))
                    log_message('delivery state: %d' % (user.get_delivery_state(),))
                    log_message('statistics: %s' % (repr(user.get_statistics()),))
                except KeyError:
                    log_message('user not online')
            elif s == 'statistics':
                total_users, total_resources = 0, 0
                with Cursor(db) as cursor:
                    result = cursor.execute('SELECT (SELECT COUNT(uid) FROM user), (SELECT COUNT(DISTINCT rid) FROM user_resource)')
                    for total_users, total_resources in result:
                        pass

                log_message('Users online/total: %d/%d' %
                            (len(ctx.storage._users) // 2, total_users))
                log_message('RDF feeds used/total: %d/%d' %
                            (len(ctx.storage._resources) // 2, total_resources))

            elif s == 'shutdown':
                break
            else:
                log_message('Unknown command \'%s\'' % (s,))

    except EOFError:
        pass

    # initiate a clean shutdown
    log_message('JabRSS shutting down...')
    del db

    bot.terminate()
    bot.loop.call_soon_threadsafe(bot.disconnect)
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


import time

import jabrssng_context as ctx

from jabrssng_context import log_message
from jabrssng_db import Cursor


def strip_resource(jid):
    pos = jid.find('/')
    if pos != -1:
        jid = jid[:pos]

    return jid.lower()

def get_week_nr():
    t = int(time.time())
    gmtime = time.gmtime(t)

    # converting old entries:
    # 1775 + x/7; 1827 + x/7
    week_nr = t - ((gmtime[3]*60 + gmtime[4])*60 + gmtime[5])
    week_nr -= gmtime[6]*24*60*60
    week_nr += 84*60*60
    week_nr //= 7*24*60*60

    return week_nr


class JabberUser:
    ##
    # self._jid
    # self._uid
    # self._uid_str
    # self._res_ids
    # self._configuration & 0x0003 .. message type
    #   (0 = plain text, 1 = headline messages, 2 = chat message, 3 = reserved)
    # self._configuration & 0x001c .. deliver when away
    #   (4 = away, 8 = xa, 16 = dnd)
    # self._configuration & 0x0020 .. migration flag
    # self._configuration & 0x00c0 .. feed title/URL in message subject
    #   (0x40 .. title, 0x80 .. URL)
    # self._configuration & 0x0300 .. feed title/URL in message text
    #   (0x100 .. title, 0x200 .. URL)
    # self._store_messages .. number of messages that should be stored
    # self._size_limit .. limit the size of descriptions
    # self._stat_start .. first week corresponding to _nr_headlines[-1]
    # self._nr_headlines[8] .. number of headlines delivered (per week)
    # self._size_headlines[8] .. size of headlines delivered (per week)
    #
    # self._unknown_msgs .. number of unknown messages received
    ##
    def __init__(self, jid, jid_resource, show=None, create=False):
        self._jid = jid
        if jid_resource != None:
            self._jid_resources = {jid_resource : show}
        else:
            self._jid_resources = {}
        self._update_presence()

        self._configuration = 0
        self._store_messages = 16
        self._size_limit = None

        with Cursor(ctx.db) as cursor:
            cursor.execute('SELECT uid, conf, store_messages, size_limit FROM user WHERE jid=?',
                           (self._jid,))
            row = cursor.fetchone()
            if row != None:
                self._uid, self._configuration, self._store_messages, self._size_limit = row
            elif create:
                cursor.execute('INSERT INTO user (jid, conf, store_messages, size_limit, since) VALUES (?, ?, ?, ?, ?)',
                               (self._jid, self._configuration, self._store_messages, self._size_limit, get_week_nr()))
                self._uid = cursor.lastrowid
            else:
                raise KeyError(jid)

            if self._size_limit == None:
                self._size_limit = 0
            else:
                self._size_limit *= 16


            self._res_ids = []
            result = cursor.execute('SELECT rid FROM user_resource WHERE uid=?',
                                    (self._uid,))
            for row in result:
                self._res_ids.append(row[0])

            self._stat_start = 0
            self._nr_headlines = []
            self._size_headlines = []
            self._unknown_msgs = 0

            result = cursor.execute('SELECT start, nr_msgs0, nr_msgs1, nr_msgs2, nr_msgs3, nr_msgs4, nr_msgs5, nr_msgs6, nr_msgs7, size_msgs0, size_msgs1, size_msgs2, size_msgs3, size_msgs4, size_msgs5, size_msgs6, size_msgs7 FROM user_stat WHERE uid=?',
                           (self._uid,))
            for row in result:
                self._stat_start = row[0]
                self._nr_headlines = list(row[1:9])
                self._size_headlines = list(row[9:17])

        self._adjust_statistics()


    def _adjust_statistics(self):
        new_stat_start = get_week_nr()
        shift = new_stat_start - self._stat_start

        self._nr_headlines = self._nr_headlines[shift:]
        self._size_headlines = self._size_headlines[shift:]
        self._stat_start = new_stat_start

        if len(self._nr_headlines) < 8:
            self._nr_headlines += (8 - len(self._nr_headlines)) * [0]
        if len(self._size_headlines) < 8:
            self._size_headlines += (8 - len(self._size_headlines)) * [0]

    def _commit_statistics(self, db_cursor=None):
        with Cursor(ctx.db, db_cursor) as cursor:
            cursor.execute('INSERT INTO user_stat (uid, start, nr_msgs0, nr_msgs1, nr_msgs2, nr_msgs3, nr_msgs4, nr_msgs5, nr_msgs6, nr_msgs7, size_msgs0, size_msgs1, size_msgs2, size_msgs3, size_msgs4, size_msgs5, size_msgs6, size_msgs7) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           tuple([self._uid, self._stat_start] + self._nr_headlines + self._size_headlines))


    def uid(self):
        return self._uid

    def jid(self):
        return self._jid


    # @return (day of year, [nr_headlines])
    def get_statistics(self):
        return (self._stat_start, self._nr_headlines, self._size_headlines)


    def set_message_type(self, message_type):
        self._configuration = (self._configuration & ~0x0003) | (message_type & 0x0003)
        self._update_configuration()

    def get_message_type(self):
        return self._configuration & 0x0003


    def set_subject_format(self, format):
        self._configuration = (self._configuration & ~0x00c0) | (((format ^ 0x1) & 0x0003) << 6)
        self._update_configuration()

    def get_subject_format(self):
        return ((self._configuration & 0x00c0) >> 6) ^ 0x1


    def set_header_format(self, format):
        self._configuration = (self._configuration & ~0x0300) | ((format & 0x0003) << 8)
        self._update_configuration()

    def get_header_format(self):
        return (self._configuration & 0x0300) >> 8


    def set_size_limit(self, size_limit):
        if size_limit > 0:
            self._size_limit = min(size_limit, 3072)
        else:
            self._size_limit = 0
        self._update_configuration()

    def get_size_limit(self):
        if self._size_limit > 0:
            return min(self._size_limit, 3072)
        else:
            return 1024


    def set_store_messages(self, store_messages):
        self._store_messages = min(64, max(0, store_messages))
        self._update_configuration()

    def get_store_messages(self):
        return self._store_messages


    def get_deliver_when_away(self):
        return self._configuration & 0x4

    def get_deliver_when_xa(self):
        return self._configuration & 0x8

    def get_deliver_when_dnd(self):
        return self._configuration & 0x10

    def set_delivery_state(self, state):
        self._configuration = (self._configuration & ~0x001c) | ((state & 7) << 2)
        self._update_configuration()


    def _update_configuration(self):
        with Cursor(ctx.db) as cursor:
            cursor.execute('UPDATE user SET conf=?, store_messages=?, size_limit=? WHERE uid=?',
                           (self._configuration, self._store_messages, self._size_limit // 16, self._uid))

    def set_configuration(self, conf, store_messages, size_limit):
        self._configuration = conf
        self._store_messages = store_messages
        self._size_limit = size_limit
        self._update_configuration()

    def get_configuration(self):
        return (self._configuration, self._store_messages, self._size_limit)


    def _update_presence(self):
        new_show = -1
        for show in self._jid_resources.values():
            if (show >= 0) and ((show < new_show) or (new_show < 0)):
                new_show = show

        self._show = new_show

    def set_presence(self, jid_resource, show):
        if show == None:
            return

        if show >= 0:
            self._jid_resources[jid_resource] = show
        else:
            try:
                del self._jid_resources[jid_resource]
            except KeyError:
                pass

            if jid_resource == '':
                for res in list(self._jid_resources.keys()):
                    try:
                        del self._jid_resources[res]
                    except KeyError:
                        pass

        self._update_presence()

    # @throws KeyError
    def presence(self, jid_resource=None):
        if jid_resource == None:
            return self._show
        else:
            return self._jid_resources[jid_resource]


    def get_delivery_state(self, presence=None):
        if presence == None:
            presence = self.presence()

        # self._configuration & 0x001c .. deliver when away
        #   (4 = away, 8 = xa, 16 = dnd)
        return (presence in (0, 1)) or \
            ((presence == 2) and (self._configuration & 0x4)) or \
            ((presence == 3) and (self._configuration & 0x8)) or \
            ((presence == 4) and (self._configuration & 0x10))


    def resources(self):
        return self._res_ids

    # @precondition resource.locked()
    # @throws ValueError
    def add_resource(self, resource, seq_nr=None, db_cursor=None):
        res_id = resource.id()
        if res_id not in self._res_ids:
            self._res_ids.append(res_id)

            # also update storage res->uid mapping
            with ctx.storage.resources_sync():
                res_uids = ctx.storage.get_resource_uids(resource, db_cursor)
                res_uids.append(self.uid())

            with Cursor(ctx.db, db_cursor) as cursor:
                cursor.execute('INSERT INTO user_resource (uid, rid, seq_nr) VALUES (?, ?, ?)',
                               (self._uid, res_id, seq_nr))
        else:
            raise ValueError(res_id)

    # @precondition resource.locked()
    # @throws ValueError
    def remove_resource(self, resource, db_cursor=None):
        res_id = resource.id()

        self._res_ids.remove(res_id)

        # also update storage res->uid mapping
        with ctx.storage.resources_sync():
            res_uids = ctx.storage.get_resource_uids(resource, db_cursor)
            try:
                res_uids.remove(self.uid())
            except ValueError:
                pass

        if len(res_uids) == 0:
            ctx.storage.evict_resource(resource)

        with Cursor(ctx.db, db_cursor) as cursor:
            cursor.execute('DELETE FROM user_resource WHERE uid=? AND rid=?',
                           (self._uid, res_id))

    def headline_id(self, resource, db_cursor=None):
        with Cursor(ctx.db, db_cursor) as cursor:
            result = cursor.execute('SELECT seq_nr FROM user_resource WHERE uid=? AND rid=?',
                                    (self._uid, resource.id()))

            headline_id = None
            for row in result:
                headline_id = row[0]

        if headline_id == None:
            headline_id = 0

        return headline_id


    def update_headline(self, resource, headline_id, new_items=[],
                        db_cursor=None):
        with Cursor(ctx.db, db_cursor) as cursor:
            cursor.execute('UPDATE user_resource SET seq_nr=? WHERE uid=? AND rid=?',
                           (headline_id, self._uid, resource.id()))

            if new_items:
                self._adjust_statistics()
                self._nr_headlines[-1] += len(new_items)
                items_size = 0
                for item in new_items:
                    items_size += len(item.title) + len(item.link)
                    if item.descr_plain != None:
                        items_size += len(item.descr_plain)
                self._size_headlines[-1] += items_size
                self._commit_statistics(cursor)


class DummyJabberUser(JabberUser):
    def __init__(self):
        self._jid = None
        self._show = 'xa'
        self._jid_resources = {None: self._show}

        self._configuration = 0x20
        self._store_messages = 0
        self._size_limit = 0

        self._uid = -1

        self._res_ids = []

        self._stat_start = 0
        self._nr_headlines = []
        self._size_headlines = []


    def _commit_statistics(self, db_cursor=None):
        pass

    def _update_configuration(self):
        pass



    def _update_presence(self):
        pass

    def get_delivery_state(self, presence=None):
        return False


    # @precondition resource.locked()
    # @throws ValueError
    def add_resource(self, resource, seq_nr=None, db_cursor=None):
        res_id = resource.id()
        log_message('dummy adding res', str(res_id), str(len(self._res_ids)))

        if res_id not in self._res_ids:
            self._res_ids.append(res_id)

            # also update storage res->uid mapping
            with ctx.storage.resources_sync():
                res_uids = ctx.storage.get_resource_uids(resource, db_cursor)
                res_uids.append(self.uid())
        else:
            raise ValueError(res_id)

    # @precondition resource.locked()
    # @throws ValueError
    def remove_resource(self, resource, db_cursor=None):
        res_id = resource.id()
        log_message('dummy removing res', str(res_id), str(len(self._res_ids)))

        if len(self._res_ids) == 0:
            return

        self._res_ids.remove(res_id)

        # also update storage res->uid mapping
        with ctx.storage.resources_sync():
            res_uids = ctx.storage.get_resource_uids(resource, db_cursor)
            try:
                res_uids.remove(self.uid())
            except ValueError:
                pass

        if len(res_uids) == 0:
            ctx.storage.evict_resource(resource)


    def headline_id(self, resource, db_cursor=None):
        return 0


    def update_headline(self, resource, headline_id, new_items=[],
                        db_cursor=None):
        pass
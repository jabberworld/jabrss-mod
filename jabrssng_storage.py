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


import sys, threading, traceback

import jabrssng_context as ctx

from parserss import RSS_Resource, RSS_Resource_id2url
from parserss import RSS_Resource_simplify

from jabrssng_context import log_message
from jabrssng_db import Cursor, FlexibleLocker
from jabrssng_user import JabberUser


class DataStorage:
    def __init__(self):
        self._users = {}
        self._users_sync = threading.Lock()
        self._resources = {}
        self._res_uids = {}
        self._resources_sync = threading.Lock()

        self._redirect_db = None


    def _redirect_cb(self, redirect_url, db, redirect_count,
                     generate_id, connect_timeout, timeout):
        redirect_resource = self.get_resource(redirect_url, db)

        # prevent resource from being evicted until redirect is processed
        with Cursor(self._redirect_db) as cursor:
            try:
                ctx.dummy_user.add_resource(redirect_resource, None, cursor)
            except ValueError:
                pass

        redirect_resource.unlock()

        new_items, next_item_id, redirect_target, redirect_seq, redirects = redirect_resource.update(db, redirect_count, redirect_cb = ctx.storage._redirect_cb)

        if len(new_items) > 0:
            redirect_resource.unlock()
            redirects.insert(0, (redirect_resource, new_items, next_item_id))
        elif (redirect_target != None) or (redirect_resource._invalid_since):
            with redirect_resource.sync():
                with Cursor(self._redirect_db) as cursor:
                    try:
                        ctx.dummy_user.remove_resource(redirect_resource, cursor)
                    except ValueError:
                        pass

        if redirect_target != None:
            redirect_resource = redirect_target

        return redirect_resource, redirects


    def users_sync(self):
        return self._users_sync

    def resources_sync(self):
        return self._resources_sync


    # get resource (by URL) from cache, database or create new object
    # @param res_cursor db cursor for resource database
    # @return resource (already locked, must be unlocked)
    def get_resource(self, url, res_db=None, lock=True, follow_redirect=True):
        resource_url = RSS_Resource_simplify(url)

        with FlexibleLocker(self.resources_sync(), lock) as resources_locker:
            while resource_url != None:
                cached_resource = True

                try:
                    resource = self._resources[resource_url]
                    resources_locker.unlock()
                    if lock:
                        resource.lock()
                    resources_locker.lock()
                except KeyError:
                    resources_locker.unlock()
                    resource = RSS_Resource(resource_url, res_db)
                    if lock:
                        resource.lock()
                    resources_locker.lock()

                    cached_resource = False

                if follow_redirect:
                    resource_url, redirect_seq = resource.redirect_info(res_db)
                else:
                    resource_url, redirect_seq = None, None

                if resource_url != None and lock:
                    resource.unlock()

            if not cached_resource:
                self._resources[resource.url()] = resource
                self._resources[resource.id()] = resource
                RSS_Resource.schedule_update(resource)

        return resource

    # @throws KeyError
    def get_cached_resource(self, url):
        resource_url = RSS_Resource_simplify(url)

        with self.resources_sync():
            return self._resources[resource_url]

    def get_resource_by_id(self, res_id, res_db=None, follow_redirect=False):
        with self.resources_sync():
            try:
                return self._resources[res_id]
            except KeyError:
                resource_url = RSS_Resource_id2url(res_id)
                return self.get_resource(resource_url, res_db, False,
                                         follow_redirect)

    def evict_resource(self, resource):
        with self.resources_sync():
            try:
                del self._resources[resource.url()]
            except KeyError:
                pass
            try:
                del self._resources[resource.id()]
            except KeyError:
                pass

            try:
                del self._res_uids[resource.id()]
            except KeyError:
                pass


    # @precondition self.resources_sync()
    def get_resource_uids(self, resource, db_cursor=None):
        res_id = resource.id()

        try:
            res_uids = self._res_uids[res_id]
        except KeyError:
            res_uids = []

            with Cursor(ctx.db, db_cursor) as cursor:
                result = cursor.execute('SELECT uid FROM user_resource WHERE rid=?',
                                        (res_id,))
                for row in result:
                    res_uids.append(row[0])

            self._res_uids[res_id] = res_uids

        return res_uids


    # @throws KeyError
    def get_user(self, jid):
        key = jid.bare.lower()
        jid_resource = jid.resource
        if jid_resource == None:
            jid_resource = ''
        return self._users[key], jid_resource

    # @throws KeyError
    def get_user_by_id(self, uid):
        return self._users[uid]

    def load_user(self, jid, presence_show, create=False):
        key = jid.bare.lower()
        jid_resource = jid.resource
        if presence_show == None:
            jid_resource = None
        elif jid_resource == None:
            jid_resource = ''

        try:
            user = self._users[key]
            user.set_presence(jid_resource, presence_show)
            return user, jid_resource
        except KeyError:
            try:
                user = JabberUser(key, jid_resource, presence_show, create)
            except KeyError:
                return None, None

            with self.users_sync():
                self._users[key] = user
                self._users[user.uid()] = user

            for res_id in user._res_ids:
                try:
                    ctx.storage.get_resource_by_id(res_id, ctx.main_res_db)
                except:
                    log_message('caught exception loading resource', str(res_id), 'for new user')
                    traceback.print_exc(file=sys.stdout)

            return user, jid_resource

    def evict_user(self, user):
        with self.users_sync():
            try:
                del self._users[user.jid()]
            except KeyError:
                pass

            try:
                del self._users[user.uid()]
            except KeyError:
                pass

    def evict_all_users(self):
        with self.users_sync():
            self._users = {}


    def remove_user(self, user):
        with Cursor(ctx.db) as cursor:
            cursor.execute('DELETE FROM user WHERE uid=?',
                           (user.uid(),))

        log_message('user %s (id %d) deleted' % (user._jid, user._uid))
        self.evict_user(user)
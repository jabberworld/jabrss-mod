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


import bisect, sys, time, traceback

import jabrssng_context as ctx

from parserss import RSS_Resource_db

from jabrssng_context import log_message
from jabrssng_db import Cursor, FlexibleLocker, get_db


class UpdaterMixin:
    def schedule_update(self, resource):
        self._update_queue_cond.acquire()
        next_update = resource.next_update()
        log_message('scheduling', resource.url(), time.asctime(time.localtime(next_update)))

        bisect.insort(self._update_queue, (next_update, resource))
        if self._update_queue[0] == (next_update, resource):
            self._update_queue_cond.notify_all()

        self._update_queue_cond.release()

    def run(self):
        db, res_db = None, None

        try:
            # initial delay before the updater starts polling; wait() on
            # the queue condition makes it interruptible by terminate()
            self._update_queue_cond.acquire()
            self._update_queue_cond.wait(20)
            self._update_queue_cond.release()

            if not self._term_flag:
                log_message('starting RSS/RDF updater')
                db = get_db()
                res_db = RSS_Resource_db()
                ctx.storage._redirect_db = db

                self._update_queue_cond.acquire()
                while not self._term_flag:
                    if self._update_queue:
                        timeout = self._update_queue[0][0] - int(time.time())

                        if timeout > 3:
                            if timeout > 300:
                                log_message('updater waiting for %d seconds' % (timeout,))
                            self._update_queue_cond.wait(timeout)
                        else:
                            resource = self._update_queue[0][1]
                            del self._update_queue[0]

                            self._update_queue_cond.release()
                            self._update_resource(resource, db, res_db)
                            self._update_queue_cond.acquire()
                    else:
                        log_message('updater queue empty...')
                        self._update_queue_cond.wait()

                self._update_queue_cond.release()
        except:
            log_message('updater thread caught exception...')
            traceback.print_exc(file=sys.stdout)
            sys.exit(1)

        log_message('updater shutting down...')
        if db is not None:
            del db
        if hasattr(ctx.storage, '_redirect_db'):
            del ctx.storage._redirect_db
        if res_db is not None:
            del res_db

        self._term.set()


    def _update_resource(self, resource, db, res_db=None):
        redirect_url, redirect_seq = resource.redirect_info(res_db)
        if redirect_url != None:
            return

        redirects = []

        with FlexibleLocker(None) as redirlock:
            with FlexibleLocker(resource.sync()) as reslock:
                with Cursor(db) as cursor:
                    uids = ctx.storage.get_resource_uids(resource, cursor)
                    cursor.commit()

                    used = False
                    with ctx.storage.users_sync():
                        for uid in uids:
                            try:
                                user = ctx.storage.get_user_by_id(uid)
                                used = True
                            except KeyError:
                                pass

                        if not used:
                            ctx.storage.evict_resource(resource)

                    if used:
                        reslock.unlock()
                        redirect_resource = None

                        try:
                            log_message(time.asctime(), 'updating', resource.url())
                            new_items, next_item_id, redirect_resource, redirect_seq, redirects = resource.update(res_db, redirect_cb = ctx.storage._redirect_cb)

                            if len(new_items) > 0:
                                reslock.locked()
                            elif redirect_resource != None:
                                reslock.lock()

                            if redirect_resource != None:
                                redirlock.replace(redirect_resource.sync())

                            if len(new_items) > 0 or redirect_resource != None:
                                deliver_users = []
                                uids = ctx.storage.get_resource_uids(resource, cursor)
                                cursor.begin()
                                for uid in uids:
                                    try:
                                        user = ctx.storage.get_user_by_id(uid)

                                        if redirect_resource != None:
                                            try:
                                                user.add_resource(redirect_resource,
                                                                  redirect_seq,
                                                                  cursor)
                                            except ValueError:
                                                pass
                                            try:
                                                ctx.dummy_user.remove_resource(redirect_resource, cursor)
                                            except ValueError:
                                                pass


                                        if len(new_items) and user.get_delivery_state():
                                            if redirect_resource == None:
                                                user.update_headline(resource,
                                                                     next_item_id,
                                                                     new_items, cursor)
                                            else:
                                                try:
                                                    user.remove_resource(resource,
                                                                         cursor)
                                                except ValueError:
                                                    pass

                                            deliver_users.append(user)

                                        elif len(new_items) == 0:
                                            try:
                                                user.remove_resource(resource, cursor)
                                            except ValueError:
                                                pass

                                    except KeyError:
                                        # just means that the user is no longer online
                                        pass

                                # we need to unlock the resource here to
                                # prevent deadlock (the main thread, which is
                                # needed for sending, might be blocked waiting
                                # to acquire resource)
                                cursor.commit()
                                reslock.unlock()
                                redirlock.unlock()

                                for user in deliver_users:
                                    self._send_headlines(user, resource, new_items,
                                                         True)
                        except:
                            log_message('exception caught updating', resource.url())
                            traceback.print_exc(file=sys.stdout)

                        redirlock.unlock()
                        if redirect_resource == None:
                            self.schedule_update(resource)

        for resource, new_items, next_item_id in redirects:
            deliver_users = []

            # remember to always lock the resource first
            with resource.sync():
                with Cursor(db) as cursor:
                    cursor.begin()

                    log_message('processing updated resource', resource.url())
                    try:
                        ctx.dummy_user.remove_resource(resource, cursor)
                    except ValueError:
                        pass

                    uids = ctx.storage.get_resource_uids(resource, cursor)
                    for uid in uids:
                        try:
                            user = ctx.storage.get_user_by_id(uid)

                            if user.get_delivery_state():
                                headline_id = user.headline_id(resource, cursor)
                                if headline_id < next_item_id:
                                    user.update_headline(resource,
                                                         next_item_id,
                                                         new_items, cursor)

                                    deliver_users.append(user)
                        except KeyError:
                            # just means that the user is no longer online
                            pass

            for user in deliver_users:
                self._send_headlines(user, resource, new_items, True)
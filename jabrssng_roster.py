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


import sys, time, traceback

from slixmpp import JID

import jabrssng_context as ctx

from jabrssng_context import log_message
from jabrssng_db import Cursor
from jabrssng_user import get_week_nr


# grace period (in seconds) before a user that is no longer subscribed
# (according to the server-side roster) is actually deleted together with
# his subscriptions; until then the user is only marked as "pending
# removal" (see the pending_removal column of the user table)
REMOVAL_GRACE_SECS = 7 * 24 * 60 * 60


class RosterMixin:
    def _unsubscribe_user(self, jid, send_unsubscribed=True):
        self.send_presence(pto=jid, ptype='unsubscribe')

        if send_unsubscribed:
            self.send_presence(pto=jid, ptype='unsubscribed')

    def _remove_user(self, jid):
        self.del_roster_item(jid)


    # delete all user information from database and evict user
    def _delete_user(self, jid):
        user, jid_resource = ctx.storage.load_user(jid, None)
        if user == None:
            return

        log_message('deleting user\'s %s subscriptions: %s' % (str(jid), repr(user.resources())))
        for res_id in user.resources():
            try:
                resource = ctx.storage.get_resource_by_id(res_id, ctx.main_res_db)
                resource.lock()
                try:
                    try:
                        user.remove_resource(resource)
                    except ValueError:
                        pass
                finally:
                    resource.unlock()
            except KeyError:
                traceback.print_exc(file=sys.stdout)

        ctx.storage.remove_user(user)


    # remove a user (and all subscriptions) only after the missing
    # subscription has persisted for REMOVAL_GRACE_SECS; the first time
    # the subscription is found to be missing the user is only marked
    # (the pending_removal column holds the timestamp of the last check)
    def _delete_user_after_grace(self, jid):
        username = str(jid.bare).lower()
        now = int(time.time())

        with Cursor(ctx.db) as cursor:
            rows = cursor.execute(
                'SELECT pending_removal FROM user WHERE jid=?',
                (username,)).fetchall()
            if not rows:
                return
            pending_removal = rows[0][0]
            if pending_removal == None:
                cursor.execute(
                    'UPDATE user SET pending_removal=? WHERE jid=?',
                    (now, username))
                log_message('user "%s": removed from roster, marked for '
                            'removal, deleting in %d s if state persists' %
                            (username, REMOVAL_GRACE_SECS))
                return

        if now - pending_removal >= REMOVAL_GRACE_SECS:
            log_message('user "%s": removed from roster for more than %d s, '
                        'deleting user and subscriptions' %
                        (username, REMOVAL_GRACE_SECS))
            self._unsubscribe_user(jid)
            self._delete_user(jid)
            return

        log_message('user "%s": still removed from roster, deletion pending '
                    'for another %d s' %
                    (username, REMOVAL_GRACE_SECS - (now - pending_removal)))


    def roster_update_event(self, iq):
        items = iq['roster']['items']
        if iq['type'] == 'result':
            now = int(time.time())
            subscribers = {}
            non_subscribers = 0
            for user, item in items.items():
                user = str(user).lower()
                subscription = item.get('subscription', 'none')
                if subscription in ('both', 'from'):
                    subscribers[user] = True
                else:
                    non_subscribers += 1
                    log_message('subscription for user "%s" is "%s" (!= "both")' % (user, subscription))

            with Cursor(ctx.db) as cursor:
                total_users = cursor.execute('SELECT COUNT(*) FROM user').fetchone()[0]

            # an empty roster result while users are stored in the
            # database is not trustworthy: it would otherwise remove every
            # user and their subscriptions; skip the cleanup in that case
            # and only log the situation. Distinguish a roster versioning
            # "no changes" reply (has a ver attribute) from a genuinely
            # empty roster. A non-empty result is trusted -- unsubscribed
            # contacts merely flow through the grace-period handling below
            # (mark pending_removal, delete only after
            # REMOVAL_GRACE_SECS), so a wrong answer cannot delete users
            # immediately.
            if total_users > 0 and not items:
                ver = iq['roster']['ver']
                if ver:
                    log_message('roster unchanged (ver=%s), not reconciling %d user(s)' % (ver, total_users))
                else:
                    log_message('roster result is empty but %d user(s) are in the database; skipping cleanup' % (total_users,))
                return

            log_message('roster result: %d item(s), %d subscriber(s), %d non-subscriber(s)' % (len(items), len(subscribers), non_subscribers))

            # remove entire roster entries for contacts we are not
            # subscribed to
            for user, item in items.items():
                user = str(user).lower()
                subscription = item.get('subscription', 'none')
                if subscription not in ('both', 'from'):
                    jid = JID(user)
                    self._unsubscribe_user(jid)
                    self._remove_user(jid)

            stale_entries = []
            not_subscribed = []
            restored_users = []

            with Cursor(ctx.db) as cursor:
                rows = cursor.execute('SELECT uid, jid, pending_removal FROM user').fetchall()
                for uid, username, pending_removal in rows:
                    if username.find('/') != -1:
                        # tidying up an old user entry that contains the
                        # resource in the stored jid
                        stale_entries.append((username, uid))
                    elif not subscribers.get(username, False):
                        log_message('user "%s" in database, but not subscribed to the service' % (username,))
                        not_subscribed.append((username, pending_removal))
                    else:
                        if pending_removal != None:
                            cursor.execute('UPDATE user SET pending_removal=NULL WHERE uid=?', (uid,))
                            log_message('user "%s": subscription restored, removal cancelled' % (username,))
                        restored_users.append(username)
                        subscribers[username] = False

            for username, uid in stale_entries:
                log_message('removing stale user entry "%s" (id %d)' % (username, uid))
                with Cursor(ctx.db) as cursor:
                    cursor.execute('DELETE FROM user WHERE uid=?', (uid,))

            deleted_users = 0
            marked_users = 0
            pending_users = 0
            for username, pending_removal in not_subscribed:
                if pending_removal == None:
                    # first time we notice the missing subscription
                    with Cursor(ctx.db) as cursor:
                        cursor.execute('UPDATE user SET pending_removal=? WHERE jid=?', (now, username))
                    log_message('user "%s": marked for removal, deleting in %d s if state persists' % (username, REMOVAL_GRACE_SECS))
                    marked_users += 1
                elif now - pending_removal >= REMOVAL_GRACE_SECS:
                    log_message('user "%s": not subscribed for more than %d s, deleting user and subscriptions' % (username, REMOVAL_GRACE_SECS))
                    self._unsubscribe_user(JID(username))
                    self._delete_user(JID(username))
                    deleted_users += 1
                else:
                    log_message('user "%s": still not subscribed, deletion pending for another %d s' % (username, REMOVAL_GRACE_SECS - (now - pending_removal)))
                    pending_users += 1

            if deleted_users + marked_users + pending_users > 0:
                log_message('roster synchronization finished: %d user(s) deleted, %d user(s) marked for removal, %d user(s) still pending' % (deleted_users, marked_users, pending_users))

            subscribers = filter(lambda x: x[1] == True,
                                 subscribers.items())
            subscribers = map(lambda x: x[0], subscribers)
            week_nr = get_week_nr()

            with Cursor(ctx.db) as cursor:
                for username in subscribers:
                    try:
                        cursor.execute('INSERT INTO user (jid, conf, store_messages, size_limit, since) VALUES (?, ?, ?, ?, ?)',
                                       (username, 0, 16, None, week_nr))
                    except:
                        pass

            with Cursor(ctx.db) as cursor:
                result = cursor.execute('SELECT jid FROM user LEFT OUTER JOIN user_stat ON (user.uid=user_stat.uid) WHERE since < ? AND (start < ? OR start IS NULL)',
                                        (week_nr - 3, week_nr - 32))
                delete_users = []
                for row in result:
                    delete_users.append(row[0])

            for username in delete_users:
                log_message('user "%s" hasn\'t used the service for more than 40 weeks' % (username,))
                jid = JID(username)
                self._unsubscribe_user(jid)
                self._remove_user(jid)
        else:
            for user, item in items.items():
                user = str(user)
                subscription = item.get('subscription')
                log_message('roster updated', user, subscription)
                if subscription in ('remove', 'none'):
                    self._delete_user_after_grace(JID(user))
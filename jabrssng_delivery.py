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


import sys, traceback

from xml.etree.ElementTree import Element

from slixmpp import JID

import jabrssng_context as ctx

from jabrssng_context import log_message
from jabrssng_text import TEXT_WELCOME, TEXT_NEWUSER


MAX_MESSAGE_SIZE = 20000


class DeliveryMixin:
    def message(self, stanza):
        typ, sender, body = (stanza['type'], stanza['from'], stanza['body'])
        if typ == None or body == '':
            return

        body = body.strip()
        log_message('message', typ, str(sender), body)

        if sender.user == '':
            log_message('ignoring server message from', str(sender))
        elif typ in ('normal', 'chat'):
            try:
                user, jid_resource = ctx.storage.get_user(sender)
            except KeyError:
                # user is not in the cache (e.g. after a reconnect):
                # try to load from the database, but do not create a
                # new user if it does not exist there either
                user, jid_resource = ctx.storage.load_user(sender, None)
            if user is None:
                return
            try:
                unknown_msg = False

                if body == 'help' or body == '?':
                    return self._process_help(stanza, user)
                elif body == 'list':
                    return self._process_list(stanza, user)
                elif body[:4] == 'set ':
                    return self._process_set(stanza, user, body[4:])
                elif (body == 'configuration') or (body == 'conf'):
                    return self._process_config(stanza, user)
                elif (body == 'stats') or (body == 'statistics') or (body == 'show statistics'):
                    return self._process_statistics(stanza, user)
                elif (body == 'usage') or (body == 'show usage'):
                    return self._process_usage(stanza, user)
                elif body[:10] == 'subscribe ':
                    return self._process_subscribe(stanza, user, body[10:])
                elif body[:4] == 'add ':
                    return self._process_subscribe(stanza, user, body[4:])
                elif body[:2] == '+ ':
                    return self._process_subscribe(stanza, user, body[2:])
                elif body[:12] == 'unsubscribe ':
                    return self._process_unsubscribe(stanza, user, body[12:])
                elif body[:4] == 'del ':
                    return self._process_unsubscribe(stanza, user, body[4:])
                elif body[:2] == '- ':
                    return self._process_unsubscribe(stanza, user, body[2:])
                elif body[:5] == 'info ':
                    return self._process_info(stanza, user, body[5:])
                else:
                    unknown_msg = True
                    # safe-guard against robot ping-pong
                    if user._unknown_msgs < 2:
                        user._unknown_msgs = user._unknown_msgs + 1
                        self._reply(stanza, 'Unknown command. Please refer to the documentation at http://dev.cmeerw.org/jabrss/Documentation')

                if not unknown_msg:
                    user._unknown_msgs = 0
            except KeyError:
                traceback.print_exc(file=sys.stdout)
        elif typ == 'headline':
            # silently ignore headline messages
            return
        elif typ == 'error':
            log_message('ignoring error message from', str(sender))
        else:
            log_message('ignoring unknown message type from', str(sender))

    def presence(self, stanza):
        typ = stanza['type']
        if typ in ('available', 'chat', 'away', 'xa', 'dnd'):
            return self.presence_available(stanza)
        elif typ in ('unavailable', 'error'):
            return self.presence_unavailable(stanza)
        elif typ == 'subscribe':
            return self.presence_subscribe(stanza)
        elif typ == 'subscribed':
            return self.presence_subscribed(stanza)
        elif typ == 'unsubscribe':
            return self.presence_unsubscribe(stanza)
        elif typ == 'unsubscribed':
            return self.presence_unsubscribed(stanza)

    def presence_available(self, stanza):
        sender, typ, status, show = (stanza['from'], stanza['type'],
                                     stanza['status'], stanza['show'] or None)
        try:
            presence = [None, 'chat', 'away', 'xa', 'dnd'].index(show)
        except ValueError:
            return

        user, jid_resource = ctx.storage.load_user(sender, presence)
        if user == None:
            log_message('presence ignored', str(sender), str(show))
        elif not user.get_delivery_state(presence):
            log_message('presence', str(sender), str(show))
        else:
            log_message('presence', str(sender), str(show))
            subs = None

            for res_id in user.resources()[:]:
                resource = ctx.storage.get_resource_by_id(res_id, ctx.main_res_db)
                if subs != None:
                    subs.append(resource.url())

                try:
                    resource.lock()

                    while True:
                        headline_id = user.headline_id(resource)
                        old_id = headline_id

                        new_items, headline_id = resource.get_headlines(headline_id, db=ctx.main_res_db)
                        if new_items:
                            self._send_headlines(user, resource, new_items)

                        redirect_url, redirect_seq = resource.redirect_info(ctx.main_res_db)
                        if redirect_url != None:
                            log_message('processing redirect to', redirect_url)

                            try:
                                user.remove_resource(resource)
                            except ValueError:
                                pass
                            resource.unlock()

                            resource = ctx.storage.get_resource(redirect_url,
                                                            ctx.main_res_db,
                                                            True, False)
                            try:
                                user.add_resource(resource, redirect_seq)
                            except ValueError:
                                pass

                            continue
                        elif new_items or headline_id != old_id:
                            user.update_headline(resource, headline_id,
                                                 new_items)

                        break
                finally:
                    resource.unlock()

    def presence_unavailable(self, stanza):
        sender, typ, status, show = (stanza['from'], stanza['type'],
                                     stanza['status'], stanza['show'] or None)
        log_message('presence', str(sender), typ)
        try:
            user, jid_resource = ctx.storage.get_user(sender)
            user.set_presence(jid_resource, -1)
            if user.presence() < 0:
                log_message('evicting user', user.jid())
                ctx.storage.evict_user(user)
        except KeyError:
            pass

    def presence_subscribe(self, stanza):
        sender, typ = stanza['from'], stanza['type']
        log_message('presence_control', str(sender), typ)

        msg_text = TEXT_WELCOME
        try:
            ctx.storage.get_user(sender)
        except KeyError:
            msg_text += TEXT_NEWUSER
            try:
                ctx.storage.load_user(sender, None, True)
            except Exception:
                traceback.print_exc(file=sys.stdout)

        self.send_message(mto=sender, mtype='normal', mbody=msg_text)
        self.send_presence(pto=sender, ptype='subscribed')
        self.send_presence(pto=sender, ptype='subscribe')

    def presence_subscribed(self, stanza):
        sender, typ = stanza['from'], stanza['type']
        log_message('presence subscribed', str(sender), typ)
        ctx.storage.load_user(sender, None, True)

    def presence_unsubscribe(self, stanza):
        sender, typ = stanza['from'], stanza['type']
        log_message('presence unsubscribe', str(sender), typ)
        self._unsubscribe_user(JID(sender.bare))
        self._delete_user(JID(sender.bare))

    def presence_unsubscribed(self, stanza):
        sender, typ = stanza['from'], stanza['type']
        log_message('presence unsubscribed', str(sender), typ)
        self._unsubscribe_user(JID(sender.bare), False)
        self._remove_user(JID(sender.bare))
        self._delete_user(JID(sender.bare))


    def _format_header(self, title, url, res_url, format):
        if url == '':
            url = res_url

        if format == 1:
            return '%s' % (title,)
        elif format == 2:
            return '%s' % (url,)
        elif format == 3:
            if title != '':
                return '%s: %s' % (title, url)
            else:
                return url

        return ''

    def _send_headlines(self, user, resource, items, not_stored=False):
        log_message('sending', user.jid(), resource.url())
        message_type = user.get_message_type()
        subject_format = user.get_subject_format()
        header_format = user.get_header_format()

        channel_info = resource.channel_info()

        subject_text = self._format_header(channel_info.title, channel_info.link, resource.url(), subject_format)

        if message_type in (0, 2): # normal message or chat
            body = []
            msgs, l = [body], 0
            header_text = self._format_header(channel_info.title, channel_info.link, resource.url(), header_format)
            if header_text != '':
                body.append('[ %s ]\n' % (header_text,))
                l += len(body[-1])

            if not not_stored and (len(items) > user.get_store_messages()):
                body.append('%d headlines suppressed (from %s)\n' % (len(items) - user.get_store_messages(), channel_info.title))
                l += len(body[-1])
                items = items[-user.get_store_messages():]

            if body:
                body.append('\n')
                l += 1

            for item in items:
                try:
                    title, link, descr = (item.title, item.link, item.descr_plain)

                    if not descr or (descr == title):
                        body.append('%s\n%s\n' % (title, link))
                    else:
                        body.append('%s\n%s\n%s\n' % (title, link,
                                                      descr[:user.get_size_limit()]))
                    l += len(body[-1]) + 1
                    body.append('\n')

                    if l >= MAX_MESSAGE_SIZE:
                        l = len(body[-1]) + 1
                        body = []
                        msgs.append(body)
                except ValueError:
                    log_message('trying to unpack tuple of wrong size', repr(item))

            mt = ('normal', 'chat')[message_type != 0]
            for body in msgs:
                if body:
                    msg = self.make_message(mto=JID(user.jid()), mtype=mt,
                                            msubject=subject_text,
                                            mbody=''.join(body))
                    self._send_stanza(msg)

        elif message_type == 1:         # headline
            if not not_stored and (len(items) > user.get_store_messages()):
                msg = self.make_message(mto=JID(user.jid()), mtype='headline',
                                        msubject=subject_text,
                                        mbody='%d headlines suppressed' % (len(items) - user.get_store_messages(),))
                oob_ext = Element('{jabber:x:oob}x')
                oob_ext_url = Element('{jabber:x:oob}url')
                oob_ext_url.text = channel_info.link
                oob_ext.append(oob_ext_url)

                oob_ext_desc = Element('{jabber:x:oob}desc')
                oob_ext_desc.text = channel_info.descr
                oob_ext.append(oob_ext_desc)
                msg.append(oob_ext)
                self._send_stanza(msg)

                items = items[-user.get_store_messages():]

            for item in items:
                title, link = (item.title, item.link)

                if item.descr_plain:
                    description = item.descr_plain
                else:
                    description = title

                msg = self.make_message(mto=JID(user.jid()), mtype='headline',
                                        msubject=subject_text,
                                        mbody=description[:user.get_size_limit()])
                oob_ext = Element('{jabber:x:oob}x')
                oob_ext_url = Element('{jabber:x:oob}url')
                oob_ext_url.text = link
                oob_ext.append(oob_ext_url)

                oob_ext_desc = Element('{jabber:x:oob}desc')
                oob_ext_desc.text = title
                oob_ext.append(oob_ext_desc)
                msg.append(oob_ext)
                self._send_stanza(msg)
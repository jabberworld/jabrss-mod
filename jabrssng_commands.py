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

import jabrssng_context as ctx

from parserss import UrlError

from jabrssng_context import log_message
from jabrssng_db import Cursor
from jabrssng_text import TEXT_HELP


class ChatCommandMixin:
    def _process_help(self, stanza, user):
        self._reply(stanza, TEXT_HELP)

    def _process_list(self, stanza, user):
        reply_body = []
        for res_id in user.resources():
            resource = ctx.storage.get_resource_by_id(res_id, ctx.main_res_db)
            res_updated, res_modified, res_invalid = resource.times()
            if res_invalid == None:
                reply_body.append(resource.url())
            else:
                error_info = resource.error_info()
                if error_info:
                    reply_body.append('%s (Error: %s)' % (resource.url(),
                                                          error_info))
                else:
                    reply_body.append('%s (error)' % (resource.url(),))

        if reply_body:
            reply_body.sort()
            reply_body = '\n'.join(reply_body)
        else:
            reply_body = 'Sorry, you are currently not subscribed to any RSS feeds.'

        self._reply(stanza, reply_body)

    def _parse_format(self, args):
        format = 0
        for arg in args:
            if arg.lower() == 'title':
                format |= 1
            elif arg.lower() == 'url' or arg.lower() == 'link':
                format |= 2
            elif arg.lower() == '<empty>':
                format = 0
                break
            else:
                raise Exception('invalid format')
        return format

    def _process_set(self, stanza, user, argstr):
        try:
            arg = argstr.strip()
            if arg == 'plaintext':
                user.set_message_type(0)
                reply_body = 'Message type set to "plaintext"'
            elif arg == 'headline':
                user.set_message_type(1)
                reply_body = 'Message type set to "headline"'
            elif arg == 'chat':
                user.set_message_type(2)
                reply_body = 'Message type set to "chat"'
            else:
                args = arg.split()
                if args[0] == 'also_deliver':
                    deliver_cfg = 0

                    for s in args[1:]:
                        s = s.lower()
                        if s == 'away':
                            deliver_cfg = deliver_cfg | 1
                        elif s == 'xa':
                            deliver_cfg = deliver_cfg | 2
                        elif s == 'dnd':
                            deliver_cfg = deliver_cfg | 4
                        elif s == 'none':
                            pass
                        else:
                            raise Exception('unknown setting for "also_deliver"')

                    user.set_delivery_state(deliver_cfg)
                    reply_body = '"also_deliver" setting adjusted'
                elif args[0] == 'store_messages':
                    store_messages = int(args[1])
                    user.set_store_messages(store_messages)
                    reply_body = '"store_messages" setting adjusted'
                elif args[0] == 'size_limit':
                    size_limit = int(args[1])
                    user.set_size_limit(size_limit)
                    reply_body = '"size_limit" setting adjusted'
                elif args[0] == 'header':
                    format = self._parse_format(args[1:])
                    user.set_header_format(format)
                    reply_body = 'header format adjusted'
                elif args[0] == 'subject':
                    format = self._parse_format(args[1:])
                    user.set_subject_format(format)
                    reply_body = 'subject format adjusted'
                else:
                    reply_body = 'Unknown configuration option'
        except:
            reply_body = 'Unknown error setting configuration option'

        self._reply(stanza, reply_body)


    def _format_format_conf(self, format):
        format_text = []
        if format & 1:
            format_text.append('title')
        if format & 2:
            format_text.append('url')
        if format_text == []:
            format_text.append('<empty>')

        return ', '.join(format_text)

    def _process_config(self, stanza, user):
        reply_body = ['Current configuration:']

        message_type = user.get_message_type()
        if message_type == 0:
            reply_body.append('message type "plaintext"')
        elif message_type == 1:
            reply_body.append('message type "headline"')
        elif message_type == 2:
            reply_body.append('message type "chat"')
        else:
            reply_body.append('message type <reserved>')

        deliver_when_away = user.get_deliver_when_away()
        deliver_when_xa = user.get_deliver_when_xa()
        deliver_when_dnd = user.get_deliver_when_dnd()
        if deliver_when_away or deliver_when_xa or deliver_when_dnd:
            deliver_list = []
            if deliver_when_away:
                deliver_list.append('Away')
            if deliver_when_xa:
                deliver_list.append('XA')
            if deliver_when_dnd:
                deliver_list.append('DND')
            reply_body.append('Headlines will also be delivered when you are %s' % (', '.join(deliver_list)))

        subject_format = user.get_subject_format()
        reply_body.append('subject format: %s' % (self._format_format_conf(subject_format),))

        header_format = user.get_header_format()
        reply_body.append('header format: %s' % (self._format_format_conf(header_format),))

        store_messages = user.get_store_messages()
        reply_body.append('At most %d headlines will be stored for later delivery' % (store_messages,))

        size_limit = user.get_size_limit()
        if size_limit:
            reply_body.append('The size of a headline message will be limited to about %d bytes' % (size_limit,))

        self._reply(stanza, '\n'.join(reply_body))


    def _process_statistics(self, stanza, user):
        reply_body = ['Statistics:']
        total_users, total_resources = 0, 0
        with Cursor(ctx.db) as cursor:
            result = cursor.execute('SELECT (SELECT COUNT(uid) FROM user), (SELECT COUNT(DISTINCT rid) FROM user_resource)')
            for total_users, total_resources in result:
                pass

        reply_body.append('Users online/total: %d/%d' %
                          (len(ctx.storage._users) // 2, total_users))
        reply_body.append('RDF feeds used/total: %d/%d' %
                          (len(ctx.storage._resources) // 2, total_resources))

        self._reply(stanza, '\n'.join(reply_body))


    def _process_usage(self, stanza, user):
        reply_body = ['Usage Statistics:']

        reply_body.append('subscribed to %d feeds' % (len(user.resources())))

        stat_start, nr_headlines, size_headlines = user.get_statistics()
        stat_start = stat_start - (len(nr_headlines) - 1)

        time_base = stat_start * 7*24*60*60 - 60*60*60

        for i in range(0, len(nr_headlines)):
            nr = nr_headlines[i]
            size = size_headlines[i]
            if nr > 0:
                month1, day1 = time.gmtime(time_base)[1:3]
                month2, day2 = time.gmtime(time_base + 6*24*60*60)[1:3]
                if size > 11*1024:
                    size_str = '%d kiB' % (size // 1024,)
                else:
                    size_str = '%d Bytes' % (size,)
                reply_body.append('%d/%d - %d/%d: %d headlines (%s)' % (day1, month1, day2, month2, nr, size_str))

            time_base += 7*24*60*60

        self._reply(stanza, '\n'.join(reply_body))

    def _process_subscribe(self, stanza, user, argstr):
        args = argstr.split()
        reply_body = None

        for url in args:
            try:
                resource = ctx.storage.get_resource(url, ctx.main_res_db)
                try:
                    url = resource.url()
                    user.add_resource(resource)

                    new_items, headline_id = resource.get_headlines(0, db=ctx.main_res_db)
                    if new_items:
                        self._send_headlines(user, resource, new_items)
                        user.update_headline(resource, headline_id, new_items)
                finally:
                    resource.unlock()

                log_message(user.jid(), 'subscribed to', url)
                reply_body = 'You have been subscribed to %s' % (url,)
            except UrlError as url_error:
                log_message(user.jid(), 'error (%s) subscribing to %s' % (url_error.args[0], url))
                reply_body = 'Error (%s) subscribing to %s' % (url_error.args[0], url)
            except ValueError:
                log_message(user.jid(), 'already subscribed to', url)
                reply_body = 'You are already subscribed to %s' % (url,)
            except:
                log_message(user.jid(), 'error subscribing to', url)
                traceback.print_exc(file=sys.stdout)
                reply_body = 'For some reason you couldn\'t be subscribed to %s' % (url,)

            if reply_body:
                self._reply(stanza, reply_body)

    def _process_unsubscribe(self, stanza, user, argstr):
        args = argstr.split()
        reply_body = None

        for url in args:
            try:
                resource = ctx.storage.get_cached_resource(url)
                resource.lock()
                try:
                    user.remove_resource(resource)
                finally:
                    resource.unlock()

                log_message(user.jid(), 'unsubscribed from', url)
                reply_body = 'You have been unsubscribed from %s' % (url,)
            except UrlError as url_error:
                reply_body = 'Invalid URL %s (%s)' % (url, url_error.args[0])
            except KeyError:
                reply_body = 'For some reason you couldn\'t be unsubscribed from %s' % (url,)
            except ValueError:
                reply_body = 'No need to unsubscribe, you weren\'t subscribed to %s anyway' % (url,)
            except:
                log_message(user.jid(), 'error unsubscribing from', url)
                traceback.print_exc(file=sys.stdout)
                reply_body = 'For some reason you couldn\'t be unsubscribed from %s' % (url,)

            if reply_body:
                self._reply(stanza, reply_body)

    def _process_info(self, stanza, user, argstr):
        args = argstr.split()
        reply_body = None

        for url in args:
            try:
                resource = ctx.storage.get_cached_resource(url)

                last_updated, last_modified, invalid_since = resource.times()
                next_update = resource.next_update(0)
                penalty = resource.penalty()
                history = resource.history()

                text = ['Information about %s' % (url,)]
                text.append('')
                text.append('Last polled: %s GMT' % (time.asctime(time.gmtime(last_updated)),))

                if len(history):
                    text.append('Last updated: %s GMT' % (time.asctime(time.gmtime(history[-1][0])),))
                text.append('Next poll: ca. %s GMT' % (time.asctime(time.gmtime(next_update)),))
                text.append('Update interval: ~%d min' % ((next_update - last_updated) // 60,))
                text.append('Feed penalty: %d (out of 1024)' % (penalty,))

                if invalid_since:
                    error_info = resource.error_info()
                    if error_info:
                        text.append('')
                        text.append('Error: %s' % (error_info,))

                if len(history) >= 4:
                    sum_items = 0
                    for h in history[1:-1]:
                        sum_items += h[1]
                    time_span = history[-1][0] - history[0][0]

                    msg_rate = sum_items / (time_span / 2592000.0)

                    if msg_rate > 150.0:
                        rate_unit = 'day'
                        msg_rate = int(msg_rate / 30.0)
                    elif msg_rate > 22.0:
                        rate_unit = 'week'
                        msg_rate = int(msg_rate / (30.0/7.0))
                    else:
                        rate_unit = 'month'
                        msg_rate = int(msg_rate)

                    text.append('')
                    text.append('Frequency: ~%d headlines per %s' % (msg_rate, rate_unit))

                reply_body = '\n'.join(text)
            except UrlError as url_error:
                reply_body = 'Invalid URL %s (%s)' % (url, url_error.args[0])
            except KeyError:
                reply_body = 'No information available about %s' % (url,)
            except:
                log_message(user.jid(), 'no information for', url)
                traceback.print_exc(file=sys.stdout)
                reply_body = 'No information available about %s' % (url,)

            if reply_body:
                self._reply(stanza, reply_body)
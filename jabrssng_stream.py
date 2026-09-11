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


import sys, threading, time

from xml.etree.ElementTree import Element

from slixmpp import ClientXMPP
from slixmpp import version as slixmpp_version
from slixmpp.util import sasl as slixmpp_sasl
from slixmpp.exceptions import IqError, IqTimeout
from slixmpp.xmlstream import resolver as slixmpp_resolver
from slixmpp.xmlstream.handler import Callback
from slixmpp.xmlstream.matcher import StanzaPath

from parserss import RSS_Resource

import jabrssng_context as ctx

from jabrssng_context import log_message
from jabrssng_commands import ChatCommandMixin
from jabrssng_delivery import DeliveryMixin
from jabrssng_roster import RosterMixin
from jabrssng_updater import UpdaterMixin
from jabrssng_db import Cursor


# number of full authentication attempts before giving up (a server may
# temporarily reject logins, e.g. after too many failed attempts)
AUTH_RETRY_LIMIT = 3


class JabRSSStream(UpdaterMixin, RosterMixin, DeliveryMixin, ChatCommandMixin,
                   ClientXMPP):
    def __init__(self, jid, host, password, port=5222):
        self._jid, self._host, self._port = jid, host, port
        self._update_queue = []
        self._update_queue_cond = threading.Condition()
        RSS_Resource.schedule_update = self.schedule_update

        self._online = False
        self._term, self._term_flag = threading.Event(), False
        self._last_disconnect = 0
        self._auth_retries = 0
        self._quiet_reconnects = 0
        self._main_thread = threading.current_thread()

        ClientXMPP.__init__(self, str(self._jid), password=password)

        # do not attempt SCRAM-SHA-*-PLUS authentication: slixmpp's
        # channel binding is incompatible with strict servers (e.g.
        # ejabberd 24.02+) which reject it with "Invalid channel binding";
        # plain SCRAM authenticates fine on the first attempt. Note that
        # use_mechs must be a list/tuple - a set would be ignored.
        self.plugin['feature_mechanisms'].use_mechs = [
            mech for mech in slixmpp_sasl.MECHANISMS
            if not mech.endswith('-PLUS')]

        # plugins
        self.register_plugin('xep_0030')      # service discovery
        self.register_plugin('xep_0092', pconfig={'name': 'JabRSS-mod'})
        self.plugin['xep_0092'].version = '1.0'
        self.plugin['xep_0092'].os = ('Python %d.%d.%d + slixmpp %s' %
                                      (sys.version_info[:3] + (slixmpp_version.__version__,)))
        self.register_plugin('xep_0199')      # ping

        # only activate our own presence subscription handling
        self.auto_authorize = None
        self.auto_subscribe = False

        for event, handler in (('session_start', self.session_start),
                               ('message', self.message),
                               ('presence', self.presence),
                               ('roster_update', self.roster_update_event),
                               ('session_bind', self._on_session_bind),
                               ('auth_success', self._on_auth_success),
                               ('connected', self._on_connected),
                               ('disconnected', self._on_disconnected),
                               ('failed_auth', self._on_failed_auth),
                               ('failed_all_auth', self._on_auth_failed_all),
                               ('no_auth', self._on_auth_failed_all)):
            self.add_event_handler(event, handler)

        # everything not handled by a more specific handler
        self.register_handler(
            Callback('JabRSS iq get', StanzaPath('iq@type=get'),
                     self.iq_get_fallback))
        self.register_handler(
            Callback('JabRSS iq set', StanzaPath('iq@type=set'),
                     self.iq_set_fallback))

        # when no connect host is configured, resolve it via SRV records
        # of the JID domain: prefer direct TLS (_xmpps-client) over
        # STARTTLS (_xmpp-client)
        self.tls_services = {'xmpps-client'}
        self.starttls_services = {'xmpp-client'}


    async def get_dns_records(self, domain, port=None):
        # resolve the connection endpoints for <domain>: first query the
        # _xmpps-client SRV records (direct TLS), then - only if there are
        # none - the _xmpp-client ones (STARTTLS); fall back to plain
        # A/AAAA lookups of the domain if neither SRV record exists
        if port is None:
            port = self._port

        resolver = slixmpp_resolver.default_resolver(self.loop)
        self.configure_dns(resolver, domain=domain, port=port)

        records = []
        for service, srv_port in (('xmpps-client', 5223),
                                  ('xmpp-client', port)):
            hosts = await slixmpp_resolver.get_SRV(
                domain, srv_port, [service], resolver=resolver,
                use_aiodns=self.use_aiodns)
            for srv_service, srv_host, srv_srv_port in hosts:
                # an empty service name means get_SRV could not perform the
                # lookup (e.g. no aiodns) and just returned the fallback
                if srv_service != service:
                    continue
                if srv_host == '' or srv_host == '.':
                    continue
                addrs = []
                if self.use_ipv6:
                    addrs += await slixmpp_resolver.get_AAAA(
                        srv_host, resolver=resolver,
                        use_aiodns=self.use_aiodns, loop=self.loop)
                addrs += await slixmpp_resolver.get_A(
                    srv_host, resolver=resolver,
                    use_aiodns=self.use_aiodns, loop=self.loop)
                for address in addrs:
                    records.append((service, srv_host, address, srv_srv_port))
                log_message('SRV record %s._tcp.%s -> %s:%d' %
                            (service, domain, srv_host, srv_srv_port))

        if records:
            return records

        log_message('no SRV records for %s; falling back to direct '
                    'resolution of %s:%d' % (domain, domain, port))
        return await slixmpp_resolver.resolve(
            domain, port, resolver=resolver, use_ipv6=self.use_ipv6,
            use_aiodns=self.use_aiodns, loop=self.loop)


    def _on_session_bind(self, event):
        # register the disco identity/features for our bound resource
        # (boundjid is only fully known now), mirroring other plugins
        self.plugin['xep_0030'].add_identity('client', 'bot')
        for feature in ('jabber:iq:last', 'jabber:iq:time',
                        'urn:xmpp:time'):
            self.plugin['xep_0030'].add_feature(feature)


    def _on_connected(self, event):
        # the 'connected' event fires before slixmpp stores the transport,
        # so grab the real peer (host:port) from the next loop iteration
        def log_peer():
            peer = None
            try:
                peer = self.transport.get_extra_info('peername')
            except (AttributeError, OSError):
                pass
            if peer:
                log_message('connected to %s:%d' % (peer[0], peer[1]))
            else:
                log_message('connected to', self.boundjid.host)

        self.loop.call_soon(log_peer)
        # a TCP connection alone does not authenticate us; if the server
        # keeps dropping us before any SASL exchange (e.g. a temporary
        # block after too many failed logins), bound these idle cycles
        if not self.authenticated:
            self._quiet_reconnects += 1
            if self._quiet_reconnects > AUTH_RETRY_LIMIT:
                self._auth_giveup('server not accepting logins after %d connection attempts (temporary block?)' %
                                  (self._quiet_reconnects,))

    def _on_auth_success(self, event):
        self._auth_retries = 0
        self._quiet_reconnects = 0

    def _on_failed_auth(self, event):
        # A single SASL mechanism has been rejected by the server. This
        # is not necessarily fatal: slixmpp retries the remaining offered
        # mechanisms (e.g. SCRAM-SHA-1 -> PLAIN), and some servers reject
        # a given mechanism even when the supplied credentials are valid.
        self._quiet_reconnects = 0

        text = None
        try:
            text = event.get('text', '')
        except Exception:
            pass
        if text:
            log_message('authentication failed (%s): %s' % (event['condition'], text))
        else:
            log_message('authentication failed (%s)' % (event['condition'],))

        mechs = self.plugin.get('feature_mechanisms', None)
        if mechs is not None and mechs.attempted_mechs:
            log_message('rejected SASL mechanism(s):', ', '.join(sorted(mechs.attempted_mechs)))

    def _on_auth_failed_all(self, event):
        # All offered SASL mechanisms have been exhausted (or none was
        # usable). The credentials may be wrong, or the server may
        # temporarily reject logins (e.g. after too many failed auth
        # attempts). Retry a limited number of times before giving up;
        # _on_disconnected already schedules the next reconnect attempt.
        self._quiet_reconnects = 0
        self._auth_retries += 1
        if self._auth_retries < AUTH_RETRY_LIMIT:
            log_message('authentication failed, retrying (%d/%d) in ~60 seconds...' %
                        (self._auth_retries, AUTH_RETRY_LIMIT))
            return

        self._auth_giveup('after %d authentication attempts' % (self._auth_retries,))

    def _auth_giveup(self, reason):
        log_message('authentication failed, giving up: %s' % (reason,))
        self._term_flag = True
        self._update_queue_cond.acquire()
        self._update_queue_cond.notify_all()
        self._update_queue_cond.release()
        self.disconnect()


    def _on_disconnected(self, event):
        ctx.storage.evict_all_users()
        self._online = False
        log_message('stream closed')

        if self.terminated():
            self.loop.call_soon(self.loop.stop)
            return

        delay = 15
        if time.time() - self._last_disconnect < 30:
            delay += 45
        self._last_disconnect = time.time()
        log_message('waiting for next connection attempt in %s seconds' % (delay,))
        self.loop.call_later(delay, self._reconnect_once)


    def _reconnect_once(self):
        if self.terminated() or self.transport is not None:
            return
        if self._host is not None:
            self.connect(host=self._host, port=self._port)
        else:
            self.connect()


    def _ping_keepalive(self):
        if not self._online:
            return
        future = self.loop.create_task(
            self.plugin['xep_0199'].ping(self.boundjid.host, timeout=30))
        future.add_done_callback(self._ping_done)


    def _ping_done(self, future):
        try:
            future.result()
        except IqTimeout:
            log_message('ping timeout')
            if self.transport is not None:
                self.transport.close()
        except IqError as e:
            log_message('ping error:', str(e))
        except Exception as e:
            log_message('ping failed:', str(e))


    def _reply(self, stanza, body):
        reply = self.make_message(mto=stanza['from'], mtype=stanza['type'],
                                  msubject=stanza['subject'], mbody=body)
        self._send_stanza(reply)


    def _send_stanza(self, stanza):
        try:
            if threading.current_thread() is self._main_thread:
                stanza.send()
            else:
                self.loop.call_soon_threadsafe(stanza.send)
        except Exception as e:
            log_message('failed to send stanza:', str(e))


    def terminate(self, timeout=None):
        self._term_flag = True

        with self._update_queue_cond:
            self._update_queue_cond.notify_all()
        self._term.wait(timeout)

    def terminated(self):
        return self._term_flag


    def iq_get_fallback(self, iq):
        logmsg = ['iq get']
        iq_id, iq_from = iq['id'], iq['from']
        if iq_id:
            logmsg.append(iq_id)
        if iq_from:
            logmsg.append(str(iq_from))
        log_message(' '.join(logmsg))

        namespaces = self._stanza_namespaces(iq)
        for ns in namespaces:
            if ns in ('http://jabber.org/protocol/disco#info',
                      'http://jabber.org/protocol/disco#items',
                      'jabber:iq:version',
                      'urn:xmpp:ping'):
                return  # handled by a plugin

        if 'jabber:iq:time' in namespaces:
            return self._reply_legacy_time(iq)

        if 'jabber:iq:last' in namespaces:
            return self._reply_last(iq)

        if 'urn:xmpp:time' in namespaces:
            return self._reply_urn_time(iq)

        return self._reply_service_unavailable(iq)


    def iq_set_fallback(self, iq):
        logmsg = ['iq set']
        iq_id, iq_from = iq['id'], iq['from']
        if iq_id:
            logmsg.append(iq_id)
        if iq_from:
            logmsg.append(str(iq_from))
        log_message(' '.join(logmsg))

        namespaces = self._stanza_namespaces(iq)
        if 'jabber:iq:roster' in namespaces:
            return  # handled by ClientXMPP, which sends the result itself

        return self._reply_service_unavailable(iq)


    def _stanza_namespaces(self, stanza):
        namespaces = []
        for child in stanza.xml:
            tag = child.tag
            if tag.startswith('{'):
                namespaces.append(tag[1:].split('}')[0])
        return namespaces


    def _reply_service_unavailable(self, iq):
        if iq['id'] == '':
            return
        reply = iq.reply()
        reply['type'] = 'error'
        reply['error']['type'] = 'cancel'
        reply['error']['condition'] = 'service-unavailable'
        reply.send()


    def _reply_legacy_time(self, iq):
        if iq['id'] == '':
            return
        reply = iq.reply()
        time_elem = Element('{jabber:iq:time}query')
        utc = Element('{jabber:iq:time}utc')
        utc.text = time.strftime('%Y%m%dT%H:%M:%S', time.gmtime())
        time_elem.append(utc)
        reply.append(time_elem)
        reply.send()


    def _reply_urn_time(self, iq):
        if iq['id'] == '':
            return
        reply = iq.reply()
        time_elem = Element('{urn:xmpp:time}time')
        tzo = Element('{urn:xmpp:time}tzo')
        tzo.text = '-00:00'
        time_elem.append(tzo)
        utc = Element('{urn:xmpp:time}utc')
        utc.text = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        time_elem.append(utc)
        reply.append(time_elem)
        reply.send()


    def _reply_last(self, iq):
        if iq['id'] == '':
            return
        reply = iq.reply()
        last_elem = Element('{jabber:iq:last}query')
        last_elem.set('seconds', '0')
        reply.append(last_elem)
        reply.send()


    async def session_start(self, event):
        log_message('session start')

        try:
            # bypass roster versioning and fetch the full roster on every
            # (re)connect: slixmpp persists client_roster.version across
            # reconnects, and the server would otherwise answer with an
            # incremental (empty) "no changes" result -- so the roster
            # reconciliation in roster_update_event would never actually
            # run after the first connect in the process lifetime
            self.client_roster.version = None
            await self.get_roster()
            log_message('roster retrieved')
        except Exception as e:
            log_message('failed to retrieve roster:', str(e))

        log_message('sending presence')
        self.send_presence()
        self._online = True
        self.update_presence()

        for name in ('Ping keepalive', 'Presence keepalive'):
            try:
                self.cancel_schedule(name)
            except KeyError:
                pass
        self.schedule('Ping keepalive', 60, self._ping_keepalive, repeat=True)
        self.schedule('Presence keepalive', 900, self.update_presence,
                      repeat=True)


    def update_presence(self):
        if self._online:
            total_users, total_resources = 0, 0
            with Cursor(ctx.db) as cursor:
                result = cursor.execute('SELECT (SELECT COUNT(uid) FROM user), (SELECT COUNT(DISTINCT rid) FROM user_resource)')
                for total_users, total_resources in result:
                    pass

            status = '%d/%d users, %d/%d feeds' % (
                len(ctx.storage._users) // 2,
                total_users,
                len(ctx.storage._resources) // 2,
                total_resources)
            self.send_presence(pstatus=status)
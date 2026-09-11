import argparse, asyncio, time

from slixmpp import ClientXMPP, JID
from slixmpp.exceptions import IqError, IqTimeout
from xml.etree.ElementTree import Element, tostring


class Probe(ClientXMPP):
    def __init__(self, jid, password, bot_arg):
        ClientXMPP.__init__(self, jid, password)
        self.bot_arg = str(JID(bot_arg))
        self.bot_bare = str(JID(bot_arg).bare)
        self.bot_full = None
        # auto-approve the subscription request the bot sends back to us
        self.auto_authorize = True

        self.results = []
        self.message_replies = []

        self.register_plugin('xep_0030')
        self.add_event_handler('session_start', self.start)
        self.add_event_handler('message', self.on_message)
        self.add_event_handler('presence', self.on_presence)
        self.add_event_handler('disconnected', self.on_disconnected)

    def on_disconnected(self, event):
        self.loop.stop()

    def on_message(self, msg):
        self.message_replies.append((str(msg['from']), msg['type'], msg['body']))

    def on_presence(self, pres):
        frm = pres['from']
        # trust only presences that actually name a session resource
        if (str(frm.bare) == self.bot_bare and frm.resource
                and self.bot_full is None):
            self.bot_full = frm.full
            print('discovered bot resource: %s (type=%s)' % (frm.full, pres['type']))

    def show(self, name, ok, detail):
        self.results.append((name, ok, detail))

    async def _q(self, name, build):
        iq = self.make_iq_get(ito=self.bot_full)
        build(iq)
        try:
            r = await iq.send(timeout=20)
            self.show(name, True, tostring(r.xml, encoding='unicode'))
        except IqError as e:
            self.show(name, False, tostring(e.iq.xml, encoding='unicode'))
        except IqTimeout:
            self.show(name, False, 'TIMEOUT')

    async def start(self, event):
        await self.get_roster()
        self.send_presence()

        # establish a mutual presence subscription: some servers (Prosody)
        # only deliver incoming messages to their clients once subscribed
        self.send_presence_subscription(pto=self.bot_bare, ptype='subscribe')

        if JID(self.bot_arg).resource:
            # full JID provided: use it directly
            self.bot_full = self.bot_arg
            print('using bot resource: %s' % self.bot_full)
        else:
            deadline = time.time() + 30
            while self.bot_full is None and time.time() < deadline:
                await asyncio.sleep(0.5)
            if self.bot_full is None:
                print('WARN: bot resource not discovered, falling back to bare JID')
                self.bot_full = self.bot_bare

        await self._q('disco#info', lambda iq: iq.appendxml(
            Element('{http://jabber.org/protocol/disco#info}query')))
        await self._q('version', lambda iq: iq.appendxml(
            Element('{jabber:iq:version}query')))
        await self._q('last', lambda iq: iq.appendxml(
            Element('{jabber:iq:last}query')))
        await self._q('ping', lambda iq: iq.appendxml(
            Element('{urn:xmpp:ping}ping')))
        await self._q('urn:xmpp:time', lambda iq: iq.appendxml(
            Element('{urn:xmpp:time}time')))
        await self._q('legacy jabber:iq:time', lambda iq: iq.appendxml(
            Element('{jabber:iq:time}query')))
        await self._q('unknown ns', lambda iq: iq.appendxml(
            Element('{urn:example:unknown}thing')))

        for cmd in ('help', 'list', 'configuration'):
            self.send_message(mto=self.bot_full, mbody=cmd)
            await asyncio.sleep(2)

        print('== protocol results ==')
        for name, ok, detail in self.results:
            print('[%s] %s' % ('OK' if ok else 'ERR', name))
            print(detail)
        print('== message replies ==')
        for frm, mtype, body in self.message_replies:
            print('FROM', frm, 'TYPE', mtype)
            print((body or '')[:120])

        if self.bot_full is not None:
            self.send_presence_subscription(pto=self.bot_bare, ptype='unsubscribe')
        self.disconnect()
        await asyncio.sleep(1)
        self.loop.stop()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='jabber.name')
    ap.add_argument('--jid', default='rss-test@jabber.name/probe0')
    ap.add_argument('--bot', default='rss-test@linuxoid.in')
    ap.add_argument('--password-file', required=True)
    args = ap.parse_args()

    pw = open(args.password_file).read().strip()
    p = Probe(args.jid, pw, args.bot)
    p.connect(host=args.host, port=5222)
    asyncio.get_event_loop().run_forever()
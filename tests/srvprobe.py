import asyncio, time

from slixmpp import ClientXMPP, JID
from slixmpp.exceptions import IqError, IqTimeout
from xml.etree.ElementTree import Element, tostring

BOT_BARE = 'rss-test@linuxoid.in'
MY_RES = 'probe0'
PW = open('/tmp/rss-test.pw').read().strip()


class SProbe(ClientXMPP):
    def __init__(self):
        ClientXMPP.__init__(self, 'rss-test@linuxoid.in/%s' % MY_RES, PW)
        self.bot_full = None
        self.results = []
        self.message_replies = []
        self.register_plugin('xep_0030')
        self.add_event_handler('session_start', self.start)
        self.add_event_handler('presence', self.on_presence)
        self.add_event_handler('message', self.on_message)

    def on_message(self, m):
        self.message_replies.append((str(m['from']), m['type'], m['body']))

    def on_presence(self, p):
        frm = p['from']
        # same-account presence from one of the bot's resources
        if (str(frm.bare) == BOT_BARE and frm.resource
                and frm.resource != MY_RES and self.bot_full is None):
            self.bot_full = frm.full
            print('discovered bot resource: %s' % frm.full, flush=True)

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

        deadline = time.time() + 30
        while self.bot_full is None and time.time() < deadline:
            await asyncio.sleep(0.5)
        if self.bot_full is None:
            print('WARN: bot resource not discovered', flush=True)
            self.loop.stop()
            return

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

        print('== protocol results ==', flush=True)
        for name, ok, detail in self.results:
            print('[%s] %s' % ('OK' if ok else 'ERR', name), flush=True)
            print(detail, flush=True)
        print('== message replies ==', flush=True)
        for frm, mtype, body in self.message_replies:
            print('FROM', frm, 'TYPE', mtype)
            print((body or '')[:120], flush=True)
        self.loop.stop()


sp = SProbe()
sp.connect(host='linuxoid.in', port=5222)
asyncio.get_event_loop().run_forever()
print('done')
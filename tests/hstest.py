import asyncio, logging, sys, time

logging.basicConfig(level=logging.DEBUG, format='%(name)s: %(message)s')
# mute the noisiest
for lg in ('slixmpp.xmlstream.xmlstream',):
    logging.getLogger(lg).setLevel(logging.INFO)

sys.path.insert(0, '.')
from slixmpp import ClientXMPP, JID
from slixmpp import version as slxver
print('slixmpp', slxver.__version__)

BOT_BARE = 'rss-test@linuxoid.in'
PW = open('/tmp/rss-test.pw').read().strip()


class Hs(ClientXMPP):
    def __init__(self):
        ClientXMPP.__init__(self, 'rss-test@jabber.name/probe0', PW)
        self.bot_full = None
        self.auto_authorize = True
        self.add_event_handler('session_start', self.start)
        self.add_event_handler('presence', self.on_presence)
        self.add_event_handler('message', self.on_msg)

    def on_msg(self, m):
        print('MSG from', m['from'], 'body=', (m['body'] or '')[:60], flush=True)

    def on_presence(self, p):
        typ = p['type']
        print('PRES type=%s from=%s' % (typ, p['from']), flush=True)
        if str(p['from'].bare) == BOT_BARE and self.bot_full is None:
            self.bot_full = p['from'].full

    async def start(self, ev):
        await self.get_roster()
        self.send_presence()
        print('sending subscribe to', BOT_BARE, flush=True)
        self.send_presence_subscription(pto=BOT_BARE, ptype='subscribe')
        end = time.time() + 25
        while self.bot_full is None and time.time() < end:
            await asyncio.sleep(0.5)
        print('bot_full =', self.bot_full, flush=True)
        await asyncio.sleep(2)
        self.loop.stop()


hs = Hs()
hs.connect(host='jabber.name', port=5222)
print('running loop', flush=True)
asyncio.get_event_loop().run_forever()
print('done', flush=True)
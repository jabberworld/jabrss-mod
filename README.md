# JabRSS-mod

Never miss a headline again! JabRSS-mod is a simple RSS (RDF Site Summary: http://en.wikipedia.org/wiki/RDF_Site_Summary ) headline notification service for Jabber ( https://en.wikipedia.org/wiki/XMPP ). It is released under the GNU General Public License ( http://www.gnu.org/copyleft/gpl.html ).

### Source Code

The complete source code is available from the JabRSS-mod repository ( https://github.com/jabberworld/jabrss-mod ).

#### Requirements for Running JabRSS-mod

* Python 3 (this version is not compatible with Python 2)
* feedparser -- feed parsing and polling
* slixmpp -- XMPP connection and message handling
* requests -- HTTP fetching

Install the Python dependencies with:

`python3 -m pip install feedparser slixmpp requests`


### Summary of Commands

* `help` or `?` -- display the list of supported commands

* `subscribe http://host.domain/path/to/file.rss` -- subscribe to the given RSS URL (also `add` or `+`)

* `unsubscribe http://host.domain/path/to/file.rss` -- unsubscribe from the given RSS URL (also `del` or `-`)

* `list` -- list currently subscribed RSS URLs (if a URL is marked with "error" it means that the last update of the RSS resource failed for some reason)

* `info http://host.domain/path/to/file.rss` -- display some information about the given RSS URL

* `set plaintext` -- set the message type for headline notifications to normal/plaintext (default)

* `set chat` -- set the message type for headline notifications to chat/plaintext

* `set headline` -- set the message type for headline notifications to headline (please note that not all Jabber clients support headline messages)

* `set also_deliver` [`Away`] [`XA`] [`DND`] [`none`] -- also deliver headline messages when your presence is "Away", "Extended Away" or "Do Not Disturb" (default is empty; `none` resets the setting)

* `set size_limit` <`number`> -- limit the size of headline message to the specified amount of bytes (default is 1024)

* `set store_messages` <`number`> -- store at most the specified number of messages for later delivery (note that there is a hard limit of 48, default is 16)

* `set header` [`Title`] [`URL`] -- include an optional header line in headline notifications with the title and/or URL of the feed (default is empty)

* `set subject` [`Title`] [`URL`] -- include an optional subject in headline notifications with the title and/or URL of the feed (default is title)

* `configuration` -- displays your current configuration (also `conf`)

* `show statistics` -- displays some basic server statistics (also `statistics` or `stats`)

* `show usage` -- displays some basic usage statistics (also `usage`)

### Usage

First of all you have to subscribe to JabRSS-mod's presence (I am running a JabRSS-mod server with JID jabrss@cmeerw.net) and have to accept the subscription request from JabRSS-mod. Then you can start using it by subscribing to your favorite RSS headlines by sending a subscription command to JabRSS-mod ("subscribe http://some.url/path/to/rss", e.g. send "subscribe http://slashdot.org/slashdot.rdf" to subscribe to Slashdot headlines; or better yet try "subscribe http://cmeerw.org/blog.rdf" which is my Weblog where I will announce JabRSS-mod updates).

If you are looking for other RSS sources, you might want to take a look at Syndic8.com ( http://www.syndic8.com/ ) - just search for a feed and subscribe to the RSS URL via jabrss.

### Terms of Use and Privacy Policy

You are invited to use JabRSS-mod at your own risk. But be warned that any abuse will be acted upon.

The service is provided "as is" without warranty of any kind and might be changed or discontinued at any time without prior notice.

Currently, there is no real privacy policy. Any Jabber message sent to JabRSS-mod might be logged and analysed for debugging purposes. No information about you will be passed on to third parties without your permission.

BTW, if you like this service you could also consider a donation ( http://cmeerw.org/donate.html ) to keep it running.

### HTTP Bot Features

* User-Agent header: "JabRSS-mod (http://jabrss.cmeerw.org)", overridable via the `user_agent` key in `jabrss.conf`
* conditional HTTP GET (Last-Modified and ETag supported)
* gzip and deflate encoded HTTP requests
* RSS parser (based on feedparser) supports RSS 0.90, RSS 0.91, RSS 2.0, RDF 1.0 and Atom 0.3
* adaptive polling intervals based on the update frequency of the feed (every 30 minutes up to once per day)
* feeds not supporting conditional HTTP requests will be slightly penalized
* support for widely-used character data encodings
* proper handling of redirects

### Internals

* SQLite database backend
* multi-threaded architecture: one thread handling the XMPP communication, another thread polling RSS feeds; only feeds that are subscribed to by online users will be polled

### Configuration and Running

Configuration lives in the INI file `jabrss.conf` (section `[jabrss]`, keys `jid`, `password`, `host`, `resource`, `user_agent`), located next to `jabrssng.py`; use `-c`/`--config` for an alternate path. Options are resolved with the precedence **command line > config file > interactive prompt**. A commented template with all options and their meaning is provided as `jabrss.conf.example`.

Run the bot with:

```bash
python3 jabrssng.py [-c <config>] [-j <jid>] [-h <host>] [-p <password> | -f <password-file>]
```

* `-h`/`--connect-host` selects the XMPP server and may include a port, e.g. `linuxoid.in:5222`; if omitted, the server is resolved via SRV records of the JID domain (`_xmpps-client` before `_xmpp-client`)
* When `jabrss.db` or `jabrss_res.db` does not exist yet, the bot creates both databases automatically from the schema in `db.sqlite`

#!/usr/bin/python
# Copyright (C) 2001-2020, Christof Meerwald
# https://jabrss.cmeerw.org
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

# Feed parsing (RSS 0.9x/1.0/1.1/2.0, Atom 0.3/1.0, RDF, CDF) is
# delegated to the "feedparser" library; this module keeps the
# fetching (via requests), HTTP caching (ETag/Last-Modified),
# persistence (SQLite), item deduplication and adaptive polling.

import calendar, functools, hashlib, logging, random, re, socket
import sys, time, threading, traceback, warnings
import sqlite3
import requests
import feedparser

from email.utils import formatdate, mktime_tz, parsedate_tz

from html.entities import name2codepoint
from html.parser import HTMLParser
from io import StringIO
from urllib.parse import urlsplit, urljoin

logger = logging.getLogger('parserss')

# fetch of feeds with untrusted TLS certificates intentionally falls back
# to an unverified connection; we log our own warning then, so suppress
# the urllib3 "Unverified HTTPS request" warning it would emit as well
warnings.filterwarnings('ignore', message='Unverified HTTPS request')


__all__ = [
    'RSS_Resource', 'RSS_Resource_id2url', 'RSS_Resource_simplify'
    'RSS_Resource_db', 'RSS_Resource_Cursor',
    'UrlError', 'init_parserss',
]

unicode_trans = {
    0x00 : 0x20, 0x01 : 0x20, 0x02 : 0x20, 0x03 : 0x20,
    0x04 : 0x20, 0x05 : 0x20, 0x06 : 0x20, 0x07 : 0x20,
    0x08 : 0x20, 0x09 : 0x20, 0x0a : 0x0a, 0x0b : 0x20,
    0x0c : 0x20, 0x0d : 0x20, 0x0e : 0x20, 0x0f : 0x20,
    0x10 : 0x20, 0x11 : 0x20, 0x12 : 0x20, 0x13 : 0x20,
    0x14 : 0x20, 0x15 : 0x20, 0x16 : 0x20, 0x17 : 0x20,
    0x18 : 0x20, 0x19 : 0x20, 0x1a : 0x20, 0x1b : 0x20,
    0x1c : 0x20, 0x1d : 0x20, 0x1e : 0x20, 0x1f : 0x20
    }

random.seed()


def RSS_Resource_db():
    db = sqlite3.connect(DB_FILENAME, timeout=60000)
    db.isolation_level = None
    db.cursor().execute('PRAGMA synchronous=NORMAL')

    return db

class Null_Synchronizer:
    def acquire(self):
        return

    def release(self):
        return


# configuration settings
INTERVAL_DIVIDER = 3
MIN_INTERVAL = 1*60
MAX_INTERVAL = 24*60*60
MAX_XML_SIZE = 4 * 1024 * 1024
DB_FILENAME = 'parserss.db'
USER_AGENT = 'JabRSS (http://jabrss.cmeerw.org)'

def init_parserss(db_fname = DB_FILENAME,
                  min_interval = MIN_INTERVAL,
                  max_interval = MAX_INTERVAL,
                  interval_div = INTERVAL_DIVIDER,
                  dbsync_obj = Null_Synchronizer(),
                  user_agent = USER_AGENT):
    global DB_FILENAME, MIN_INTERVAL, MAX_INTERVAL, INTERVAL_DIVIDER, \
           USER_AGENT

    DB_FILENAME = db_fname
    MIN_INTERVAL = min_interval
    MAX_INTERVAL = max_interval
    INTERVAL_DIVIDER = interval_div
    USER_AGENT = user_agent

    RSS_Resource._db_sync = dbsync_obj


# convert an HTML fragment to plain text (used to turn the HTML that
# feedparser returns for titles/summaries into the plain-text
# descriptions that JabRSS delivers via XMPP)
def html2plain(html, ignore_errors=False):
    class HTML2Plain(HTMLParser):
        def __init__(self, ignore_errors=False):
            HTMLParser.__init__(self)
            self.__buf = StringIO()
            self.__processed, self.__errors, self.__ignore_errors = 0, 0, ignore_errors
            self.__in_pre, self.__has_space, self.__has_nl = False, True, True

        def close(self):
            HTMLParser.close(self)
            text = self.__buf.getvalue()
            self.__buf.close()

            if self.__ignore_errors or self.__errors == 0 or \
               self.__processed > 3*self.__errors:
                return text
            else:
                return None

        def handle_data(self, data):
            if not self.__in_pre and data:
                l = data.split()
                if l:
                    pre_space = not self.__has_space and (data[:1] in (' ', '\t', '\r', '\n'))
                    post_space = (data[-1:] in (' ', '\t', '\r', '\n'))
                    data = int(pre_space)*' ' + ' '.join(data.split()) + int(post_space)*' '
                    self.__has_space, self.__has_nl = post_space, False
                else:
                    data = ''

            self.__buf.write(data)

        def handle_charref(self, name):
            try:
                self.handle_data(chr(int(name)))
            except ValueError:
                self.__errors += 1

        def handle_entityref(self, name):
            try:
                self.handle_data(chr(name2codepoint[name]))
            except KeyError:
                self.__errors += 1

        def handle_starttag(self, tag, attrs):
            if tag in ('br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'h7',
                       'div', 'p', 'pre', 'tr'):
                if not self.__has_nl:
                    self.__buf.write('\n')
                    self.__has_nl, self.__has_space = True, True
                if tag == 'pre':
                    self.__in_pre = True
            elif tag in ('li',):
                if not self.__has_nl:
                    self.__buf.write('\n')
                self.__buf.write(' * ')
                self.__has_nl, self.__has_space = True, True
            elif tag in ('td',):
                if not self.__has_space and not self.__has_nl:
                    self.__buf.write(' ')
                    self.__has_nl, self.__has_space = False, True
            elif tag == 'img':
                d = dict(attrs)
                self.handle_data(d.get('alt', '') or d.get('title', ''))
            self.__processed += 1

        def handle_startendtag(self, tag, attrs):
            return self.handle_starttag(tag, attrs)

        def handle_endtag(self, tag):
            if tag == 'pre':
                self.__in_pre, self.__has_nl, self.__has_space = False, False, True
            self.__processed += 1

        def handle_comment(self, data):
            self.__processed += 1

        def unknown_decl(self, data):
            self.__errors += 1


    try:
        parser = HTML2Plain(ignore_errors)
        parser.feed(html)
        text = parser.close()
    except:
        text = None

    if text == None:
        return html
    else:
        return text


# HTML autodiscovery: extract the feed URL from a page containing
# <link rel="alternate" type="application/rss+xml" href="...">
class _AutodiscoveryParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.redirect_url = None

    def handle_starttag(self, tag, attrs):
        if tag == 'link' and self.redirect_url is None:
            dattrs = dict(attrs)
            if (dattrs.get('rel') == 'alternate' and
                dattrs.get('type') in ('application/atom+xml',
                                       'application/rdf+xml',
                                       'application/rss+xml')):
                self.redirect_url = dattrs.get('href')

    def handle_startendtag(self, tag, attrs):
        return self.handle_starttag(tag, attrs)


def _try_autodiscovery(raw):
    text = None
    for enc in ('utf-8', 'iso8859-1'):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, ValueError):
            pass

    if text is None:
        return None

    parser = _AutodiscoveryParser()
    try:
        parser.feed(text)
        parser.close()
    except:
        return None

    return parser.redirect_url


class UrlError(ValueError):
    pass

def split_url(url):
    u = urlsplit(url)

    protocol = u.scheme

    netloc_comp = u.netloc.split(':', 2)
    host = netloc_comp[0].lower().split('.')
    if len(netloc_comp) >= 2:
        port = netloc_comp[1]
    else:
        port = None

    path = u.path
    if u.query:
        path += '?' + u.query

    validhost = False

    if len(host) == 1 and host[0][:1] == '[' and host[0][-1:] == ']':
        try:
            host[0] = '[%s]' % (socket.inet_ntop(socket.AF_INET6, socket.inet_pton(socket.AF_INET6, host[0][1:-1])))
            validhost = True
        except socket.error:
            validhost = False
    elif len(host) == 4:
        try:
            host = socket.inet_ntop(socket.AF_INET, socket.inet_pton(socket.AF_INET, '.'.join(host))).split('.')
            if int(host[0]) == 10:
                pass
            elif int(host[0]) == 172 and int(host[1]) in range(16, 32):
                pass
            elif int(host[0]) == 192 and int(host[1]) == 168:
                pass
            elif int(host[0]) >= 240:
                pass
            else:
                validhost = True
        except socket.error:
            validhost = False

    if not validhost and len(host) >= 2:
        if len(host[-1]) >= 2 and host[-1].isalpha():
            validhost = True

    if not validhost:
        raise UrlError('invalid host in URL "%s"' % ('.'.join(host),))

    if protocol == 'http':
        if port not in (None, '80', 'http'):
            raise UrlError('http ports != 80 not allowed')
    elif protocol == 'https':
        if port not in (None, '443', 'https'):
            raise UrlError('https ports != 443 not allowed')
    else:
        raise UrlError('unsupported protocol "%s"' % (protocol))

    if path == '':
        path = '/'

    while path[:2] == '//':
        path = path[1:]

    return protocol, '.'.join(host), path


def normalize_text(s):
    s = s.translate(unicode_trans)

    s = '\n'.join(filter(lambda x: x != '', [ x.strip() for x in s.split('\n') ]))
    s = ' '.join(filter(lambda x: x != '', s.split(' ')))
    return s

def normalize_obj(o):
    for attr in dir(o):
        if attr[0] != '_':
            value = getattr(o, attr)
            if type(value) is str:
                setattr(o, attr, normalize_text(value))

    return o

def normalize_item(item):
    normalize_obj(item)

    if item.descr == '':
        item.descr = None

    if not hasattr(item, 'descr_plain'):
        item.descr_plain = item.descr

    if not hasattr(item, 'descr_xhtml'):
        item.descr_xhtml = None

    if item.descr_plain:
        item.descr_plain = item.descr_plain[:4096]

    del item.descr

    return item


def parse_Rfc822DateTime(s):
    if s == None:
        return None

    try:
        tstamp = int(mktime_tz(parsedate_tz(s)))
    except:
        tstamp = None

    return tstamp


def compare_items(l, r):
    lguid, ltitle, llink = l.guid, l.title, l.link
    rguid, rtitle, rlink = r.guid, r.title, r.link

    if ltitle == rtitle:
        if (lguid != None) and (rguid != None):
            return lguid == rguid

        lurl = urlsplit(llink)
        rurl = urlsplit(rlink)

        if lurl.scheme == rurl.scheme and lurl.path == rurl.path and \
                lurl.query == rurl.query and lurl.fragment == rurl.fragment:
            lhostparts = lurl.netloc.lower().split('.')
            if lhostparts[-1] == '':
                del lhostparts[-1]

            rhostparts = rurl.netloc.lower().split('.')
            if rhostparts[-1] == '':
                del rhostparts[-1]

            if len(lhostparts) >= 2:
                del lhostparts[-1]
            if len(rhostparts) >= 2:
                del rhostparts[-1]

            if len(lhostparts) > len(rhostparts):
                tmp = lhostparts
                lhostparts = rhostparts
                rhostparts = tmp
                del tmp

            if len(lhostparts) == len(rhostparts):
                return lhostparts == rhostparts
            else:
                return lhostparts == rhostparts[-len(lhostparts):]
        else:
            return 0
    else:
        return 0

class CleanupOnError:
    def __init__(self, callable=None):
        self.__callable = callable

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_value, traceback):
        if self.__callable != None and exc_type != None:
            self.__callable()

class Cursor:
    def __init__(self, dbconn, parent=None):
        self._txn, self._locked = False, False
        self._parent = parent
        if self._parent == None:
            if not hasattr(dbconn, 'cursor'):
                self._cursor = dbconn().cursor()
            else:
                self._cursor = dbconn.cursor()
        else:
            self._cursor = None

    def __enter__(self):
        if self._parent == None:
            return self
        else:
            return self._parent

    def __exit__(self, exc_type, exc_value, traceback):
        if self._parent == None:
            self.commit()

    def commit(self):
        try:
            if self._txn:
                self._cursor.execute('COMMIT')
                self._txn = False
        finally:
            if self._locked:
                RSS_Resource._db_sync.release()
                self._locked = False

    def begin(self):
        if not self._locked:
            RSS_Resource._db_sync.acquire()
            self._locked = True

        if not self._txn:
            self._cursor.execute('BEGIN')
            self._txn = True

    def execute(self, stmt, bindings=None):
        self.begin()

        if bindings == None:
            return self._cursor.execute(stmt)
        else:
            return self._cursor.execute(stmt, bindings)

    def __getattr__(self, name):
        if name == 'lastrowid':
            return self._cursor.lastrowid
        elif name == 'rowcount':
            return self._cursor.rowcount

        raise AttributeError('object has no attribute \'%s\'' % (name,))

    def getdb(self):
        return self._cursor.getconnection()

RSS_Resource_Cursor = Cursor

class Data:
    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


class FeedError(Exception):
    def __init__(self, e):
        Exception.__init__(self, e)


# ---- feedparser adapter -------------------------------------------------

def _content_rank(typ):
    if typ.startswith('text/plain'):
        return 2
    elif typ in ('text/html', 'application/xhtml+xml') or 'html' in typ:
        return 1
    return 0

def _as_plain(value, detail=None):
    if value is None:
        return None

    typ = ''
    if detail is not None:
        typ = detail.get('type') or ''

    if typ.startswith('text/plain'):
        return value.strip() if isinstance(value, str) else value

    if 'html' in typ or typ in ('application/xhtml+xml',):
        return html2plain(value) or value

    return value.strip() if isinstance(value, str) else value

def _first_ts(*parsed_list):
    for parsed in parsed_list:
        if parsed is not None:
            return calendar.timegm(parsed)
    return None

def _feed_item_description(entry):
    content = entry.get('content')
    if content:
        best, best_rank = None, -1
        for c in content:
            rank = _content_rank(c.get('type') or '')
            if rank >= best_rank:
                best, best_rank = c, rank

        value = best.get('value')
        if value is None:
            return None
        if best.get('type', '').startswith('text/plain'):
            return value
        return html2plain(value) or value

    summary = entry.get('summary')
    if summary is not None:
        return _as_plain(summary, entry.get('summary_detail'))

    return None


class FeedParser:
    # feedparser-based replacement for the old streaming XML parser
    def __init__(self, base_url):
        # base_url is a (protocol, host, path) tuple
        self.__base_url = base_url
        self.__buf = []
        self.__error_log = None

        self.redirect_url = None
        self.info = None
        self.elements = []

    def get_error_log(self):
        return self.__error_log

    def get_info(self):
        return self.info

    def get_items(self):
        return self.elements

    def get_redirect_url(self):
        return self.redirect_url

    def feed(self, data):
        self.__buf.append(data)

    def __resolve_link(self, url):
        if url == None:
            return ''

        protocol, host, path = self.__base_url
        base = '%s://%s%s' % (protocol, host, path)
        return urljoin(base, url)

    def __feed_info(self, feed):
        if feed is None:
            return None

        title = feed.get('title')
        if title is not None:
            title = _as_plain(title, feed.get('title_detail'))

        descr = feed.get('subtitle')
        if descr is None:
            descr = feed.get('description')
        if descr is not None:
            descr = _as_plain(descr,
                              feed.get('subtitle_detail') or
                              feed.get('description_detail'))

        link = feed.get('link') or ''
        if not link:
            for l in feed.get('links') or []:
                if l.get('rel') in (None, 'alternate'):
                    link = l.get('href') or ''
                    break

        guid = feed.get('id')
        published = _first_ts(feed.get('published_parsed'),
                              feed.get('updated_parsed'))

        return Data(title=title, descr=descr, link=link,
                    guid=guid, published=published)

    @staticmethod
    def __is_feed(feed, entries):
        # a real feed either has entries or at least a channel title/link/id
        if entries:
            return True
        if feed.get('title') or feed.get('link') or feed.get('id'):
            return True
        return False

    @staticmethod
    def __autodiscovery_url(feed, raw):
        for l in feed.get('links') or []:
            if (l.get('rel') == 'alternate' and
                l.get('type') in ('application/atom+xml',
                                  'application/rdf+xml',
                                  'application/rss+xml')):
                href = l.get('href')
                if href:
                    return href

        return _try_autodiscovery(raw)

    def __feed_item(self, entry):
        title = entry.get('title')
        if title is not None:
            title = _as_plain(title, entry.get('title_detail'))

        descr = _feed_item_description(entry)

        link = entry.get('feedburner_origlink') or \
            entry.get('pheedo_origlink') or ''
        if not link:
            link = entry.get('link') or ''
        if not link:
            for l in entry.get('links') or []:
                if l.get('rel') in (None, 'alternate'):
                    link = l.get('href') or ''
                    break

        enclosure = None
        for e in entry.get('enclosures') or []:
            enclosure = e
            break
        if enclosure is not None and enclosure.get('href'):
            if not link or enclosure.get('type') in ('audio/mpeg',):
                # prioritise certain types of enclosures
                link = enclosure.get('href')

        guid = entry.get('id')
        published = _first_ts(entry.get('published_parsed'),
                              entry.get('updated_parsed'))

        return Data(title=title, descr=descr, link=link,
                    guid=guid, published=published)

    def close(self):
        raw = b''.join(self.__buf)
        self.__buf = None

        try:
            parsed = feedparser.parse(raw)
        except Exception as e:
            raise FeedError(str(e))

        if parsed.get('bozo'):
            self.__error_log = str(parsed.get('bozo_exception'))

        feed = parsed.get('feed') or {}
        entries = parsed.get('entries') or []

        if not self.__is_feed(feed, entries):
            # document not recognized as a feed, retry as HTML
            # (autodiscovery)
            redirect_url = self.__autodiscovery_url(feed, raw)
            if redirect_url is not None:
                self.redirect_url = self.__resolve_link(redirect_url)
                return

            raise FeedError('No feed information found')

        info = self.__feed_info(feed)
        if info is None:
            raise FeedError('No feed information found')

        info.link = self.__resolve_link(info.link)
        self.info = info

        for entry in entries:
            item = self.__feed_item(entry)
            item.link = self.__resolve_link(item.link)
            self.elements.append(item)


def default_redirect_cb(redirect_url, db, redirect_count,
                        generate_id, connect_timeout, timeout):
    resource_url = RSS_Resource_simplify(redirect_url)
    while resource_url != None:
        redirect_resource = RSS_Resource(resource_url, db, generate_id, connect_timeout, timeout)
        resource_url, redirect_seq = redirect_resource.redirect_info(db)

    new_items, next_item_id, redirect_target, redirect_seq, redirects = redirect_resource.update(db, redirect_count)

    if len(new_items) > 0:
        redirects.insert(0, (redirect_resource, new_items, next_item_id))

    if redirect_target != None:
        redirect_resource = redirect_target

    return redirect_resource, redirects


@functools.total_ordering
class RSS_Resource:
    NR_ITEMS = 96

    _db_sync = Null_Synchronizer()
    http_proxy = None


    def __init__(self, url, res_db=None, generate_id=None,
                 connect_timeout=30, timeout=20):
        self._lock = threading.Lock()
        self._url = url
        self._url_protocol, self._url_host, self._url_path = split_url(url)
        self._generate_id = generate_id
        self._connect_timeout, self._timeout = connect_timeout, timeout

        self._id = None
        self._last_updated, self._last_modified = None, None
        self._etag = None
        self._invalid_since, self._err_info = None, None
        self._redirect, self._redirect_seq = None, None
        self._insecure = False
        self._penalty = 0
        title, description, link = None, None, None

        if res_db == None:
            db = RSS_Resource_db()
        else:
            db = res_db

        with Cursor(db) as cursor:
            result = cursor.execute('SELECT rid, last_updated, last_modified, etag, invalid_since, redirect, redirect_seq, penalty, err_info, title, description, link FROM resource WHERE url=?',
                                    (self._url,))
            for row in result:
                self._id, self._last_updated, self._last_modified, self._etag, self._invalid_since, self._redirect, self._redirect_seq, self._penalty, self._err_info, title, description, link = row

            if self._id == None:
                if generate_id == None:
                    cursor.execute('INSERT INTO resource (url) VALUES (?)',
                                   (self._url,))
                    self._id = cursor.lastrowid
                else:
                    for i in range(0, 5):
                        try:
                            id = generate_id()
                            cursor.execute('INSERT INTO resource (rid, url) VALUES (?, ?)',
                                           (id, self._url))
                            self._id = id
                            break
                        except sqlite3.IntegrityError:
                            pass

                    if self._id == None:
                        raise UrlError('Unable to add to database')

            if self._last_updated == None:
                self._last_updated = 0

            if self._penalty == None:
                self._penalty = 0

            if title == None:
                title = self._url
            if link == None:
                link = ''
            if description == None:
                description = ''

            self._channel_info = Data(title=title, link=link, descr=description)

            self._history = []
            result = cursor.execute('SELECT time_items0, time_items1, time_items2, time_items3, time_items4, time_items5, time_items6, time_items7, time_items8, time_items9, time_items10, time_items11, time_items12, time_items13, time_items14, time_items15, nr_items0, nr_items1, nr_items2, nr_items3, nr_items4, nr_items5, nr_items6, nr_items7, nr_items8, nr_items9, nr_items10, nr_items11, nr_items12, nr_items13, nr_items14, nr_items15 FROM resource_history WHERE rid=?',
                           (self._id,))
            for row in result:
                history_times = filter(lambda x: x!=None, row[0:16])
                history_nr = filter(lambda x: x!=None, row[16:32])
                self._history = list(zip(history_times, history_nr))
        del db


    def __eq__(self, other):
        return (other != None) and (self._id == other._id)

    def __lt__(self, other):
        return (other != None) and (self._id < other._id)


    def sync(self):
        return self._lock

    def lock(self):
        self._lock.acquire()

    def unlock(self):
        self._lock.release()


    def url(self):
        return self._url

    def id(self):
        return self._id

    def channel_info(self):
        return self._channel_info

    def times(self):
        last_updated, last_modified, invalid_since = self._last_updated, self._last_modified, self._invalid_since
        if last_modified == None:
            last_modified = 0

        return last_updated, last_modified, invalid_since

    def redirect_info(self, res_db=None):
        if self._redirect == None:
            return None, None

        if res_db == None:
            db = RSS_Resource_db()
        else:
            db = res_db

        with Cursor(db) as cursor:
            result = cursor.execute('SELECT url FROM resource WHERE rid=?',
                                    (self._redirect,))
            redirect_url = None
            for row in result:
                redirect_url = row[0]

        return redirect_url, self._redirect_seq

    def penalty(self):
        return self._penalty

    def error_info(self):
        return self._err_info


    def history(self):
        return self._history


    # @return ([item], next_item_id, redirect_resource, redirect_seq, [redirects])
    # locks the resource object if new_items are returned
    def update(self, db=RSS_Resource_db, redirect_count=5,
               redirect_cb=default_redirect_cb):
        now = int(time.time())

        # sanity check update interval
        if now - self._last_updated < 60:
            return [], None, None, None, []

        error_info = None
        nr_new_items = 0
        feed_xml_downloaded = False
        feed_xml_changed = False
        first_item_id = None
        items = []

        prev_updated = self._last_updated
        self._last_updated = now

        if not self._invalid_since:
            # expect the worst, will be reset later
            self._invalid_since = now

        sess = requests.Session()
        sess.headers.update({ 'User-Agent' : USER_AGENT })

        redirect_penalty = 0
        redirect_tries = redirect_count
        redirect_permanent = True
        redirect_resource = None
        redirect_seq = None
        redirects = []
        visited = {}

        with Cursor(db) as cursor:
            try:
                url_protocol, url_host, url_path = self._url_protocol, self._url_host, self._url_path
                logger.debug('updating %s://%s%s' % (url_protocol, url_host, url_path))

                while redirect_tries > 0:
                    redirect_tries = -(redirect_tries - 1)

                    if redirect_permanent:
                        redirect_url = url_protocol + '://' + url_host + url_path

                        if redirect_url != self._url:
                            redirect_resource, redirects = redirect_cb(redirect_url, db, -redirect_tries + 1, self._generate_id, self._connect_timeout, self._timeout)

                            # only perform the redirect if target is valid
                            if redirect_resource._invalid_since:
                                error_info = redirect_resource._err_info
                                self._last_modified = redirect_resource._last_modified
                                self._etag = redirect_resource._etag
                                redirect_resource = None
                            else:
                                redirect_items, redirect_seq = redirect_resource.get_headlines(0, cursor)

                                items, first_item_id, nr_new_items = self._process_new_items(redirect_items, cursor)
                                del redirect_items

                                self._last_modified, self._etag = None, None

                                self._redirect = redirect_resource._id
                                self._redirect_seq = redirect_seq
                                cursor.execute('UPDATE resource SET redirect=?, redirect_seq=? WHERE rid=?',
                                               (self._redirect,
                                                self._redirect_seq, self._id))

                            break


                    headers = {}
                    if self._last_modified:
                        headers['If-Modified-Since'] = formatdate(self._last_modified, usegmt=True)
                    if self._etag != None:
                        headers['If-None-Match'] = self._etag

                    k = (url_protocol, url_host, url_path)
                    if k in visited:
                        error_info = 'redirect cycle %s://%s%s' % k
                        break
                    else:
                        visited[k] = True

                    # verify the TLS certificate first; if that fails
                    # (untrusted/expired certificate), fall back to an
                    # insecure connection for this feed, but log our own
                    # warning so it stays visible on the console
                    if self._insecure:
                        logger.warning('using insecure (unverified TLS) connection for %s://%s%s' %
                                       (url_protocol, url_host, url_path))
                        response = sess.get('%s://%s%s' % (url_protocol, url_host,
                                                           url_path),
                                            allow_redirects=False, stream=True,
                                            timeout=self._connect_timeout,
                                            verify=False)
                    else:
                        try:
                            response = sess.get('%s://%s%s' % (url_protocol, url_host,
                                                               url_path),
                                                allow_redirects=False, stream=True,
                                                timeout=self._connect_timeout,
                                                verify=True)
                        except requests.exceptions.SSLError as e:
                            self._insecure = True
                            logger.warning('TLS certificate verification failed for %s://%s%s: %s - using insecure fallback for this feed' %
                                           (url_protocol, url_host, url_path, e))
                            response = sess.get('%s://%s%s' % (url_protocol, url_host,
                                                               url_path),
                                                allow_redirects=False, stream=True,
                                                timeout=self._connect_timeout,
                                                verify=False)

                    errcode = response.status_code
                    errmsg = response.reason
                    headers = response.headers

                    # check the error code
                    # handle "304 Not Modified"
                    if errcode == 304 or errcode == 412:
                        # RSS resource is valid
                        self._invalid_since = None
                    elif (errcode >= 200) and (errcode < 300):
                        feed_xml_downloaded = True

                        self._last_modified, self._etag = parse_Rfc822DateTime(headers.get('last-modified', None)), headers.get('etag', None)

                        rss_parser = FeedParser((self._url_protocol,
                                                 self._url_host,
                                                 self._url_path))

                        bytes_processed = 0
                        xml_started = False
                        file_hash = hashlib.md5()

                        for data in response.iter_content(4096):
                            file_hash.update(data)

                            if not xml_started:
                                data = data.lstrip()
                                if data:
                                    xml_started = True

                            bytes_processed = bytes_processed + len(data)
                            if bytes_processed > MAX_XML_SIZE:
                                raise ValueError('file exceeds maximum allowed decompressed size')

                            rss_parser.feed(data)

                        response.close()
                        rss_parser.close()

                        redirect_url = rss_parser.get_redirect_url()
                        if redirect_url:
                            logger.info('Following feed autodiscovery to "%s"' % (redirect_url,))
                            url_protocol, url_host, url_path = split_url(redirect_url)
                            redirect_tries = -redirect_tries
                        else:
                            error_log = rss_parser.get_error_log()
                            if error_log:
                                logger.warning('feed parser error log:\n%s' % (error_log,))

                            new_channel_info = normalize_obj(rss_parser.get_info())

                            hash_buffer = file_hash.digest()
                            cursor.execute('UPDATE resource SET hash=? WHERE rid=? AND (hash IS NULL OR hash<>?)',
                                           (hash_buffer, self._id, hash_buffer))
                            feed_xml_changed = (cursor.rowcount != 0)

                            self._update_channel_info(new_channel_info, cursor)

                            new_items = [ normalize_item(x) for x in rss_parser.get_items() ]
                            new_items.reverse()

                            items, first_item_id, nr_new_items = self._process_new_items(new_items, cursor)
                            del new_items

                    # handle "301 Moved Permanently", "302 Found" and
                    # "307 Temporary Redirect"
                    elif (errcode >= 300) and (errcode < 400):
                        bytes_received = 0
                        for data in response.iter_content(4096):
                            bytes_received = bytes_received + len(data)
                            if bytes_received > 128 * 1024:
                                raise ValueError('file exceeds maximum allowed size')

                        response.close()

                        if errcode != 301:
                            redirect_permanent = False
                            redirect_penalty += 1

                        redirect_url = headers.get('location', None)
                        if redirect_url:
                            base_url = '%s://%s/%s' % (url_protocol, url_host, url_path)
                            redirect_url = urljoin(base_url, redirect_url)
                            logger.info('Following redirect (%d) to "%s"' % (errcode, redirect_url))
                            redirect_tries = -redirect_tries
                        else:
                            error_info = 'HTTP: %d %s' % (errcode, repr(errmsg))
                            logger.warning(error_info + '\n' + str(headers))
                    else:
                        error_info = 'HTTP: %d %s' % (errcode, repr(errmsg))
                        logger.warning(error_info + '\n' + str(headers))

                if self._invalid_since and not error_info and redirect_tries == 0:
                    error_info = 'redirect: maximum number of redirects exceeded'
            except socket.timeout as e:
                error_info = 'timeout: ' + str(e)
            except requests.exceptions.HTTPError as e:
                error_info = 'HTTP: ' + str(e)
            except requests.exceptions.ConnectionError as e:
                error_info = 'HTTP connection: ' + str(e)
            except socket.error as e:
                error_info = 'socket: ' + str(e)
            except IOError as e:
                error_info = 'I/O error: ' + str(e)
            except FeedError as e:
                error_info = 'feed: ' + str(e)
            except AssertionError as e:
                error_info = 'assertion: ' + str(e)
            except UnicodeError as e:
                error_info = 'encoding: ' + str(e)
            except LookupError as e:
                error_info = 'encoding: ' + str(e)
            except ValueError as e:
                error_info = 'misc: ' + str(e)
            except:
                traceback.print_exc(file=sys.stdout)

            if error_info:
                logger.warning('Error: %s' % (error_info,))

            if error_info != self._err_info:
                self._err_info = error_info
                cursor.execute('UPDATE resource SET err_info=? WHERE rid=?',
                               (self._err_info, self._id))

            if not self._invalid_since:
                if feed_xml_downloaded:
                    if nr_new_items > 0:
                        # downloaded and new items available, good
                        self._penalty = (5 * self._penalty) // 6
                    elif not feed_xml_changed:
                        # downloaded, but not changed, very bad
                        self._penalty = (3 * self._penalty) // 4 + 256
                    else:
                        # downloaded and changed, but no new items, bad
                        self._penalty = (15 * self._penalty) // 16 + 64
                else:
                    # "not modified" response from server, good
                    self._penalty = (3 * self._penalty) // 4

            if redirect_penalty > 0:
                # penalty for temporary redirects
                self._penalty = (7 * self._penalty) // 8 + 128


            cursor.execute('UPDATE resource SET last_modified=?, last_updated=?, etag=?, invalid_since=?, penalty=? WHERE rid=?',
                           (self._last_modified, self._last_updated, self._etag,
                            self._invalid_since, self._penalty, self._id))

        if nr_new_items:
            new_items = items[-nr_new_items:]
            next_item_id = first_item_id + len(items)
        else:
            new_items = []
            next_item_id = None

        return new_items, next_item_id, redirect_resource, redirect_seq, redirects


    def _update_channel_info(self, new_channel_info, cursor):
        if self._channel_info != new_channel_info:
            self._channel_info = new_channel_info

            cursor.execute('UPDATE resource SET title=?, link=?, description=? WHERE rid=?',
                           (self._channel_info.title,
                            self._channel_info.link,
                            self._channel_info.descr,
                            self._id))


    # @return ([item], first_item_id, nr_new_items)
    def _process_new_items(self, new_items, cursor):
        items, next_item_id = self.get_headlines(0, cursor)
        first_item_id = next_item_id - len(items)

        nr_new_items = self._update_items(items, new_items)
        del new_items

        if nr_new_items:
            # we must not have any other objects locked when trying to lock
            # a resource
            cursor.commit()
            self.lock()
            cleanup = CleanupOnError(self.unlock)
        else:
            cleanup = CleanupOnError()

        with cleanup:
            cursor.begin()

            if len(items) > RSS_Resource.NR_ITEMS:
                first_item_id += len(items) - RSS_Resource.NR_ITEMS
                del items[:-RSS_Resource.NR_ITEMS]
                cursor.execute('DELETE FROM resource_data WHERE rid=? AND seq_nr<?',
                               (self._id, first_item_id))

            # RSS resource is valid
            self._invalid_since = None

            if nr_new_items:
                # update history information
                self._history.append((int(time.time()), nr_new_items))
                self._history = self._history[-16:]

                history_times = [ x[0] for x in self._history ]
                if len(history_times) < 16:
                    history_times += (16 - len(history_times)) * [None]

                history_nr = [ x[1] for x in self._history ]
                if len(history_nr) < 16:
                    history_nr += (16 - len(history_nr)) * [None]

                cursor.execute('INSERT INTO resource_history (rid, time_items0, time_items1, time_items2, time_items3, time_items4, time_items5, time_items6, time_items7, time_items8, time_items9, time_items10, time_items11, time_items12, time_items13, time_items14, time_items15, nr_items0, nr_items1, nr_items2, nr_items3, nr_items4, nr_items5, nr_items6, nr_items7, nr_items8, nr_items9, nr_items10, nr_items11, nr_items12, nr_items13, nr_items14, nr_items15) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               tuple([self._id] + history_times + history_nr))

                i = first_item_id
                for item in items:
                    cursor.execute('INSERT INTO resource_data (rid, seq_nr, published, title, link, guid, descr_plain, descr_xhtml) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                                   (self._id, i,
                                    item.published, item.title, item.link,
                                    item.guid, item.descr_plain, item.descr_xhtml))
                    i += 1

            return items, first_item_id, nr_new_items


    # @return nr_new_items
    def _update_items(self, items, new_items):
        nr_old_items = len(items)
        nr_new_items = 0

        tstamplist = list(filter(lambda x: x != None,
                                 [ x.published for x in items ]))
        tstamplist.sort()

        if (len(tstamplist) > 4) and (len(new_items) > RSS_Resource.NR_ITEMS):
            tstamplist = tstamplist[len(tstamplist) // 2 : -len(tstamplist) // 6]
            cutoff = sum(tstamplist) // len(tstamplist)
        elif len(tstamplist) > 2:
            cutoff = tstamplist[0]
        else:
            cutoff = 0

        new_items = list(filter(lambda x: (x.published == None) or (x.published >= cutoff), new_items))
        new_items.sort(key=lambda x: x.published or 0)

        for item in new_items:
            found = False

            for i in range(0, len(items)):
                if compare_items(items[i], item):
                    items[i] = item
                    found = True

            if not found:
                items.append(item)
                nr_new_items = nr_new_items + 1

        return nr_new_items


    # @return ([item], next id)
    def get_headlines(self, first_id, db_cursor=None, db=RSS_Resource_db):
        with Cursor(db, db_cursor) as cursor:
            if first_id == None:
                first_id = 0

            result = cursor.execute('SELECT seq_nr, published, title, link, guid, descr_plain, descr_xhtml FROM resource_data WHERE rid=? AND seq_nr>=? ORDER BY seq_nr',
                                    (self._id, first_id))
            items = []
            last_id = first_id
            for seq_nr, published, title, link, guid, descr_plain, descr_xhtml in result:
                if seq_nr >= last_id:
                    last_id = seq_nr + 1
                items.append(Data(published=published, title=title, link=link,
                                  guid=guid, descr_plain=descr_plain,
                                  descr_xhtml=descr_xhtml))

        return items, last_id


    def next_update(self, randomize=True):
        min_interval = MIN_INTERVAL
        max_interval = MAX_INTERVAL

        if len(self._history) >= 2:
            hist_items = len(self._history)

            sum_items = 0
            for h in self._history[1:]:
                sum_items += h[1]
            time_span = self._last_updated - self._history[0][0]

            if hist_items >= 12:
                time_span_old = self._history[hist_items // 2][0] - self._history[0][0]
                sum_items_old = 0
                for h in self._history[1:hist_items // 2 + 1]:
                    sum_items_old += h[1]

                if (3 * sum_items_old < sum_items) and (5 * time_span_old < time_span):
                    time_span = time_span_old
                    sum_items = sum_items_old
                # sum_items_new = sum_items - sum_items_old
                elif (3 * sum_items_old > 2 * sum_items) and (5 * time_span_old > 4 * time_span):
                    time_span = time_span - time_span_old
                    sum_items = sum_items - sum_items_old

            interval = time_span // sum_items // INTERVAL_DIVIDER

            # apply a bonus for well-behaved feeds
            interval = 32 * interval // (64 - self._penalty // 28)
            max_interval = 32 * max_interval // (64 - self._penalty // 28)
            min_interval = 32 * min_interval // (48 - self._penalty // 64)
        elif len(self._history) == 1:
            time_span = self._last_updated - self._history[0][0]

            interval = 30*60 + time_span // 3
            min_interval = 60*60
        elif self._invalid_since:
            time_span = self._last_updated - self._invalid_since

            interval = 4*60*60 + time_span // 4
            max_interval = 48*60*60
        else:
            interval = 8*60*60

        if self._url.lower().find('slashdot.org') != -1:
            # yes, slashdot sucks - this is a special slashdot
            # throttle to avaoid being banned by slashdot
            interval = interval + 150*60

        # apply upper and lower bounds to the interval
        interval = min(max_interval, max(min_interval, interval))

        # and add some random factor
        if randomize:
            return self._last_updated + interval + int(random.normalvariate(30, 50 + interval // 50))
        else:
            return self._last_updated + interval


def RSS_Resource_id2url(res_id, db_cursor=None):
    with Cursor(RSS_Resource_db) as cursor:
        url = None
        result = cursor.execute('SELECT url FROM resource WHERE rid=?',
                                (res_id,))
        for row in result:
            url = row[0]

    if url == None:
        raise KeyError(res_id)

    return url


def RSS_Resource_simplify(url):
    url_protocol, url_host, url_path = split_url(url)
    simple_url = '%s://%s%s' % (url_protocol, url_host, url_path)
    return url


if __name__ == '__main__':
    logger = logging.getLogger()
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.DEBUG)

    init_parserss()
    db = RSS_Resource_db()

    for url in sys.argv[1:]:
        while url != None:
            resource = RSS_Resource(url, db)
            url, seqnr = resource.redirect_info(db)

        new_items, next_item_id, redirect_resource, redirect_seq, redirects = resource.update(db)
        channel_info = resource.channel_info()
        print('%s %s %s' % (channel_info.title, channel_info.link, channel_info.descr))
        error_info = resource.error_info()
        if error_info:
            print('error info %s' % (error_info))

        if len(new_items) > 0:
            print('new items (next id: %d):\n  %s' %
                  (next_item_id,
                   '\n  '.join(['%s - %s' % (x.title, x.link) for x in new_items])))

    db.close()
    del db

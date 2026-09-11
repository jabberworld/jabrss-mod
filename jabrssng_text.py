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


TEXT_WELCOME = '''\
Welcome to JabRSS. Please note that the current privacy policy is quite simple: all your data are belong to me and might be sold to your favorite spammer. :-) For more information, please visit the JabRSS Web site at http://jabrss.cmeerw.org

BTW, if you like this service, you can help keeping it running by making a donation, see http://cmeerw.org/donate.html'''

TEXT_NEWUSER = '''

Now there is only one more thing to do before you can use JabRSS: you have to authorize the presence subscription request from JabRSS. This is necessary so that JabRSS knows your presence status and only sends you RSS headlines when you are online.'''

TEXT_HELP = '''\
Supported commands:

help | ?
subscribe   | add | + http://host.domain/path/feed.rss
unsubscribe | del | - http://host.domain/path/feed.rss
info http://host.domain/path/feed.rss
list
set ( plaintext | chat | headline )
set also_deliver [ Away ] [ XA ] [ DND ] [ none ]
set header [ Title ] [ URL ]
set subject  [ Title ] [ URL ]
set size_limit <num>
set store_messages <num>
configuration   || conf
show statistics || statistics || stats
show usage      || usage

Please refer to the JabRSS command reference at http://dev.cmeerw.org/jabrss/Documentation for more information.

And of course, if you like this service you might also consider a donation, see http://cmeerw.org/donate.html'''
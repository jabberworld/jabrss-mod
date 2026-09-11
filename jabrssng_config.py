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


import configparser, getopt, os, socket, sys

from getpass import getpass

from slixmpp import JID


TEXT_USAGE = '''\
Usage: python3 jabrssng.py [options]

The bot reads its connection parameters from the config file (default:
jabrss.conf next to this script) or from the command line. Command line
options take precedence over the config file. Parameters missing from
both are requested interactively (when running on a terminal) and saved
back to the config file.

Options:
  --help                 show this help and exit
  -c, --config <file>    path to the config file
  -j, --jid <jid>        JabRSS/Jabber ID, a resource is optional
  -r, --resource <res>   resource to use (default: hostname)
  -h, --connect-host <host>
                         XMPP server to connect to; may include a port,
                         e.g. "linuxoid.in:5222" (default: SRV records
                         of the Jabber ID domain, trying _xmpps-client
                         before _xmpp-client)
  -p, --password <pw>    Jabber password
  -f, --password-file <file>
                         read the password from the first line of <file>

The config file uses an INI format with a [jabrss] section and the keys
jid, host, password, resource and user_agent (the latter overrides the
HTTP User-Agent used when polling feeds).'''


CONFIG_OPTIONS = ('jid', 'host', 'password', 'resource')
CONFIG_REQUIRED = ('jid', 'password')


def _config_path(opts):
    return opts.get('-c', opts.get('--config',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jabrss.conf')))


def _load_config(config_path):
    config = configparser.ConfigParser(interpolation=None)
    if os.path.exists(config_path):
        try:
            config.read(config_path)
        except configparser.Error as exc:
            print('JabRSS: cannot read config %s: %s' % (config_path, exc),
                  file=sys.stderr)
            sys.exit(2)
    return config


def _config_value(config, name):
    if not config.has_section('jabrss'):
        return None
    if not config.has_option('jabrss', name):
        return None
    value = config.get('jabrss', name)
    if value is None or value.strip() == '':
        return None
    return value.strip()


def _save_config(config, config_path, values):
    if not config.has_section('jabrss'):
        config.add_section('jabrss')
    for name, value in values.items():
        if value is not None:
            config.set('jabrss', name, value)
    try:
        with open(config_path, 'w') as fd:
            config.write(fd)
        os.chmod(config_path, 0o600)
    except OSError as exc:
        print('JabRSS: cannot write config %s: %s' % (config_path, exc),
              file=sys.stderr)
        sys.exit(2)


# resolve the full configuration from the command line and the config
# file (command line takes precedence); missing required options are
# requested interactively when running on a terminal and saved back to
# the config file. Returns a dict with the keys jid, host, port,
# password and user_agent.
def read_config(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    opts, args = getopt.getopt(argv, 'c:f:h:p:j:r:',
                               ['config=', 'password-file=', 'password=',
                                'jid=', 'connect-host=', 'resource=', 'help'])
    startup_opts = dict(opts)

    if '--help' in startup_opts:
        print(TEXT_USAGE)
        sys.exit(0)

    cli_values = {}
    for optname, optval in opts:
        if optname in ('-f', '--password-file'):
            fd = open(optval, 'r')
            cli_values['password'] = fd.readline().strip()
            fd.close()
        elif optname in ('-h', '--connect-host'):
            cli_values['host'] = optval
        elif optname in ('-p', '--password'):
            cli_values['password'] = optval
        elif optname in ('-j', '--jid'):
            cli_values['jid'] = optval
        elif optname in ('-r', '--resource'):
            cli_values['resource'] = optval

    config_file = _config_path(startup_opts)
    config = _load_config(config_file)

    # command line overrides the config file; anything still missing is
    # requested interactively and saved back to the config file
    values = {}
    for name in CONFIG_OPTIONS:
        values[name] = cli_values.get(name, _config_value(config, name))

    if any(values[name] is None for name in CONFIG_REQUIRED):
        prompted = False
        if sys.stdin.isatty():
            for name in CONFIG_REQUIRED:
                if values[name] is None:
                    if name == 'password':
                        value = getpass('Password: ')
                    else:
                        value = input('JabRSS JID: ')
                    if value:
                        values[name] = value.strip()
                        prompted = True
        if prompted:
            _save_config(config, config_file, values)
            # re-read what was just saved so stored values match exactly
            config = _load_config(config_file)
            for name in CONFIG_OPTIONS:
                values[name] = _config_value(config, name)

        missing = [name for name in CONFIG_REQUIRED if values[name] is None]
        if missing:
            print('JabRSS: missing option(s): %s; provide them via the command '
                  'line or in %s' % (', '.join(missing), config_file),
                  file=sys.stderr)
            sys.exit(2)

    connect_jid = JID(values['jid'])
    password = values['password']

    # the connect host is optional and may include a port, e.g.
    # "linuxoid.in:5222"; the port is split off here so that a literal
    # "host:port" string is not passed to the DNS resolution later on
    host = None
    port = 5222
    if values['host'] is not None:
        host_part, separator, port_part = values['host'].rpartition(':')
        if separator and port_part.isdigit():
            host = host_part
            port = int(port_part)
        else:
            host = values['host']

    # the resource is optional: the command line / config file take
    # precedence, then the resource part of the JID, and finally we fall
    # back to the hostname
    resource = values['resource']
    if resource is None:
        resource = connect_jid.resource
    if resource is None or resource == '':
        resource = socket.gethostname()
    jid = JID(connect_jid.bare + '/' + resource)

    return {'jid': jid, 'host': host, 'port': port, 'password': password,
            'user_agent': _config_value(config, 'user_agent')}
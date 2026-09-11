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


import os, sqlite3, sys

import jabrssng_context as ctx


def get_db():
    db = sqlite3.connect('jabrss.db', timeout=60000)
    db.isolation_level = None
    db.cursor().execute('PRAGMA synchronous=NORMAL')

    return db


def ensure_databases():
    # create the databases from the schema (db.sqlite) if they don't
    # exist yet
    if os.path.exists('jabrss.db') and os.path.exists('jabrss_res.db'):
        pass
    else:
        schema_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'db.sqlite')
        if not os.path.exists(schema_file):
            print('JabRSS: %s not found and jabrss.db/jabrss_res.db are '
                  'missing; cannot initialize the databases' % (schema_file,),
                  file=sys.stderr)
            sys.exit(2)
        try:
            with open(schema_file, 'r') as fd:
                schema = fd.read()
        except OSError as exc:
            print('JabRSS: cannot read %s: %s' % (schema_file, exc),
                  file=sys.stderr)
            sys.exit(2)
        db = sqlite3.connect('jabrss.db', timeout=60000)
        try:
            db.executescript(schema)
        except sqlite3.Error as exc:
            print('JabRSS: cannot initialize the databases: %s' % (exc,),
                  file=sys.stderr)
            sys.exit(2)
        finally:
            db.close()
    _ensure_pending_removal_column()


def _ensure_pending_removal_column():
    # migrate an existing jabrss.db to include the pending_removal column
    # (used to defer the removal of users until the subscription state
    # has persisted for a grace period, see REMOVAL_GRACE_SECS)
    db = sqlite3.connect('jabrss.db', timeout=60000)
    try:
        db.execute('ALTER TABLE user ADD COLUMN pending_removal INTEGER')
        db.commit()
    except sqlite3.Error:
        pass  # column already exists (or table unusable)
    finally:
        db.close()


class FlexibleLocker:
    def __init__(self, lock, active = True):
        self._lock, self._active = lock, active
        self._locked = False

    def __enter__(self):
        self.lock()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.unlock()

    def locked(self):
        if not self._locked and self._active and self._lock != None:
            self._locked = True

    def lock(self):
        if not self._locked and self._active and self._lock != None:
            self._lock.acquire()
            self._locked = True

    def unlock(self):
        if self._locked and self._active and self._lock != None:
            self._lock.release()
            self._locked = False

    def replace(self, lock):
        self.unlock()
        self._lock = lock
        self.lock()


class Cursor:
    def __init__(self, dbconn, parent = None):
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

    def begin(self):
        if not self._locked:
            ctx.db_sync.acquire()
            self._locked = True

        if not self._txn:
            self._cursor.execute('BEGIN')
            self._txn = True

    def commit(self):
        try:
            if self._txn:
                self._cursor.execute('COMMIT')
                self._txn = False
        finally:
            if self._locked:
                ctx.db_sync.release()
                self._locked = False

    def execute(self, stmt, bindings=None):
        self.begin()

        if bindings == None:
            return self._cursor.execute(stmt)
        else:
            return self._cursor.execute(stmt, bindings)

    def fetchone(self):
        return self._cursor.fetchone()

    def __getattr__(self, name):
        if name == 'lastrowid':
            return self._cursor.lastrowid
        elif name == 'rowcount':
            return self._cursor.rowcount

        raise AttributeError('object has no attribute \'%s\'' % (name,))
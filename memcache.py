#!/usr/bin/env python

"""

Usage summary
=============

This should give you a feel for how this module operates::

    import memcache
    mc = memcache.Client(['127.0.0.1:11211'], debug=0)

    mc.set("some_key", "Some value")
    value = mc.get("some_key")

    mc.set("another_key", 3)
    mc.delete("another_key")

    mc.set("key", "1") # note that the key used for incr/decr must be
                       # a string.
    mc.incr("key")
    mc.decr("key")

The standard way to use memcache with a database is like this:

    key = derive_key(obj)
    obj = mc.get(key)
    if not obj:
        obj = backend_api.get(...)
        mc.set(key, obj)

    # we now have obj, and future passes through this code
    # will use the object from the cache.

"""

import binascii
import pickle
import re
import socket
import sys
import threading
import time
import zlib

from io import BytesIO


def cmemcache_hash(key):
    return (((binascii.crc32(key) & 0xffffffff) >> 16) & 0x7fff) or 1
serverHashFunction = cmemcache_hash


def useOldServerHashFunction():
    """Use the old python-memcache server hash function."""
    global serverHashFunction
    serverHashFunction = binascii.crc32


valid_key_chars_re = re.compile(b'[\x21-\x7e\x80-\xff]+$')


#  Original author: Evan Martin of Danga Interactive
__author__ = "Sean Reifschneider <jafo-memcached@tummy.com>"
__version__ = "1.58"
__copyright__ = "Copyright (C) 2003 Danga Interactive"
#  http://en.wikipedia.org/wiki/Python_Software_Foundation_License
__license__ = "Python Software Foundation License"

SERVER_MAX_KEY_LENGTH = 250
# Storing values larger than 1MB requires starting memcached with -I <size> for
# memcached >= 1.4.2 or recompiling for < 1.4.2. If you do, this value can be
# changed by doing "memcache.SERVER_MAX_VALUE_LENGTH = N" after importing this
# module.
SERVER_MAX_VALUE_LENGTH = 1024 * 1024


class _Error(Exception):
    pass


class _ConnectionDeadError(Exception):
    pass


_DEAD_RETRY = 30  # number of seconds before retrying a dead server.
_SOCKET_TIMEOUT = 3  # number of seconds before sockets timeout.


class Client(threading.local):

    _FLAG_PICKLE = 1 << 0
    _FLAG_INTEGER = 1 << 1
    _FLAG_LONG = 1 << 2
    _FLAG_COMPRESSED = 1 << 3

    _SERVER_RETRIES = 10  # how many times to try finding a free server.

    # exceptions for Client
    class MemcachedKeyError(Exception):
        pass

    class MemcachedKeyLengthError(MemcachedKeyError):
        pass

    class MemcachedKeyCharacterError(MemcachedKeyError):
        pass

    class MemcachedKeyNoneError(MemcachedKeyError):
        pass

    class MemcachedKeyTypeError(MemcachedKeyError):
        pass

    class MemcachedStringEncodingError(Exception):
        pass

    def __init__(self, servers, debug=0, pickleProtocol=0,
                 pickler=pickle.Pickler, unpickler=pickle.Unpickler,
                 compressor=zlib.compress, decompressor=zlib.decompress,
                 pload=None, pid=None,
                 server_max_key_length=None, server_max_value_length=None,
                 dead_retry=_DEAD_RETRY, socket_timeout=_SOCKET_TIMEOUT,
                 cache_cas=False, flush_on_reconnect=0, check_keys=True):

        super(Client, self).__init__()
        self.debug = debug
        self.dead_retry = dead_retry
        self.socket_timeout = socket_timeout
        self.flush_on_reconnect = flush_on_reconnect
        self.set_servers(servers)
        self.stats = {}
        self.cache_cas = cache_cas
        self.reset_cas()
        self.do_check_key = check_keys

        # Allow users to modify pickling/unpickling behavior
        self.pickleProtocol = pickleProtocol
        self.pickler = pickler
        self.unpickler = unpickler
        self.compressor = compressor
        self.decompressor = decompressor
        self.persistent_load = pload
        self.persistent_id = pid
        self.server_max_key_length = server_max_key_length
        if self.server_max_key_length is None:
            self.server_max_key_length = SERVER_MAX_KEY_LENGTH
        self.server_max_value_length = server_max_value_length
        if self.server_max_value_length is None:
            self.server_max_value_length = SERVER_MAX_VALUE_LENGTH

        #  figure out the pickler style
        file = BytesIO()
        try:
            pickler = self.pickler(file, protocol=self.pickleProtocol)
            self.picklerIsKeyword = True
        except TypeError:
            self.picklerIsKeyword = False

    def _encode_key(self, key):
        if isinstance(key, tuple):
            if isinstance(key[1], six.text_type):
                return (key[0], key[1].encode('utf8'))
        elif isinstance(key, six.text_type):
            return key.encode('utf8')
        return key

    def _encode_cmd(self, cmd, key, headers, noreply, *args):
        cmd_bytes = cmd.encode('utf-8') if six.PY3 else cmd
        fullcmd = [cmd_bytes, b' ', key]

        if headers:
            if six.PY3:
                headers = headers.encode('utf-8')
            fullcmd.append(b' ')
            fullcmd.append(headers)

        if noreply:
            fullcmd.append(b' noreply')

        if args:
            fullcmd.append(b' ')
            fullcmd.extend(args)
        return b''.join(fullcmd)

    def reset_cas(self):

        self.cas_ids = {}

    def set_servers(self, servers):

        self.servers = [_Host(s, self.debug, dead_retry=self.dead_retry,
                              socket_timeout=self.socket_timeout,
                              flush_on_reconnect=self.flush_on_reconnect)
                        for s in servers]
        self._init_buckets()

    def get_stats(self, stat_args=None):

        data = []
        for s in self.servers:
            if not s.connect():
                continue
            if s.family == socket.AF_INET:
                name = '%s:%s (%s)' % (s.ip, s.port, s.weight)
            elif s.family == socket.AF_INET6:
                name = '[%s]:%s (%s)' % (s.ip, s.port, s.weight)
            else:
                name = 'unix:%s (%s)' % (s.address, s.weight)
            if not stat_args:
                s.send_cmd('stats')
            else:
                s.send_cmd('stats ' + stat_args)
            serverData = {}
            data.append((name, serverData))
            readline = s.readline
            while 1:
                line = readline()
                if not line or line.decode('ascii').strip() == 'END':
                    break
                stats = line.decode('ascii').split(' ', 2)
                serverData[stats[1]] = stats[2]

        return data

    def get_slab_stats(self):
        data = []
        for s in self.servers:
            if not s.connect():
                continue
            if s.family == socket.AF_INET:
                name = '%s:%s (%s)' % (s.ip, s.port, s.weight)
            elif s.family == socket.AF_INET6:
                name = '[%s]:%s (%s)' % (s.ip, s.port, s.weight)
            else:
                name = 'unix:%s (%s)' % (s.address, s.weight)
            serverData = {}
            data.append((name, serverData))
            s.send_cmd('stats slabs')
            readline = s.readline
            while 1:
                line = readline()
                if not line or line.strip() == 'END':
                    break
                item = line.split(' ', 2)
                if line.startswith('STAT active_slabs') or line.startswith('STAT total_malloced'):
                    serverData[item[1]] = item[2]
                else:
                    # 0 = STAT, 1 = ITEM, 2 = Value
                    slab = item[1].split(':', 2)
                    # 0 = Slab #, 1 = Name
                    if slab[0] not in serverData:
                        serverData[slab[0]] = {}
                    serverData[slab[0]][slab[1]] = item[2]
        return data

    def get_slabs(self):
        data = []
        for s in self.servers:
            if not s.connect():
                continue
            if s.family == socket.AF_INET:
                name = '%s:%s (%s)' % (s.ip, s.port, s.weight)
            elif s.family == socket.AF_INET6:
                name = '[%s]:%s (%s)' % (s.ip, s.port, s.weight)
            else:
                name = 'unix:%s (%s)' % (s.address, s.weight)
            serverData = {}
            data.append((name, serverData))
            s.send_cmd('stats items')
            readline = s.readline
            while 1:
                line = readline()
                if not line or line.strip() == 'END':
                    break
                item = line.split(' ', 2)
                # 0 = STAT, 1 = ITEM, 2 = Value
                slab = item[1].split(':', 2)
                # 0 = items, 1 = Slab #, 2 = Name
                if slab[1] not in serverData:
                    serverData[slab[1]] = {}
                serverData[slab[1]][slab[2]] = item[2]
        return data

    def flush_all(self):
        """Expire all data in memcache servers that are reachable."""
        for s in self.servers:
            if not s.connect():
                continue
            s.flush()

    def debuglog(self, str):
        if self.debug:
            sys.stderr.write("MemCached: %s\n" % str)

    def _statlog(self, func):
        if func not in self.stats:
            self.stats[func] = 1
        else:
            self.stats[func] += 1

    def forget_dead_hosts(self):
        """Reset every host in the pool to an "alive" state."""
        for s in self.servers:
            s.deaduntil = 0

    def _init_buckets(self):
        self.buckets = []
        for server in self.servers:
            for i in range(server.weight):
                self.buckets.append(server)

    def _get_server(self, key):
        if isinstance(key, tuple):
            serverhash, key = key
        else:
            serverhash = serverHashFunction(key)

        if not self.buckets:
            return None, None

        for i in range(Client._SERVER_RETRIES):
            server = self.buckets[serverhash % len(self.buckets)]
            if server.connect():
                # print("(using server %s)" % server,)
                return server, key
            serverhash = str(serverhash) + str(i)
            if isinstance(serverhash, six.text_type):
                serverhash = serverhash.encode('ascii')
            serverhash = serverHashFunction(serverhash)
        return None, None

    def disconnect_all(self):
        for s in self.servers:
            s.close_socket()

    def delete_multi(self, keys, time=None, key_prefix='', noreply=False):

        self._statlog('delete_multi')

        server_keys, prefixed_to_orig_key = self._map_and_prefix_keys(
            keys, key_prefix)

        # send out all requests on each server before reading anything
        dead_servers = []

        rc = 1
        for server in six.iterkeys(server_keys):
            bigcmd = []
            write = bigcmd.append
            if time is not None:
                headers = str(time)
            else:
                headers = None
            for key in server_keys[server]:  # These are mangled keys
                cmd = self._encode_cmd('delete', key, headers, noreply, b'\r\n')
                write(cmd)
            try:
                server.send_cmds(b''.join(bigcmd))
            except socket.error as msg:
                rc = 0
                if isinstance(msg, tuple):
                    msg = msg[1]
                server.mark_dead(msg)
                dead_servers.append(server)

        # if noreply, just return
        if noreply:
            return rc

        # if any servers died on the way, don't expect them to respond.
        for server in dead_servers:
            del server_keys[server]

        for server, keys in six.iteritems(server_keys):
            try:
                for key in keys:
                    server.expect(b"DELETED")
            except socket.error as msg:
                if isinstance(msg, tuple):
                    msg = msg[1]
                server.mark_dead(msg)
                rc = 0
        return rc

    def delete(self, key, time=None, noreply=False):

        return self._deletetouch([b'DELETED', b'NOT_FOUND'], "delete", key,
                                 time, noreply)

    def touch(self, key, time=0, noreply=False):

        return self._deletetouch([b'TOUCHED'], "touch", key, time, noreply)

    def _deletetouch(self, expected, cmd, key, time=0, noreply=False):
        key = self._encode_key(key)
        if self.do_check_key:
            self.check_key(key)
        server, key = self._get_server(key)
        if not server:
            return 0
        self._statlog(cmd)
        if time is not None and time != 0:
            headers = str(time)
        else:
            headers = None
        fullcmd = self._encode_cmd(cmd, key, headers, noreply)

        try:
            server.send_cmd(fullcmd)
            if noreply:
                return 1
            line = server.readline()
            if line and line.strip() in expected:
                return 1
            self.debuglog('%s expected %s, got: %r'
                          % (cmd, ' or '.join(expected), line))
        except socket.error as msg:
            if isinstance(msg, tuple):
                msg = msg[1]
            server.mark_dead(msg)
        return 0

    def incr(self, key, delta=1, noreply=False):

        return self._incrdecr("incr", key, delta, noreply)

    def decr(self, key, delta=1, noreply=False):

        return self._incrdecr("decr", key, delta, noreply)

    def _incrdecr(self, cmd, key, delta, noreply=False):
        key = self._encode_key(key)
        if self.do_check_key:
            self.check_key(key)
        server, key = self._get_server(key)
        if not server:
            return None
        self._statlog(cmd)
        fullcmd = self._encode_cmd(cmd, key, str(delta), noreply)
        try:
            server.send_cmd(fullcmd)
            if noreply:
                return
            line = server.readline()
            if line is None or line.strip() == b'NOT_FOUND':
                return None
            return int(line)
        except socket.error as msg:
            if isinstance(msg, tuple):
                msg = msg[1]
            server.mark_dead(msg)
            return None

    def add(self, key, val, time=0, min_compress_len=0, noreply=False):
        '''Add new key with value.

        Like L{set}, but only stores in memcache if the key doesn't
        already exist.

        @return: Nonzero on success.
        @rtype: int
        '''
        return self._set("add", key, val, time, min_compress_len, noreply)

    def append(self, key, val, time=0, min_compress_len=0, noreply=False):
        '''Append the value to the end of the existing key's value.

        Only stores in memcache if key already exists.
        Also see L{prepend}.

        @return: Nonzero on success.
        @rtype: int
        '''
        return self._set("append", key, val, time, min_compress_len, noreply)

    def prepend(self, key, val, time=0, min_compress_len=0, noreply=False):
        '''Prepend the value to the beginning of the existing key's value.

        Only stores in memcache if key already exists.
        Also see L{append}.

        @return: Nonzero on success.
        @rtype: int
        '''
        return self._set("prepend", key, val, time, min_compress_len, noreply)

    def replace(self, key, val, time=0, min_compress_len=0, noreply=False):
        return self._set("replace", key, val, time, min_compress_len, noreply)

    def set(self, key, val, time=0, min_compress_len=0, noreply=False):
        return self._set("set", key, val, time, min_compress_len, noreply)

    def cas(self, key, val, time=0, min_compress_len=0, noreply=False):

        return self._set("cas", key, val, time, min_compress_len, noreply)

    def _map_and_prefix_keys(self, key_iterable, key_prefix):
        """Map keys to the servers they will reside on.

        Compute the mapping of server (_Host instance) -> list of keys to
        stuff onto that server, as well as the mapping of prefixed key
        -> original key.
        """
        key_prefix = self._encode_key(key_prefix)
        # Check it just once ...
        key_extra_len = len(key_prefix)
        if key_prefix and self.do_check_key:
            self.check_key(key_prefix)

        # server (_Host) -> list of unprefixed server keys in mapping
        server_keys = {}

        prefixed_to_orig_key = {}
        # build up a list for each server of all the keys we want.
        for orig_key in key_iterable:
            if isinstance(orig_key, tuple):
                # Tuple of hashvalue, key ala _get_server(). Caller is
                # essentially telling us what server to stuff this on.
                # Ensure call to _get_server gets a Tuple as well.
                serverhash, key = orig_key

                key = self._encode_key(key)
                if not isinstance(key, six.binary_type):
                    # set_multi supports int / long keys.
                    key = str(key)
                    if six.PY3:
                        key = key.encode('utf8')
                bytes_orig_key = key

                # Gotta pre-mangle key before hashing to a
                # server. Returns the mangled key.
                server, key = self._get_server(
                    (serverhash, key_prefix + key))

                orig_key = orig_key[1]
            else:
                key = self._encode_key(orig_key)
                if not isinstance(key, six.binary_type):
                    # set_multi supports int / long keys.
                    key = str(key)
                    if six.PY3:
                        key = key.encode('utf8')
                bytes_orig_key = key
                server, key = self._get_server(key_prefix + key)

            #  alert when passed in key is None
            if orig_key is None:
                self.check_key(orig_key, key_extra_len=key_extra_len)

            # Now check to make sure key length is proper ...
            if self.do_check_key:
                self.check_key(bytes_orig_key, key_extra_len=key_extra_len)

            if not server:
                continue

            if server not in server_keys:
                server_keys[server] = []
            server_keys[server].append(key)
            prefixed_to_orig_key[key] = orig_key

        return (server_keys, prefixed_to_orig_key)

    def set_multi(self, mapping, time=0, key_prefix='', min_compress_len=0,
                  noreply=False):
        self._statlog('set_multi')

        server_keys, prefixed_to_orig_key = self._map_and_prefix_keys(
            six.iterkeys(mapping), key_prefix)

        # send out all requests on each server before reading anything
        dead_servers = []
        notstored = []  # original keys.

        for server in six.iterkeys(server_keys):
            bigcmd = []
            write = bigcmd.append
            try:
                for key in server_keys[server]:  # These are mangled keys
                    store_info = self._val_to_store_info(
                        mapping[prefixed_to_orig_key[key]],
                        min_compress_len)
                    if store_info:
                        flags, len_val, val = store_info
                        headers = "%d %d %d" % (flags, time, len_val)
                        fullcmd = self._encode_cmd('set', key, headers,
                                                   noreply,
                                                   b'\r\n', val, b'\r\n')
                        write(fullcmd)
                    else:
                        notstored.append(prefixed_to_orig_key[key])
                server.send_cmds(b''.join(bigcmd))
            except socket.error as msg:
                if isinstance(msg, tuple):
                    msg = msg[1]
                server.mark_dead(msg)
                dead_servers.append(server)

        # if noreply, just return early
        if noreply:
            return notstored

        # if any servers died on the way, don't expect them to respond.
        for server in dead_servers:
            del server_keys[server]

        #  short-circuit if there are no servers, just return all keys
        if not server_keys:
            return list(mapping.keys())

        for server, keys in six.iteritems(server_keys):
            try:
                for key in keys:
                    if server.readline() == b'STORED':
                        continue
                    else:
                        # un-mangle.
                        notstored.append(prefixed_to_orig_key[key])
            except (_Error, socket.error) as msg:
                if isinstance(msg, tuple):
                    msg = msg[1]
                server.mark_dead(msg)
        return notstored

    def _val_to_store_info(self, val, min_compress_len):
        """Transform val to a storable representation.

        Returns a tuple of the flags, the length of the new value, and
        the new value itself.
        """
        flags = 0
        if isinstance(val, six.binary_type):
            pass
        elif isinstance(val, six.text_type):
            val = val.encode('utf-8')
        elif isinstance(val, int):
            flags |= Client._FLAG_INTEGER
            val = '%d' % val
            if six.PY3:
                val = val.encode('ascii')
            # force no attempt to compress this silly string.
            min_compress_len = 0
        elif six.PY2 and isinstance(val, long):  # noqa: F821
            flags |= Client._FLAG_LONG
            val = str(val)
            if six.PY3:
                val = val.encode('ascii')
            # force no attempt to compress this silly string.
            min_compress_len = 0
        else:
            flags |= Client._FLAG_PICKLE
            file = BytesIO()
            if self.picklerIsKeyword:
                pickler = self.pickler(file, protocol=self.pickleProtocol)
            else:
                pickler = self.pickler(file, self.pickleProtocol)
            if self.persistent_id:
                pickler.persistent_id = self.persistent_id
            pickler.dump(val)
            val = file.getvalue()

        lv = len(val)
        # We should try to compress if min_compress_len > 0
        # and this string is longer than our min threshold.
        if min_compress_len and lv > min_compress_len:
            comp_val = self.compressor(val)
            # Only retain the result if the compression result is smaller
            # than the original.
            if len(comp_val) < lv:
                flags |= Client._FLAG_COMPRESSED
                val = comp_val

        #  silently do not store if value length exceeds maximum
        if (self.server_max_value_length != 0 and
                len(val) > self.server_max_value_length):
            return 0

        return (flags, len(val), val)

    def _set(self, cmd, key, val, time, min_compress_len=0, noreply=False):
        key = self._encode_key(key)
        if self.do_check_key:
            self.check_key(key)
        server, key = self._get_server(key)
        if not server:
            return 0

        def _unsafe_set():
            self._statlog(cmd)

            if cmd == 'cas' and key not in self.cas_ids:
                return self._set('set', key, val, time, min_compress_len,
                                 noreply)

            store_info = self._val_to_store_info(val, min_compress_len)
            if not store_info:
                return 0
            flags, len_val, encoded_val = store_info

            if cmd == 'cas':
                headers = ("%d %d %d %d"
                           % (flags, time, len_val, self.cas_ids[key]))
            else:
                headers = "%d %d %d" % (flags, time, len_val)
            fullcmd = self._encode_cmd(cmd, key, headers, noreply,
                                       b'\r\n', encoded_val)

            try:
                server.send_cmd(fullcmd)
                if noreply:
                    return True
                return server.expect(b"STORED", raise_exception=True) == b"STORED"
            except socket.error as msg:
                if isinstance(msg, tuple):
                    msg = msg[1]
                server.mark_dead(msg)
            return 0

        try:
            return _unsafe_set()
        except _ConnectionDeadError:
            # retry once
            try:
                if server._get_socket():
                    return _unsafe_set()
            except (_ConnectionDeadError, socket.error) as msg:
                server.mark_dead(msg)
            return 0

    def _get(self, cmd, key):
        key = self._encode_key(key)
        if self.do_check_key:
            self.check_key(key)
        server, key = self._get_server(key)
        if not server:
            return None

        def _unsafe_get():
            self._statlog(cmd)

            try:
                cmd_bytes = cmd.encode('utf-8') if six.PY3 else cmd
                fullcmd = b''.join((cmd_bytes, b' ', key))
                server.send_cmd(fullcmd)
                rkey = flags = rlen = cas_id = None

                if cmd == 'gets':
                    rkey, flags, rlen, cas_id, = self._expect_cas_value(
                        server, raise_exception=True
                    )
                    if rkey and self.cache_cas:
                        self.cas_ids[rkey] = cas_id
                else:
                    rkey, flags, rlen, = self._expectvalue(
                        server, raise_exception=True
                    )

                if not rkey:
                    return None
                try:
                    value = self._recv_value(server, flags, rlen)
                finally:
                    server.expect(b"END", raise_exception=True)
            except (_Error, socket.error) as msg:
                if isinstance(msg, tuple):
                    msg = msg[1]
                server.mark_dead(msg)
                return None

            return value

        try:
            return _unsafe_get()
        except _ConnectionDeadError:
            # retry once
            try:
                if server.connect():
                    return _unsafe_get()
                return None
            except (_ConnectionDeadError, socket.error) as msg:
                server.mark_dead(msg)
            return None

    def get(self, key):
        '''Retrieves a key from the memcache.

        @return: The value or None.
        '''
        return self._get('get', key)

    def gets(self, key):
        '''Retrieves a key from the memcache. Used in conjunction with 'cas'.

        @return: The value or None.
        '''
        return self._get('gets', key)

    def get_multi(self, keys, key_prefix=''):

        self._statlog('get_multi')

        server_keys, prefixed_to_orig_key = self._map_and_prefix_keys(
            keys, key_prefix)

        # send out all requests on each server before reading anything
        dead_servers = []
        for server in six.iterkeys(server_keys):
            try:
                fullcmd = b"get " + b" ".join(server_keys[server])
                server.send_cmd(fullcmd)
            except socket.error as msg:
                if isinstance(msg, tuple):
                    msg = msg[1]
                server.mark_dead(msg)
                dead_servers.append(server)

        # if any servers died on the way, don't expect them to respond.
        for server in dead_servers:
            del server_keys[server]

        retvals = {}
        for server in six.iterkeys(server_keys):
            try:
                line = server.readline()
                while line and line != b'END':
                    rkey, flags, rlen = self._expectvalue(server, line)
                    #  Bo Yang reports that this can sometimes be None
                    if rkey is not None:
                        val = self._recv_value(server, flags, rlen)
                        # un-prefix returned key.
                        retvals[prefixed_to_orig_key[rkey]] = val
                    line = server.readline()
            except (_Error, socket.error) as msg:
                if isinstance(msg, tuple):
                    msg = msg[1]
                server.mark_dead(msg)
        return retvals

    def _expect_cas_value(self, server, line=None, raise_exception=False):
        if not line:
            line = server.readline(raise_exception)

        if line and line[:5] == b'VALUE':
            resp, rkey, flags, len, cas_id = line.split()
            return (rkey, int(flags), int(len), int(cas_id))
        else:
            return (None, None, None, None)

    def _expectvalue(self, server, line=None, raise_exception=False):
        if not line:
            line = server.readline(raise_exception)

        if line and line[:5] == b'VALUE':
            resp, rkey, flags, len = line.split()
            flags = int(flags)
            rlen = int(len)
            return (rkey, flags, rlen)
        else:
            return (None, None, None)

    def _recv_value(self, server, flags, rlen):
        rlen += 2  # include \r\n
        buf = server.recv(rlen)
        if len(buf) != rlen:
            raise _Error("received %d bytes when expecting %d"
                         % (len(buf), rlen))

        if len(buf) == rlen:
            buf = buf[:-2]  # strip \r\n

        if flags & Client._FLAG_COMPRESSED:
            buf = self.decompressor(buf)
            flags &= ~Client._FLAG_COMPRESSED

        if flags == 0:
            # Bare string
            if six.PY3:
                val = buf.decode('utf8')
            else:
                val = buf
        elif flags & Client._FLAG_INTEGER:
            val = int(buf)
        # elif flags & Client._FLAG_LONG:
        #     if six.PY3:
        #         val = int(buf)
        #     else:
        #         val = long(buf)  # noqa: F821
        elif flags & Client._FLAG_PICKLE:
            try:
                file = BytesIO(buf)
                unpickler = self.unpickler(file)
                if self.persistent_load:
                    unpickler.persistent_load = self.persistent_load
                val = unpickler.load()
            except Exception as e:
                self.debuglog('Pickle error: %s\n' % e)
                return None
        else:
            self.debuglog("unknown flags on get: %x\n" % flags)
            raise ValueError('Unknown flags on get: %x' % flags)

        return val

    def check_key(self, key, key_extra_len=0):
        """Checks sanity of key.

            Fails if:

            Key length is > SERVER_MAX_KEY_LENGTH (Raises MemcachedKeyLength).
            Contains control characters  (Raises MemcachedKeyCharacterError).
            Is not a string (Raises MemcachedStringEncodingError)
            Is an unicode string (Raises MemcachedStringEncodingError)
            Is not a string (Raises MemcachedKeyError)
            Is None (Raises MemcachedKeyError)
        """
        if isinstance(key, tuple):
            key = key[1]
        if key is None:
            raise Client.MemcachedKeyNoneError("Key is None")
        if key is '':
            if key_extra_len is 0:
                raise Client.MemcachedKeyNoneError("Key is empty")

            #  key is empty but there is some other component to key
            return

        if not isinstance(key, six.binary_type):
            raise Client.MemcachedKeyTypeError("Key must be a binary string")

        if (self.server_max_key_length != 0 and
                len(key) + key_extra_len > self.server_max_key_length):
            raise Client.MemcachedKeyLengthError(
                "Key length is > %s" % self.server_max_key_length
            )
        if not valid_key_chars_re.match(key):
            raise Client.MemcachedKeyCharacterError(
                "Control/space characters not allowed (key=%r)" % key)


class _Host(object):

    def __init__(self, host, debug=0, dead_retry=_DEAD_RETRY,
                 socket_timeout=_SOCKET_TIMEOUT, flush_on_reconnect=0):
        self.dead_retry = dead_retry
        self.socket_timeout = socket_timeout
        self.debug = debug
        self.flush_on_reconnect = flush_on_reconnect
        if isinstance(host, tuple):
            host, self.weight = host
        else:
            self.weight = 1

        #  parse the connection string
        m = re.match(r'^(?P<proto>unix):(?P<path>.*)$', host)
        if not m:
            m = re.match(r'^(?P<proto>inet6):'
                         r'\[(?P<host>[^\[\]]+)\](:(?P<port>[0-9]+))?$', host)
        if not m:
            m = re.match(r'^(?P<proto>inet):'
                         r'(?P<host>[^:]+)(:(?P<port>[0-9]+))?$', host)
        if not m:
            m = re.match(r'^(?P<host>[^:]+)(:(?P<port>[0-9]+))?$', host)
        if not m:
            raise ValueError('Unable to parse connection string: "%s"' % host)

        hostData = m.groupdict()
        if hostData.get('proto') == 'unix':
            self.family = socket.AF_UNIX
            self.address = hostData['path']
        elif hostData.get('proto') == 'inet6':
            self.family = socket.AF_INET6
            self.ip = hostData['host']
            self.port = int(hostData.get('port') or 11211)
            self.address = (self.ip, self.port)
        else:
            self.family = socket.AF_INET
            self.ip = hostData['host']
            self.port = int(hostData.get('port') or 11211)
            self.address = (self.ip, self.port)

        self.deaduntil = 0
        self.socket = None
        self.flush_on_next_connect = 0

        self.buffer = b''

    def debuglog(self, str):
        if self.debug:
            sys.stderr.write("MemCached: %s\n" % str)

    def _check_dead(self):
        if self.deaduntil and self.deaduntil > time.time():
            return 1
        self.deaduntil = 0
        return 0

    def connect(self):
        if self._get_socket():
            return 1
        return 0

    def mark_dead(self, reason):
        self.debuglog("MemCache: %s: %s.  Marking dead." % (self, reason))
        self.deaduntil = time.time() + self.dead_retry
        if self.flush_on_reconnect:
            self.flush_on_next_connect = 1
        self.close_socket()

    def _get_socket(self):
        if self._check_dead():
            return None
        if self.socket:
            return self.socket
        s = socket.socket(self.family, socket.SOCK_STREAM)
        if hasattr(s, 'settimeout'):
            s.settimeout(self.socket_timeout)
        try:
            s.connect(self.address)
        except socket.timeout as msg:
            self.mark_dead("connect: %s" % msg)
            return None
        except socket.error as msg:
            if isinstance(msg, tuple):
                msg = msg[1]
            self.mark_dead("connect: %s" % msg)
            return None
        self.socket = s
        self.buffer = b''
        if self.flush_on_next_connect:
            self.flush()
            self.flush_on_next_connect = 0
        return s

    def close_socket(self):
        if self.socket:
            self.socket.close()
            self.socket = None

    def send_cmd(self, cmd):
        if isinstance(cmd, six.text_type):
            cmd = cmd.encode('utf8')
        self.socket.sendall(cmd + b'\r\n')

    def no_socket():
        return lambda buf_size: b''

    def send_cmds(self, cmds):
        """cmds already has trailing \r\n's applied."""
        if isinstance(cmds, six.text_type):
            cmds = cmds.encode('utf8')
        self.socket.sendall(cmds)

    def readline(self, raise_exception=False):
        """Read a line and return it.

        If "raise_exception" is set, raise _ConnectionDeadError if the
        read fails, otherwise return an empty string.
        """
        buf = self.buffer
        if self.socket:
            recv = self.socket.recv
        else:
            # recv = lambda bufsize: b''
            recv = self.no_socket()

        while True:
            index = buf.find(b'\r\n')
            if index >= 0:
                break
            data = recv(4096)
            if not data:
                # connection close, let's kill it and raise
                self.mark_dead('connection closed in readline()')
                if raise_exception:
                    raise _ConnectionDeadError()
                else:
                    return ''

            buf += data
        self.buffer = buf[index + 2:]
        return buf[:index]

    def expect(self, text, raise_exception=False):
        line = self.readline(raise_exception)
        if self.debug and line != text:
            if six.PY3:
                text = text.decode('utf8')
                log_line = line.decode('utf8', 'replace')
            else:
                log_line = line
            self.debuglog("while expecting %r, got unexpected response %r"
                          % (text, log_line))
        return line

    def recv(self, rlen):
        self_socket_recv = self.socket.recv
        buf = self.buffer
        while len(buf) < rlen:
            foo = self_socket_recv(max(rlen - len(buf), 4096))
            buf += foo
            if not foo:
                raise _Error('Read %d bytes, expecting %d, '
                             'read returned 0 length bytes' % (len(buf), rlen))
        self.buffer = buf[rlen:]
        return buf[:rlen]

    def flush(self):
        self.send_cmd('flush_all')
        self.expect(b'OK')

    def __str__(self):
        d = ''
        if self.deaduntil:
            d = " (dead until %d)" % self.deaduntil

        if self.family == socket.AF_INET:
            return "inet:%s:%d%s" % (self.address[0], self.address[1], d)
        elif self.family == socket.AF_INET6:
            return "inet6:[%s]:%d%s" % (self.address[0], self.address[1], d)
        else:
            return "unix:%s%s" % (self.address, d)


def _doctest():
    import doctest
    import memcache
    servers = ["127.0.0.1:11211"]
    mc = memcache.Client(servers, debug=1)
    globs = {"mc": mc}
    results = doctest.testmod(memcache, globs=globs)
    mc.disconnect_all()
    print("Doctests: %s" % (results,))
    if results.failed:
        sys.exit(1)

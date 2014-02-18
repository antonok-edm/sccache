# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import httplib
import os
import subprocess
import sys
import time
import urllib2
import which
from collections import OrderedDict


if 'SCCACHE_NAMESERVER' in os.environ:
    from dns.resolver import Resolver
    resolver = Resolver(configure=False)
    resolver.nameservers.append(os.environ['SCCACHE_NAMESERVER'])
    def dns_query(host):
        for rr in resolver.query(host):
            return rr.address
else:
    import socket
    def dns_query(host):
        for family, socktype, proto, canonname, sockaddr in \
                socket.getaddrinfo(host, 0):
            return sockaddr[0]


def WrapperFactory(parent_class):
    class WrapperConnection(parent_class):
        def connect(self):
            timer = Timer.current()
            timer.start('dns')
            self.host = dns_query(self.host)
            timer.start('conn')
            parent_class.connect(self)
            timer.stop()

        def send(self, data):
            if self.sock is None:
                self.connect()
            timer = Timer.current()
            timer.start('send')
            parent_class.send(self, data)
            timer.stop()

        def getresponse(self, buffering=False):
            timer = Timer.current()
            timer.start('resp')
            res = parent_class.getresponse(self, buffering)
            timer.stop()
            return res

    return WrapperConnection

SCCacheHTTPConnection = WrapperFactory(httplib.HTTPConnection)
SCCacheHTTPSConnection = WrapperFactory(httplib.HTTPSConnection)

class SCCacheHTTPHandler(urllib2.HTTPHandler):
    def http_open(self, req):
        return self.do_open(SCCacheHTTPConnection, req)

class SCCacheHTTPSHandler(urllib2.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(SCCacheHTTPSConnection, req)

urllib2.install_opener(
    urllib2.build_opener(SCCacheHTTPHandler, SCCacheHTTPSHandler))

FILE_TYPES = {
    '.c': 'cpp-output',
    '.cc': 'c++-cpp-output',
    '.cpp': 'c++-cpp-output',
    '.cxx': 'c++-cpp-outout',
}


class Timer(object):
    _stack = []

    @classmethod
    def current(cls):
        if cls._stack:
            return cls._stack[-1]
        return None

    def __init__(self):
        self._timings = OrderedDict()
        self._timers = {}
        self._current = None
        self._stop = None

    def start(self, name):
        now = time.time()
        self.stop(now)
        self._stop = None
        self._current = (name, now)
        Timer._stack.append(self)

    def start_group(self, name):
        self.start(name)
        timer = self._timers[name] = Timer()
        Timer._stack.append(timer)
        return timer

    def stop(self, when=None):
        end = when or time.time()
        if self._stop:
            duration = (end - self._stop) * 1000
            if duration >= 1:
                self._store('other', duration)
        if not self._current:
            return
        self._stop = end
        n, t = self._current
        duration = (end - t) * 1000
        self._store(n, duration)
        if n in self._timers:
            self._timers[n].stop(end)
        self._current = None
        Timer._stack.pop()

    def _store(self, name, duration):
        if name in self._timings:
            self._timings[name] += duration
        else:
            self._timings[name] = duration

    def __getitem__(self, name):
        return self._timings[name]

    def __str__(self):
        self.stop()

        def serialize(o):
            if isinstance(o, Timer):
                return '%.2f (%s)' % (
                    sum(o._timings.values()),
                    '; '.join('%s: %s' % (k, serialize(self._timers.get(k, v)))
                              for k, v in o._timings.items()),
                )
            else:
                return '%.2f' % o

        return serialize(self)


class Bucket(object):
    def __init__(self):
        self._name = os.environ.get('SCCACHE_BUCKET', '')
        self._status = 0

    def get(self, key, timer=None):
        self._status = -1
        if not self._name:
            return None
        try:
            data = urllib2.urlopen('http://%s.s3.amazonaws.com/%s' %
                (self._name, key)).read()
            self._status = 200
            return CacheData(data) if data else None
        except urllib2.HTTPError as e:
            self._status = e.code
            return None
        except:
            return None

    def put(self, key, data):
        self._status = -1
        if not self._name:
            raise Exception('No bucket name')
        from boto.s3.connection import S3Connection
        from boto.exception import S3ResponseError
        try:
            conn = S3Connection(
                https_connection_factory=(SCCacheHTTPSConnection, ()))
            bucket = conn.get_bucket(self._name, validate=False)
            k = bucket.new_key(key)
            k.set_contents_from_string(data, headers={
                'x-amz-acl': 'public-read',
            })
        except S3ResponseError as e:
            self._status = e.status
            raise

    @property
    def status(self):
        return self._status

    @property
    def name(self):
        return self._name


class CacheData(object):
    def __init__(self, data=None, obj=None):
        assert bool(data) != bool(obj)
        self._data = data
        self._obj = obj

    def dump(self, output):
        with open(output, 'wb') as out:
            if self._obj:
                out.write(self._obj)
            else:
                import gzip
                from cStringIO import StringIO
                import shutil
                with gzip.GzipFile(mode='r',
                        fileobj=StringIO(self._data)) as obj:
                    shutil.copyfileobj(obj, out)

    @property
    def data(self):
        if not self._data:
            import gzip
            from cStringIO import StringIO
            data = StringIO()
            with gzip.GzipFile(mode='w', compresslevel=6, fileobj=data) as fh:
                fh.write(self._obj)
            self._data = data.getvalue()

        return self._data

    @classmethod
    def from_object(self, path):
        with open(path, 'rb') as fh:
            return CacheData(obj=fh.read())


def parse_arguments(command):
    program, args = command[0], command[1:]
    if not os.path.exists(program):
        try:
            program = which.which(program)
        except which.whichError:
            return None

    reduced_args = []
    input = ()
    output = None
    target = None
    need_explicit_target = False
    compilation = False
    iter_args = iter(args)
    for arg in iter_args:
        if arg == '-c':
            compilation = True
        elif arg == '-o':
            output = iter_args.next()
        elif arg in ('--param', '-A', '-D', '-F', '-G', '-I', '-L', '-MF',
                     '-MQ', '-U', '-V', '-Xassembler', '-Xlinker',
                     '-Xpreprocessor', '-aux-info', '-b', '-idirafter',
                     '-iframework', '-imacros', '-imultilib', '-include',
                     '-install_name', '-iprefix', '-iquote', '-isysroot',
                     '-isystem', '-iwithprefix', '-iwithprefixbefore',
                     '-u'):
            reduced_args.append(arg)
            reduced_args.append(iter_args.next())
        elif arg == '-MT':
            target = iter_args.next()
        elif arg == '-fprofile-use':
            return None
        else:
            if arg in ('-M', '-MM', '-MD', '-MMD'):
                need_explicit_target = True
            if arg.startswith('-') and len(arg) != 1:
                reduced_args.append(arg)
            else:
                input += (arg,)

    if not compilation or not output or len(input) != 1 or input[0] == '-':
        return None

    typ = os.path.splitext(input[0])[1]
    if typ not in FILE_TYPES:
        return None

    mt = ['-MT', target or output] if need_explicit_target else []

    return program, input[0], FILE_TYPES[typ], output, args, mt, reduced_args


def hash_key(program, args, preprocessed):
    import hashlib
    hash = hashlib.new('sha1')
    hash.update(str(os.path.getmtime(program)))
    hash.update(str(os.path.getsize(program)))
    hash.update(program)
    hash.update(' '.join('args'))
    hash.update(preprocessed)
    digest = hash.hexdigest()
    return '%s/%s/%s/%s' % (digest[0], digest[1], digest[2], digest)


def cache_store(out, output, key):
    def do_store(output, key, conn):
        cache = CacheData.from_object(output)
        conn.send('')
        conn.close()

        timer = Timer.current().start_group('put')
        bucket = Bucket()
        try:
            bucket.put(key, cache.data)
        except:
            out.write('sccache: Failure caching %s as %s to cache [%s]\n' %
                (output, key, status(timer, bucket=bucket)))
            return
        out.write('sccache: Cached %s as %s [%s]\n' %
            (output, key, status(timer)))

    from multiprocessing import Process, Pipe
    parent_conn, child_conn = Pipe()
    Process(target=do_store, args=(output, key, child_conn)).start()
    parent_conn.recv()
    parent_conn.close()


def status(timer, bytes=0, bucket=None):
    fmt = '%(timer)s'
    if bytes:
        fmt += ' %(bytes)dB'
    if bucket:
        fmt += ' %(status)d'
    return fmt % {
        'bytes': bytes,
        'status': bucket.status if bucket else 0,
        'timer': timer,
    }

def main(command, out=sys.stderr):
    timer = Timer()
    if not command:
        return 0

    timer.start('args')
    compilation = parse_arguments(command)
    if not compilation:
        # Fallback to whatever we're wrapping in case parse_arguments didn't
        # like the command line.
        return subprocess.call(command)

    timer.start('pp')
    program, input, file_type, output, args, mt, reduced_args = compilation
    try:
        preprocessed = subprocess.check_output(
            [program, '-E', input] + mt + reduced_args)
    except subprocess.CalledProcessError as e:
        out.write('sccache: Preprocessor failed [%s]\n' % (timer))
        return e.returncode

    bucket = None

    if preprocessed:
        timer.start('hash')
        key = hash_key(program, args, preprocessed)

        if not 'SCCACHE_RECACHE' in os.environ:
            timer.start_group('get')
            bucket = Bucket()
            cache = bucket.get(key)
            if cache:
                timer.start('unz')
                cache.dump(output)
                out.write('sccache: Using cache %s for %s [%s]\n' %
                    (key, output, status(timer, len(cache.data))))
                return 0

    timer.start('comp')
    proc = subprocess.Popen([program, '-c', '-x', file_type, '-', '-o', output]
        + reduced_args, stdin=subprocess.PIPE)
    proc.communicate(preprocessed)
    ret = proc.wait()

    if ret or not os.path.exists(output):
        out.write('sccache: Compilation failed for %s [%s]\n' %
            (output, status(timer, bucket=bucket)))
        return ret

    timer.start('spawn')
    cache_store(out, output, key)
    out.write('sccache: Caching %s as %s [%s]\n' % (output, key,
        status(timer, bucket=bucket)))
    return 0


if __name__ == '__main__':
    # When invoked from configure, configure tests may fail because of
    # our verbose output. Fortunately, we inherit configure's file
    # descriptors, so we can redirect to file descriptor 5, which is
    # config.log.
    try:
        out = os.fdopen(5, 'w')
    except:
        out = sys.stderr

    ret = main(sys.argv[1:], out=out)
    out.flush()
    # Use os._exit because we don't want automatic Process.join.
    os._exit(ret)
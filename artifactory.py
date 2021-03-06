#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim:ts=4:sw=4:expandtab
#
# ==================================================================
#
# Copyright (c) 2005-2014 Parallels Software International, Inc.
# Released under the terms of MIT license (see LICENSE for details)
#
# ==================================================================
#
# pylint: disable=no-self-use, maybe-no-member

""" artifactory: a python module for interfacing with JFrog Artifactory

This module is intended to serve as a logical descendant of pathlib
(https://docs.python.org/3/library/pathlib.html), a Python 3 module
 for object-oriented path manipulations. As such, it implements
everything as closely as possible to the origin with few exceptions,
such as stat().

There are PureArtifactoryPath and ArtifactoryPath that can be used
to manipulate artifactory paths. See pathlib docs for details how
pure paths can be used.
"""

import sys
import errno
import pathlib
import collections
import requests
import json
import dateutil.parser
import requests.packages.urllib3 as urllib3


class HTTPResponseWrapper(object):
    """
    This class is intended as a workaround for 'requests' module
    inability to consume HTTPResponse as a streaming upload source.
    I.e. if you want to download data from one url and upload it
    to another.
    The problem is that underlying code uses seek() and tell() to
    calculate stream length, but HTTPResponse throws a NotImplementedError,
    according to python file-like object implementation guidelines, since
    the stream is obviously non-rewindable.
    Another problem arises when requests.put() tries to calculate stream
    length with other methods. It tries several ways, including len()
    and __len__(), and falls back to reading the whole stream. But
    since the stream is not rewindable, by the time it tries to send
    actual content, there is nothing left in the stream.
    """
    def __init__(self, obj):
        self.obj = obj

    def __getattr__(self, attr):
        """
        Redirect member requests except seek() to original object
        """
        if attr in self.__dict__:
            return self.__dict__[attr]

        if attr == 'seek':
            raise AttributeError

        return getattr(self.obj, attr)

    def __len__(self):
        """
        __len__ will be used by requests to determine stream size
        """
        return int(self.getheader('content-length'))


class _ArtifactoryFlavour(pathlib._Flavour):
    """
    Implements Artifactory-specific pure path manipulations.
    I.e. what is 'drive', 'root' and 'path' and how to split full path into
    components.
    See 'pathlib' documentation for explanation how those are used.

    drive: in context of artifactory, it's the base URI like
      http://mysite/artifactory

    root: repository, e.g. 'libs-snapshot-local' or 'ext-release-local'

    path: relative artifact path within the repository
    """
    sep = '/'
    altsep = '/'
    has_drv = True
    pathmod = pathlib.posixpath

    is_supported = (True)

    def parse_parts(self, parts):
        drv, root, parsed = super(_ArtifactoryFlavour, self).parse_parts(parts)
        return drv, root, parsed

    def splitroot(self, part, sep=sep):
        """
        Splits path string into drive, root and relative path

        Uses '/artifactory/' as a splitting point in URI. Everything
        before it, including '/artifactory/' itself is treated as drive.
        The next folder is treated as root, and everything else is taken
        for relative path.
        """
        drv = ''
        root = ''

        mark = sep+'artifactory'+sep
        parts = part.split(mark)

        if len(parts) >= 2:
            drv = parts[0] + mark.rstrip('/')
            rest = '/' + mark.join(parts[1:])
        elif part.endswith(sep+'artifactory'):
            drv = part
            rest = ''
        else:
            rest = part

        if not rest:
            return drv, '', ''

        if rest == '/':
            return drv, '', ''

        if rest.startswith(sep):
            root, _, part = rest[1:].partition(sep)
            root = sep + root + sep

        return drv, root, part

    def casefold(self, string):
        """
        Convert path string to default FS case if it's not
        case-sensitive. Do nothing otherwise.
        """
        return string

    def casefold_parts(self, parts):
        """
        Convert path parts to default FS case if it's not
        case sensitive. Do nothing otherwise.
        """
        return parts

    def resolve(self, path):
        """
        Resolve all symlinks and relative paths in 'path'
        """
        return path

    def is_reserved(self, _):
        """
        Returns True if the file is 'reserved', e.g. device node or socket
        For Artifactory there are no reserved files.
        """
        return False

    def make_uri(self, path):
        """
        Return path as URI. For Artifactory this is the same as returning
        'path' unmodified.
        """
        return path


_artifactory_flavour = _ArtifactoryFlavour()


ArtifactoryFileStat = collections.namedtuple(
    'ArtifactoryFileStat',
    ['ctime',
     'mtime',
     'created_by',
     'modified_by',
     'mime_type',
     'size',
     'sha1',
     'md5',
     'is_dir',
     'children'])


class _ArtifactoryAccessor(pathlib._Accessor):
    """
    Implements operations with Artifactory REST API
    """
    def rest_get(self, url, auth=None):
        """
        Perform a GET request to url with optional authentication
        """
        res = requests.get(url, auth=auth)
        return res.text, res.status_code

    def rest_put(self, url, auth=None):
        """
        Perform a PUT request to url with optional authentication
        """
        res = requests.put(url, auth=auth)
        return res.text, res.status_code

    def rest_del(self, url, auth=None):
        """
        Perform a DELETE request to url with optional authentication
        """
        res = requests.delete(url, auth=auth)
        return res.text, res.status_code

    def rest_put_stream(self, url, stream, auth=None):
        """
        Perform a chunked PUT request to url with optional authentication
        This is specifically to upload files.
        """
        res = requests.put(url, auth=auth, data=stream)
        return res.text, res.status_code

    def rest_get_stream(self, url, auth=None):
        """
        Perform a chunked GET request to url with optional authentication
        This is specifically to download files.
        """
        res = requests.get(url, auth=auth, stream=True)
        return res.raw, res.status_code

    def get_stat_json(self, pathobj):
        """
        Request remote file/directory status info
        Returns a json object as specified by Artifactory REST API
        """
        url = '/'.join([pathobj.drive,
                        'api/storage',
                        str(pathobj.relative_to(pathobj.drive)).strip('/')])

        text, code = self.rest_get(url, pathobj.auth)
        if code == 404 and "Unable to find item" in text:
            raise OSError(2, "No such file or directory: '%s'" % url)
        if code != 200:
            raise RuntimeError(text)

        return json.loads(text)

    def stat(self, pathobj):
        """
        Request remote file/directory status info
        Returns an object of class ArtifactoryFileStat.

        The following fields are available:
          ctime -- file creation time
          mtime -- file modification time
          created_by -- original uploader
          modified_by -- last user modifying the file
          mime_type -- MIME type of the file
          size -- file size
          sha1 -- SHA1 digest of the file
          md5 -- MD5 digest of the file
          is_dir -- 'True' if path is a directory
          children -- list of children names
        """
        jsn = self.get_stat_json(pathobj)

        is_dir = False
        if 'size' not in jsn:
            is_dir = True

        children = None
        if 'children' in jsn:
            children = [child['uri'][1:] for child in jsn['children']]

        stat = ArtifactoryFileStat(
            ctime=dateutil.parser.parse(jsn['created']),
            mtime=dateutil.parser.parse(jsn['lastModified']),
            created_by=jsn.get('createdBy', None),
            modified_by=jsn.get('modifiedBy', None),
            mime_type=jsn.get('mimeType', None),
            size=int(jsn.get('size', '0')),
            sha1=jsn.get('checksums', {'sha1': None})['sha1'],
            md5=jsn.get('checksums', {'md5': None})['md5'],
            is_dir=is_dir,
            children=children)

        return stat

    def is_dir(self, pathobj):
        """
        Returns True if given path is a directory
        """
        try:
            stat = self.stat(pathobj)

            return stat.is_dir
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
            return False

    def is_file(self, pathobj):
        """
        Returns True if given path is a regular file
        """
        try:
            stat = self.stat(pathobj)

            return not stat.is_dir
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
            return False

    def listdir(self, pathobj):
        """
        Returns a list of immediate sub-directories and files in path
        """
        stat = self.stat(pathobj)

        if not stat.is_dir:
            raise OSError(20, "Not a directory: %s" % str(pathobj))

        return stat.children

    def mkdir(self, pathobj, _):
        """
        Creates remote directory
        Note that this operation is not recursive
        """
        if not pathobj.drive or not pathobj.root:
            raise RuntimeError("Full path required: '%s'" % str(pathobj))

        if pathobj.exists():
            raise OSError(17, "File exists: '%s'" % str(pathobj))

        url = str(pathobj) + '/'
        text, code = self.rest_put(url, auth=pathobj.auth)

        if not code == 201:
            raise RuntimeError("%s %d" % (text, code))

    def rmdir(self, pathobj):
        """
        Removes a directory
        """
        stat = self.stat(pathobj)

        if not stat.is_dir:
            raise OSError(20, "Not a directory: '%s'" % str(pathobj))

        url = str(pathobj) + '/'

        text, code = self.rest_del(url, auth=pathobj.auth)

        if code not in [200, 202, 204]:
            raise RuntimeError("Failed to delete directory: '%s'" % text)

    def unlink(self, pathobj):
        """
        Removes a file
        """
        stat = self.stat(pathobj)

        if stat.is_dir:
            raise OSError(1, "Operation not permitted: '%s'" % str(pathobj))

        url = str(pathobj)
        text, code = self.rest_del(url, auth=pathobj.auth)

        if code not in [200, 202, 204]:
            raise RuntimeError("Failed to delete file: %d '%s'" % (code, text))

    def touch(self, pathobj):
        """
        Create an empty file
        """
        if not pathobj.drive or not pathobj.root:
            raise RuntimeError('Full path required')

        if pathobj.exists():
            return

        url = str(pathobj)
        text, code = self.rest_put(url, auth=pathobj.auth)

        if not code == 201:
            raise RuntimeError("%s %d" % (text, code))

    def owner(self, pathobj):
        """
        Returns file owner
        This makes little sense for Artifactory, but to be consistent
        with pathlib, we return modified_by instead, if available
        """
        stat = self.stat(pathobj)

        if not stat.is_dir:
            return stat.modified_by
        else:
            return 'nobody'

    def creator(self, pathobj):
        """
        Returns file creator
        This makes little sense for Artifactory, but to be consistent
        with pathlib, we return created_by instead, if available
        """
        stat = self.stat(pathobj)

        if not stat.is_dir:
            return stat.created_by
        else:
            return 'nobody'

    def open(self, pathobj):
        """
        Opens the remote file and returns a file-like object HTTPResponse
        Given the nature of HTTP streaming, this object doesn't support
        seek()
        """
        url = str(pathobj)
        raw, code = self.rest_get_stream(url, auth=pathobj.auth)

        if not code == 200:
            raise RuntimeError("%d" % code)

        return raw

    def deploy(self, pathobj, fobj):
        """
        Uploads a given file-like object
        HTTP chunked encoding will be attempted
        """
        if isinstance(fobj, urllib3.response.HTTPResponse):
            fobj = HTTPResponseWrapper(fobj)

        url = str(pathobj)

        text, code = self.rest_put_stream(url, fobj, auth=pathobj.auth)

        if code not in [200, 201]:
            raise RuntimeError("%s" % text)


_artifactory_accessor = _ArtifactoryAccessor()


class ArtifactoryProAccessor(_ArtifactoryAccessor):
    """
    TODO: implement OpenSource/Pro differentiation
    """
    pass


class ArtifactoryOpensourceAccessor(_ArtifactoryAccessor):
    """
    TODO: implement OpenSource/Pro differentiation
    """
    pass


class PureArtifactoryPath(pathlib.PurePath):
    """
    A class to work with Artifactory paths that doesn't connect
    to Artifactory server. I.e. it supports only basic path
    operations.
    """
    _flavour = _artifactory_flavour
    __slots__ = ()


class _FakePathTemplate(object):
    def __init__(self, accessor):
        self._accessor = accessor


class ArtifactoryPath(pathlib.Path, PureArtifactoryPath):
    """
    Implements fully-featured pathlib-like Artifactory interface
    Unless explicitly mentioned, all methods copy the behaviour
    of their pathlib counterparts.

    Note that because of peculiarities of pathlib.Path, the methods
    that create new path objects, have to also manually set the 'auth'
    field, since the copying strategy of pathlib.Path is not based
    on regular constructors, but rather on templates.
    """
    # Pathlib limits what members can be present in 'Path' class,
    # so authentication information has to be added via __slots__
    __slots__ = ('auth')

    def __new__(cls, *args, **kwargs):
        """
        pathlib.Path overrides __new__ in order to create objects
        of different classes based on platform. This magic prevents
        us from adding an 'auth' argument to the constructor.
        So we have to first construct ArtifactoryPath by Pathlib and
        only then add auth information.
        """
        obj = pathlib.Path.__new__(cls, *args, **kwargs)

        obj.auth = None
        if 'auth' in kwargs:
            obj.auth = kwargs['auth']

        return obj

    def _init(self, *args, **kwargs):
        if not 'template' in kwargs:
            kwargs['template'] = _FakePathTemplate(_artifactory_accessor)

        super(ArtifactoryPath, self)._init(*args, **kwargs)

    @property
    def parent(self):
        """
        The logical parent of the path.
        """
        obj = super(ArtifactoryPath, self).parent
        obj.auth = self.auth
        return obj

    def with_name(self, name):
        """
        Return a new path with the file name changed.
        """
        obj = super(ArtifactoryPath, self).with_name(name)
        obj.auth = self.auth
        return obj

    def with_suffix(self, suffix):
        """
        Return a new path with the file suffix changed (or added, if none).
        """
        obj = super(ArtifactoryPath, self).with_suffix(suffix)
        obj.auth = self.auth
        return obj

    def relative_to(self, *other):
        """
        Return the relative path to another path identified by the passed
        arguments.  If the operation is not possible (because this is not
        a subpath of the other path), raise ValueError.
        """
        obj = super(ArtifactoryPath, self).relative_to(*other)
        obj.auth = self.auth
        return obj

    def joinpath(self, *args):
        """
        Combine this path with one or several arguments, and return a
        new path representing either a subpath (if all arguments are relative
        paths) or a totally different path (if one of the arguments is
        anchored).
        """
        obj = super(ArtifactoryPath, self).joinpath(*args)
        obj.auth = self.auth
        return obj

    def __truediv__(self, key):
        """
        Join two paths with '/'
        """
        obj = super(ArtifactoryPath, self).__truediv__(key)
        obj.auth = self.auth
        return obj

    def __rtruediv__(self, key):
        """
        Join two paths with '/'
        """
        obj = super(ArtifactoryPath, self).__truediv__(key)
        obj.auth = self.auth
        return obj

    if sys.version_info < (3,):
        __div__ = __truediv__
        __rdiv__ = __rtruediv__

    def _make_child(self, args):
        obj = super(ArtifactoryPath, self)._make_child(args)
        obj.auth = self.auth
        return obj

    def _make_child_relpath(self, args):
        obj = super(ArtifactoryPath, self)._make_child_relpath(args)
        obj.auth = self.auth
        return obj

    def open(self, mode='r', buffering=-1, encoding=None,
             errors=None, newline=None):
        """
        Open the given Artifactory URI and return a file-like object
        HTTPResponse, as if it was a regular filesystem object.
        The only difference is that this object doesn't support seek()
        """
        if mode != 'r' or buffering != -1 or encoding or errors or newline:
            raise NotImplementedError('Only the default open() ' +
                                      'arguments are supported')

        return self._accessor.open(self)

    def owner(self):
        """
        Returns file owner.
        This makes little sense for Artifactory, but to be consistent
        with pathlib, we return modified_by instead, if available.
        """
        return self._accessor.owner(self)

    def creator(self):
        """
        Returns file creator.
        This makes little sense for Artifactory, but to be consistent
        with pathlib, we return created_by instead, if available.
        """
        return self._accessor.creator(self)

    def is_dir(self):
        """
        Whether this path is a directory.
        """
        return self._accessor.is_dir(self)

    def is_file(self):
        """
        Whether this path is a regular file.
        """
        return self._accessor.is_file(self)

    def is_symlink(self):
        """
        Whether this path is a symlink.
        Since Artifactory doen't have special files, returns False.
        """
        return False

    def is_socket(self):
        """
        Whether this path is a socket.
        Since Artifactory doen't have special files, returns False.
        """
        return False

    def is_fifo(self):
        """
        Whether this path is a fifo.
        Since Artifactory doen't have special files, returns False.
        """
        return False

    def is_block_device(self):
        """
        Whether this path is a block device.
        Since Artifactory doen't have special files, returns False.
        """
        return False

    def is_char_device(self):
        """
        Whether this path is a character device.
        Since Artifactory doen't have special files, returns False.
        """
        return False

    def touch(self, mode=0o666, exist_ok=True):
        """
        Create a file if it doesn't exist.
        Mode is ignored by Artifactory.
        """
        if self.exists() and not exist_ok:
            raise OSError(17, "File exists", str(self))

        self._accessor.touch(self)

    def chmod(self, mode):
        """
        Throw NotImplementedError
        Changing access rights makes no sense for Artifactory.
        """
        raise NotImplementedError()

    def lchmod(self, mode):
        """
        Throw NotImplementedError
        Changing access rights makes no sense for Artifactory.
        """
        raise NotImplementedError()

    def symlink_to(self, target, target_is_directory=False):
        """
        Throw NotImplementedError
        Artifactory doesn't have symlinks
        """
        raise NotImplementedError()

    def deploy(self, fobj):
        """
        Upload the given file object to this path
        """
        return self._accessor.deploy(self, fobj)

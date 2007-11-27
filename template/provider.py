import os
import re
import sys
import stat
import time
import types

from template import util
from template.base import Base, TemplateException
from template.config import Config
from template.constants import *
from template.document import Document


PREV = 0
NAME = 1
DATA = 2
LOAD = 3
NEXT = 4
STAT = 5

MAX_DIRS = 64
STAT_TTL = 1
DEBUG = 0

RELATIVE_PATH = re.compile(r"(?:^|/)\.+/")


class Error(Exception):
  pass


class Provider(Base):
  def __init__(self, params):
    Base.__init__(self)
    size = params.get("CACHE_SIZE")
    path = params.get("INCLUDE_PATH") or "."
    cdir = params.get("COMPILE_DIR") or ""
    dlim = params.get("DELIMITER")

    if dlim is None:
      if os.name == "nt":
        dlim = r":(?!\/)"
      else:
        dlim = r":"
    if not isinstance(path, list):
      path = re.split(dlim, path)
    if size is not None and (size == 1 or size < 0):
      size = 2
    debug = params.get("DEBUG")
    if debug is not None:
      self.DEBUG = debug & (DEBUG_PROVIDER & DEBUG_FLAGS)
    else:
      self.DEBUG = DEBUG
    if cdir:
      for dir in path:
        if not isinstance(dir, str):
          continue
        wdir = dir
        if os.name == "nt":
          wdir = re.sub(r":", "", wdir)
        if not os.path.isdir(wdir):
          os.makedirs(wdir)

    self.LOOKUP = {}
    self.SLOTS  = 0
    self.SIZE   = size
    self.INCLUDE_PATH = path
    self.DELIMITER = dlim
    self.COMPILE_DIR = cdir
    self.COMPILE_EXT = params.get("COMPILE_EXT") or ""
    self.ABSOLUTE = params.get("ABSOLUTE") or 0
    self.RELATIVE = params.get("RELATIVE") or 0
    self.TOLERANT = params.get("TOLERANT") or 0
    self.DOCUMENT = params.get("DOCUMENT") or Document
    self.PARSER   = params.get("PARSER")
    self.DEFAULT  = params.get("DEFAULT")
    self.ENCODING = params.get("ENCODING")
    self.PARAMS   = params
    self.HEAD     = None
    self.TAIL     = None

  def fetch(self, name, prefix=None):
    if not isinstance(name, str):
      data = self._load(name)
      data = data and self._compile(data)
      data = data and data["data"]
      return data
    elif os.path.isabs(name):
      if self.ABSOLUTE:
        return self._fetch(name)
      elif self.TOLERANT:
        return None
      else:
        raise Error("%s: absolute paths are not allowed (set ABSOLUTE option)"
                    % name)
    elif RELATIVE_PATH.search(name):
      if self.RELATIVE:
        return self._fetch(name)
      elif self.TOLERANT:
        return None
      else:
        raise Error("%s: relative paths are not allowed (set RELATIVE option)"
                    % name)
    else:
      if self.INCLUDE_PATH:
        return self._fetch_path(name)
      else:
        return None

  def _load(self, name, alias=None):
    now = time.time()
    if alias is None and isinstance(name, str):
      alias = name
    # LOAD: {
    if isinstance(name, util.Literal):
      # name can be a Literal wrapper around the input text...
      data = {"text": name.text(),
              "time": now,
              "load": 0}
      if alias is not None:
        data["name"] = alias
      else:
        data["name"] = "input text"
    elif not isinstance(name, str):
      # ...or a file handle...
      text = name.read()
      data = {"text": name.read(),
              "time": now,
              "load": 0}
      if alias is not None:
        data["name"] = alias
      else:
        data["name"] = "input file"
    elif os.path.isfile(name):
      try:
        fh = open(name)
      except IOError, e:
        if self.TOLERANT:
          return None
        else:
          raise Error("%s: %s" % (alias, e))
      else:
        data = {"name": alias,
                "path": name,
                "text": fh.read(),
                "time": os.stat(name)[stat.ST_MTIME],
                "load": now}
        fh.close()
    else:
      return None

    if data and isinstance(data, dict) and data.get("path") is None:
      data["path"] = data["name"]

    return data

  def _fetch(self, name):
    compiled = self._compiled_filename(name)
    if self.SIZE is not None and not self.SIZE:
      if (compiled
          and os.path.isfile(compiled)
          and not self._modified(name, os.stat(compiled)[stat.ST_MTIME])):
        data = self.__load_compiled(compiled)
      else:
        data = self._load(name)
        data = data and self._compile(data, compiled)
        data = data and data["data"]
    else:
      slot = self.LOOKUP.get(name)
      if slot:
        # cached entry exists, so refresh slot and extract data
        data = self._refresh(slot)
        data = slot[DATA]
      else:
        # nothing in cache so try to load, compile, and cache
        if (compiled
            and os.path.isfile(compiled)
            and os.stat(name)[stat.ST_MTIME]
            <= os.stat(compiled)[stat.ST_MTIME]):
          data = self.__load_compiled(compiled)
          self.store(name, data)
        else:
          data = self._load(name)
          data = data and self._compile(data, compiled)
          data = data and self._store(name, data)

    return data

  def _compile(self, data, compfile=None):
    text = data["text"]
    error = None

    if not self.PARSER:
      self.PARSER = Config.parser(self.PARAMS)

    # discard the template text - we don't need it any more
    del data["text"]

    parsedoc = self.PARSER.parse(text, data)
    if parsedoc:
      parsedoc["METADATA"].setdefault("name", data["name"])
      parsedoc["METADATA"].setdefault("modtime", data["time"])
      # write the Python code to the file compfile, if defined
      if compfile:
        basedir = os.path.dirname(compfile)
        if not os.path.isdir(basedir):
          try:
            os.makedirs(basedir)
          except IOError, e:
            error = ("failed to create compiled templates "
                     "directory: %s (%s)" % (basedir, e))
        if not error:
          docclass = self.DOCUMENT
          if not docclass.write_python_file(compfile, parsedoc):
            error = "cache failed to write %s: %s" % (
              os.path.basename(compfile), docclass.Error())
        if error is None and data.get("time") is not None:
          if not compfile:
            raise Error("invalid null filename")
          ctime = int(data.get("time"))
          os.utime(compfile, (ctime, ctime))

      if not error:
        data["data"] = Document(parsedoc)
        return data

    else:
      error = TemplateException("parse",
                                "%s %s" % (data["name"], self.PARSER.error()))

    if self.TOLERANT:
      return None
    else:
      raise Error(error)

  def _fetch_path(self, name):
    compiled = None
    caching = self.SIZE is None or self.SIZE
    # INCLUDE: {
    while True:
      # the template may have been stored using a non-filename name
      slot = self.LOOKUP.get(name)
      if caching and slot:
        # cached entry exists, so refresh slot and extract data
        data = self._refresh(slot)
        data = slot[DATA]
        break  # last INCLUDE;
      paths = self.paths()
      # search the INCLUDE_PATH for the file, in cache or on disk
      for dir in paths:
        path = os.path.join(dir, name)
        slot = self.LOOKUP.get(path)
        if caching and slot:
          # cached entry exists, so refresh slot and extract data
          data = self._refresh(slot)
          data = slot[DATA]
          return data  # last INCLUDE;
        elif os.path.isfile(path):
          if self.COMPILE_EXT or self.COMPILE_DIR:
            compiled = self._compiled_filename(path)
          if (compiled
              and os.path.isfile(compiled)
              and os.stat(path)[stat.ST_MTIME] <=
                  os.stat(compiled)[stat.ST_MTIME]):
            data = self.__load_compiled(compiled)
            if data:
              # store in cache
              data = self.store(path, data)
              return data  # last INCLUDE;
          # compiled is set if an attempt to write the compiled
          # template to disk should be made
          data = self._load(path, name)
          data = data and self._compile(data, compiled)
          if caching:
            data = data and self._store(path, data)
          if not caching:
            data = data and data["data"]
          # all done if error is OK or ERROR
          return data  # last INCLUDE;

      # template not found, so look for a DEFAULT template
      if self.DEFAULT is not None and name != self.DEFAULT:
        name = self.DEFAULT
        # redo INCLUDE;
      else:
        return None

    return data

  def _compiled_filename(self, file):
    if not (self.COMPILE_EXT or self.COMPILE_DIR):
      return None
    path = file
    if os.name == "nt":
      path = path.replace(":", "")
    compiled = "%s%s" % (path, self.COMPILE_EXT)
    if self.COMPILE_DIR:
      # Can't use os.path.join here; compiled may be absolute.
      compiled = "%s%s%s" % (self.COMPILE_DIR, os.path.sep, compiled)
    return compiled

  def _modified(self, name, time=None):
    load = os.stat(name)[stat.ST_MTIME]
    if not load:
      return time and 1 or 0
    if time:
      return load > time
    else:
      return load

  def _refresh(self, slot):
    data = None
    if time.time() - slot[STAT] > STAT_TTL:
      statbuf = statfile(slot[NAME])
      if statbuf:
        slot[STAT] = time.time()
        if statbuf[stat.ST_MTIME] != slot[LOAD]:
          data = self._load(slot[NAME], slot[DATA].name)
          data = self._compile(data)
          slot[DATA] = data["data"]
          slot[LOAD] = data["time"]
    if self.HEAD is not slot:
      # remove existing slot from usage chain...
      if slot[PREV]:
        slot[PREV][NEXT] = slot[NEXT]
      else:
        self.HEAD = slot[NEXT]
      if slot[NEXT]:
        slot[NEXT][PREV] = slot[PREV]
      else:
        self.TAIL = slot[PREV]
      # ...and add to start of list
      head = self.HEAD
      if head:
        head[PREV] = slot
      slot[PREV] = None
      slot[NEXT] = head
      self.HEAD  = slot

    return data

  def __load_compiled(self, path):
    try:
      return Document.evaluate_file(path, "document")
    except TemplateException, e:
      raise Error("compiled template %s: %s" % (path, e))

  def _store(self, name, data, compfile=None):
    load = self._modified(name)
    data = data["data"]
    if self.SIZE is not None and self.SLOTS >= self.SIZE:
      # cache has reached size limit, so reuse oldest entry
      # remove entry from tail or list
      slot = self.TAIL
      slot[PREV][NEXT] = None
      self.TAIL = slot[PREV]

      # remove name lookup for old node
      del self.LOOKUP[slot[NAME]]

      # add modified node to head of list
      head = self.HEAD
      if head:
        head[PREV] = slot
      slot[:] = [None, name, data, load, head, time.time()]
      self.HEAD = slot

      # add name lookup for new node
      self.LOOKUP[name] = slot
    else:
      # cache is under size limit, or none is defined
      head = self.HEAD
      slot = [None, name, data, load, head, time.time()]
      if head:
        head[PREV] = slot
      self.HEAD = slot
      if not self.TAIL:
        self.TAIL = slot
      # add lookup from name to slot and increment nslots
      self.LOOKUP[name] = slot
      self.SLOTS += 1

    return data

  def paths(self):
    ipaths = self.INCLUDE_PATH[:]
    opaths = []
    count = MAX_DIRS
    while ipaths and count > 0:
      count -= 1
      dir = ipaths.pop(0)
      if not dir:
        continue
      # dir can be a sub or object ref which returns a reference
      # to a dynamically generated list of search paths
      if callable(dir):
        dpaths = dir()
        ipaths[:0] = dpaths
      elif isinstance(dir, types.InstanceType) and util.can(dir, "paths"):
        dpaths = dir.paths()
        if not dpaths:
          raise Error(dir.error())
        ipaths[:0] = dpaths
      else:
        opaths.append(dir)

    if ipaths:
      raise Error("INCLUDE_PATH exceeds %d directories" % (MAX_DIRS,))

    return opaths

  def store(self, name, data):
    return self._store(name, { "data": data, "load": 0 })

  def load(self, name, prefix=None):
    path = name
    error = None
    if os.path.isabs(name):
      if not self.ABSOLUTE:
        error = ("%s: absolute paths are not allowed (set ABSOLUTE option)"
                 % name)
    elif RELATIVE_PATH.search(name):
      if not self.RELATIVE:
        error = ("%s: relative paths are not allowed (set RELATIVE option)"
                 % name)
    else:
      paths = self.paths()
      if not paths:
        return self.error(), STATUS_ERROR
      for dir in paths:
        path = os.path.join(dir, name)
        if os.path.isfile(path):
          break
      else:
        path = None

    if path is not None and not error:
      try:
        data = open(path).read()
      except IOError, e:
        error = "%s: %s" % (name, e)

    if error:
      if self.TOLERANT:
        return None
      else:
        raise Error(error)
    elif path is None:
      return None
    else:
      return data

  def include_path(self, path=None):
    if path:
      self.INCLUDE_PATH = None
    return self.INCLUDE_PATH

  def parser(self):
    return self.PARSER

  def tolerant(self):
    return self.TOLERANT


def statfile(path):
  try:
    return os.stat(path)
  except OSError:
    return None

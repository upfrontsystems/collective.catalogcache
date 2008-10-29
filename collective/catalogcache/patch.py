import BTrees.Length
from BTrees.IIBTree import intersection, weightedIntersection, IISet
from BTrees.OIBTree import OIBTree
from BTrees.IOBTree import IOBTree
from Products.ZCatalog.Lazy import LazyMap, LazyCat
import types

from DateTime import DateTime
from md5 import md5
import time
from Products.ZCatalog.Catalog import LOG
from os import environ

try:
    import memcache
    s = environ.get('MEMCACHE_SERVERS', '')
    if s:
        servers = s.split(',')
    if not servers:
        LOG.info("No memcached servers defined. Catalog will function as normal.")
        HAS_MEMCACHE = False
    else:
        mem_cache = memcache.Client(servers, debug=0)
        HAS_MEMCACHE = True
        LOG.info("Using memcached servers %s" % ",".join(servers))

except ImportError:
    mem_cache = None
    HAS_MEMCACHE = False
    LOG.info("Cannot import memcached. Catalog will function as normal.")

MEMCACHE_DURATION = 7200
MEMCACHE_RETRY_INTERVAL = 10
memcache_insertion_timestamps = {}
_hits = {}
_misses = {}
_memcache_failure_timestamp = 0
_cache_misses = {}

def _memcache_available(self):
    global HAS_MEMCACHE, MEMCACHE_RETRY_INTERVAL, _memcache_failure_timestamp
    if not HAS_MEMCACHE:
        return False

    now_seconds = int(time.time())
    if now_seconds - _memcache_failure_timestamp < MEMCACHE_RETRY_INTERVAL:
        return False

    _memcache_failure_timestamp = 0
    return True

def _cache_result(self, cache_key, rs, search_indexes=[]):
    global mem_cache, MEMCACHE_DURATION,  _memcache_failure_timestamp

    if not self._memcache_available():
        return

    # Insane case. This only happens when search returns everything 
    # in the catalog. Naturally we avoid this.
    if rs is None:
        return

    cache_id = '/'.join(self.getPhysicalPath())
    to_set = {}

    lcache_key = cache_id + cache_key
    to_set[cache_key] = rs

    # Use get_multi with a prefix to save bandwidth
    to_get = []
    for r in rs:
        to_get.append(str(r))
        to_set[str(r)] = [lcache_key]
    for idx in search_indexes:
        if idx in ('sort_on','sort_order','sort_limit'): continue  
        to_get.append(idx)
        to_set[idx] = [lcache_key]

    # Augment the values of to_set with possibly existing values
    # An edge case in the python memcache wrapper requires that
    # we catch KeyErrors.
    try:        
        result = mem_cache.get_multi(to_get, key_prefix=cache_id)
    except KeyError:
        return
    for k,v in result.items():
        if not isinstance(v, types.ListType): continue
        to_set[k].extend(v)

    if to_set:
        now_seconds = int(time.time())

        # During a large number of new queries (and hence new calls to this method)
        # we may try to set the same to_set in memcache over and over again, and
        # all of them will timeout in the python memcache wrapper. The inserts will 
        # probably still take place, but it will be the same to_set applied many
        # times. This is obviously redundant and will cause memcache to consume too
        # much CPU. 
        # To overcome this we abide by this rule: you cannot insert the same set more 
        # than once every N seconds.           
        li = to_set.items()
        li.sort()
        hash = md5(str(li)).hexdigest()
        if (now_seconds - memcache_insertion_timestamps.get(hash, 0)) < 10:
            LOG.debug("Prevent a call to set_multi since the same insert was done recently")
            return
        memcache_insertion_timestamps[hash] = now_seconds                       

        # An edge case in the python memcache wrapper requires that
        # we catch TypeErrors.
        try:
            ret = mem_cache.set_multi(to_set, key_prefix=cache_id, time=MEMCACHE_DURATION)
        except TypeError:
            return
        # Return value of non-empty list indicates error
        if isinstance(ret, types.ListType) and len(ret):
            LOG.error("_cache_result set_multi failed") 
            _memcache_failure_timestamp = now_seconds
            # The return value of set_multi is the original to_set list in 
            # case of no daemons  responding.
            if len(ret) != len(to_set.keys()):
                LOG.error("Some keys were successfully written to memcache. This case needs further handling.")
                # xxx: maybe do a self._clear_cache()?

def _get_cached_result(self, cache_key, default=[]):
    global mem_cache, _memcache_failure_timestamp

    if not self._memcache_available():
        return default

    cache_id = '/'.join(self.getPhysicalPath())
    key = cache_id + cache_key
    _cache_misses.setdefault(key, 0)
    res = mem_cache.get(key)
    # todo: Return default if any item in rs is not an integer. How?        
    if res is None:
        # Record the time of the miss. If we keep missing this key
        # then something is wrong with memcache and we must stop
        # hitting it for a while.
        now_seconds = int(time.time())           
        if _cache_misses.get(key, 0) > 10:
            LOG.error("_get_cache_key failed 10 times") 
            _memcache_failure_timestamp = now_seconds
            _cache_misses.clear()
        else:
            try:
                _cache_misses[key] += 1
            except KeyError:
                pass
        return default

    _cache_misses[key] = 0       
    return res

def _invalidate_cache(self, rid=None, index_name=''):
    """ Invalidate cached results affected by rid and / or index_name
    """
    global mem_cache, _memcache_failure_timestamp

    if not self._memcache_available():
        return

    cache_id = '/'.join(self.getPhysicalPath())
    LOG.debug('[%s] _invalidate_cache rid=%s, index_name=%s' % (cache_id, rid, index_name))

    to_delete = []

    # rid and index_name are mutually exclusive, so no need for get_multi
    # trickery.

    if rid is not None:
        s_rid = cache_id + str(rid)
        rid_map = mem_cache.get(s_rid)
        if rid_map is not None:
            to_delete.extend(rid_map)
            to_delete.append(s_rid)

    if index_name:
        s_index_name = cache_id + index_name
        index_map = mem_cache.get(s_index_name) 
        if index_map is not None:
            to_delete.extend(index_map)
            to_delete.append(s_index_name)

    if to_delete:
        now_seconds = int(time.time())
        LOG.debug('[%s] Remove %s items from cache' % (cache_id, len(to_delete)))
        # Return value of 1 indicates no error
        if mem_cache.delete_multi(to_delete) != 1:
            LOG.error("_invalidate_cache delete_multi failed")
            _memcache_failure_timestamp = now_seconds

def _clear_cache(self):
    global mem_cache
    if not self._memcache_available():
        return
    LOG.debug('Flush cache')
    # No return value for flush_all
    # xxx: This flushes all caches which is inefficient. Currently
    # there is no way to delete all keys starting with eg. 
    # /site/portal_catalog
    mem_cache.flush_all()
    _hits.clear()
    _misses.clear()

def _get_cache_key(self, args):

    def pin_datetime(dt):
        # Pin to dt granularity which is 1 minute by default
        return dt.strftime('%Y-%m-%d.%h:%m %Z')

    items = list(args.request.items())
    items.extend(list(args.keywords.items()))
    items.sort()
    sorted = []
    for k, v in items:
        if isinstance(v, types.ListType):
            v.sort()

        elif isinstance(v, types.TupleType):
            v = list(v)
            v.sort()

        elif isinstance(v, DateTime):
            v = pin_datetime(v)

        elif isinstance(v, types.DictType):               
            # Find DateTime objects in v and pin them
            tsorted = []
            titems = v.items()
            titems.sort()
            for tk, tv in titems:
                if isinstance(tv, DateTime):
                    tv = pin_datetime(tv)
                elif isinstance(tv, types.ListType) or isinstance(tv, types.TupleType):
                    li = []
                    for item in list(tv):
                        if isinstance(item, DateTime):
                            item = pin_datetime(item)
                        li.append(item)
                    tv = li

                tsorted.append((tk, tv))
            v = tsorted

        sorted.append((k,v))
    cache_key = str(sorted)
    return md5(cache_key).hexdigest()

def _get_search_indexes(self, args):
    keys = list(args.request.keys())
    keys.extend(list(args.keywords.keys()))
    return keys

# Methods clear, catalog, uncatalogObject, search are from the default Catalog.py
def clear(self):
    """ clear catalog """

    self._clear_cache()
    self.data  = IOBTree()  # mapping of rid to meta_data
    self.uids  = OIBTree()  # mapping of uid to rid
    self.paths = IOBTree()  # mapping of rid to uid
    self._length = BTrees.Length.Length()

    for index in self.indexes.keys():
        self.getIndex(index).clear()

def catalogObject(self, object, uid, threshold=None, idxs=None,
                  update_metadata=1):
    """
    Adds an object to the Catalog by iteratively applying it to
    all indexes.

    'object' is the object to be cataloged

    'uid' is the unique Catalog identifier for this object

    If 'idxs' is specified (as a sequence), apply the object only
    to the named indexes.

    If 'update_metadata' is true (the default), also update metadata for
    the object.  If the object is new to the catalog, this flag has
    no effect (metadata is always created for new objects).

    """        
    if idxs is None:
        idxs = []

    data = self.data
    index = self.uids.get(uid, None)

    if index is not None:
        self._invalidate_cache(rid=index)

    if index is None:  # we are inserting new data
        #self._clear_cache() # not needed? 
        index = self.updateMetadata(object, uid)

        if not hasattr(self, '_length'):
            self.migrate__len__()
        self._length.change(1)
        self.uids[uid] = index
        self.paths[index] = uid

    elif update_metadata:  # we are updating and we need to update metadata
        self.updateMetadata(object, uid)

    # do indexing

    total = 0

    if idxs==[]: use_indexes = self.indexes.keys()
    else:        use_indexes = idxs

    for name in use_indexes:
        x = self.getIndex(name)
        if hasattr(x, 'index_object'):
            before = self.getIndex(name).getEntryForObject(index, "")

            blah = x.index_object(index, object, threshold)

            after = self.getIndex(name).getEntryForObject(index, "")

            # If index has changed we must invalidate parts of the cache
            if before != after:
                self._invalidate_cache(index_name=name)

            total = total + blah
        else:
            LOG.error('catalogObject was passed bad index object %s.' % str(x))

    return total

def uncatalogObject(self, uid):
    """
    Uncatalog and object from the Catalog.  and 'uid' is a unique
    Catalog identifier

    Note, the uid must be the same as when the object was
    catalogued, otherwise it will not get removed from the catalog

    This method should not raise an exception if the uid cannot
    be found in the catalog.

    """
    data = self.data
    uids = self.uids
    paths = self.paths
    indexes = self.indexes.keys()
    rid = uids.get(uid, None)

    if rid is not None:
        self._invalidate_cache(rid=rid)

        for name in indexes:
            x = self.getIndex(name)
            if hasattr(x, 'unindex_object'):
                x.unindex_object(rid)
        del data[rid]
        del paths[rid]
        del uids[uid]
        if not hasattr(self, '_length'):
            self.migrate__len__()
        self._length.change(-1)
        
    else:
        LOG.error('uncatalogObject unsuccessfully '
                  'attempted to uncatalog an object '
                  'with a uid of %s. ' % str(uid))

def search(self, request, sort_index=None, reverse=0, limit=None, merge=1):
    """Iterate through the indexes, applying the query to each one. If
    merge is true then return a lazy result set (sorted if appropriate)
    otherwise return the raw (possibly scored) results for later merging.
    Limit is used in conjuntion with sorting or scored results to inform
    the catalog how many results you are really interested in. The catalog
    can then use optimizations to save time and memory. The number of
    results is not guaranteed to fall within the limit however, you should
    still slice or batch the results as usual."""

    rs = None # resultset

    # Indexes fulfill a fairly large contract here. We hand each
    # index the request mapping we are given (which may be composed
    # of some combination of web request, kw mappings or plain old dicts)
    # and the index decides what to do with it. If the index finds work
    # for itself in the request, it returns the results and a tuple of
    # the attributes that were used. If the index finds nothing for it
    # to do then it returns None.

    # For hysterical reasons, if all indexes return None for a given
    # request (and no attributes were used) then we append all results
    # in the Catalog. This generally happens when the search values
    # in request are all empty strings or do not coorespond to any of
    # the indexes.

    # Note that if the indexes find query arguments, but the end result
    # is an empty sequence, we do nothing
    global mem_cache
    cache_id = '/'.join(self.getPhysicalPath())
    cache_key = self._get_cache_key(request)
    _misses.setdefault(cache_id, 0)
    _hits.setdefault(cache_id, 0)
    marker = '_marker'
    rs = self._get_cached_result(cache_key, marker)

    if rs is marker:
        LOG.debug('[%s] MISS: %s' % (cache_id, cache_key)) 
        rs = None
        for i in self.indexes.keys():
            index = self.getIndex(i)
            _apply_index = getattr(index, "_apply_index", None)
            if _apply_index is None:
                continue
            r = _apply_index(request)

            if r is not None:
                r, u = r
                w, rs = weightedIntersection(rs, r)

        search_indexes = self._get_search_indexes(request)
        LOG.debug("[%s] Search indexes = %s" % (cache_id, str(search_indexes)))
        self._cache_result(cache_key, rs, search_indexes)

        try:
            _misses[cache_id] += 1
        except KeyError:
            pass
    else:
        try:
            _hits[cache_id] += 1
        except KeyError:
            pass

    # Output stats
    if int(time.time()) % 10 == 0:
        hits = _hits.get(cache_id)
        if hits:
            misses = _misses.get(cache_id, 0)
            LOG.info('[%s] Hit rate: %.2f%%' % (cache_id, hits*100.0/(hits+misses)))

    if rs is None:
        # None of the indexes found anything to do with the request
        # We take this to mean that the query was empty (an empty filter)
        # and so we return everything in the catalog
        if sort_index is None:
            return LazyMap(self.instantiate, self.data.items(), len(self))
        else:
            return self.sortResults(
                self.data, sort_index, reverse,  limit, merge)
    elif rs:
        # We got some results from the indexes.
        # Sort and convert to sequences.
        # XXX: The check for 'values' is really stupid since we call
        # items() and *not* values()
        if sort_index is None and hasattr(rs, 'values'):
            # having a 'values' means we have a data structure with
            # scores.  Build a new result set, sort it by score, reverse
            # it, compute the normalized score, and Lazify it.
                            
            if not merge:
                # Don't bother to sort here, return a list of 
                # three tuples to be passed later to mergeResults
                # note that data_record_normalized_score_ cannot be
                # calculated and will always be 1 in this case
                getitem = self.__getitem__
                return [(score, (1, score, rid), getitem) 
                        for rid, score in rs.items()]
            
            rs = rs.byValue(0) # sort it by score
            max = float(rs[0][0])

            # Here we define our getter function inline so that
            # we can conveniently store the max value as a default arg
            # and make the normalized score computation lazy
            def getScoredResult(item, max=max, self=self):
                """
                Returns instances of self._v_brains, or whatever is passed
                into self.useBrains.
                """
                score, key = item
                r=self._v_result_class(self.data[key])\
                      .__of__(self.aq_parent)
                r.data_record_id_ = key
                r.data_record_score_ = score
                r.data_record_normalized_score_ = int(100. * score / max)
                return r
            
            return LazyMap(getScoredResult, rs, len(rs))

        elif sort_index is None and not hasattr(rs, 'values'):
            # no scores
            if hasattr(rs, 'keys'):
                rs = rs.keys()
            return LazyMap(self.__getitem__, rs, len(rs))
        else:
            # sort.  If there are scores, then this block is not
            # reached, therefore 'sort-on' does not happen in the
            # context of a text index query.  This should probably
            # sort by relevance first, then the 'sort-on' attribute.
            return self.sortResults(rs, sort_index, reverse, limit, merge)
    else:
        # Empty result set
        return LazyCat([])

def __getitem__(self, index, ttype=type(())):
    """
    Returns instances of self._v_brains, or whatever is passed
    into self.useBrains.
    """
    if type(index) is ttype:
        # then it contains a score...
        normalized_score, score, key = index
        # Memcache may be responsible for this bad key. Invalidate the
        # cache and let the exception take place. This should never be
        # needed since we started using transaction aware caching.
        if not self.data.has_key(key) or not isinstance(key, types.IntType):
            LOG.error("Weighted rid %s leads to KeyError. Removing from cache." % index)
            self._invalidate_cache(rid=index)
        r=self._v_result_class(self.data[key]).__of__(self.aq_parent)
        r.data_record_id_ = key
        r.data_record_score_ = score
        r.data_record_normalized_score_ = normalized_score
    else:
        # otherwise no score, set all scores to 1
        if not self.data.has_key(index) or not isinstance(index, types.IntType):
            LOG.error("rid %s leads to KeyError. Removing from cache." % index)
            self._invalidate_cache(rid=index)
        r=self._v_result_class(self.data[index]).__of__(self.aq_parent)
        r.data_record_id_ = index
        r.data_record_score_ = 1
        r.data_record_normalized_score_ = 1
    return r

from Products.ZCatalog.Catalog import Catalog
Catalog._memcache_available = _memcache_available
Catalog._cache_result = _cache_result
Catalog._get_cached_result = _get_cached_result
Catalog._invalidate_cache = _invalidate_cache
Catalog._clear_cache = _clear_cache
Catalog._get_cache_key = _get_cache_key
Catalog._get_search_indexes = _get_search_indexes
Catalog.clear = clear
Catalog.catalogObject = catalogObject
Catalog.uncatalogObject = uncatalogObject
Catalog.search = search
Catalog.__getitem__ = __getitem__

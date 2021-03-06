import collections
import copy
import event_model
from datetime import datetime
import dask
import dask.bag
from dask import array
import functools
import heapq
import importlib
import itertools
import intake.catalog.base
import errno
import intake.container.base
import intake_xarray.base
from intake.compat import unpack_kwargs
import msgpack
import requests
from requests.compat import urljoin
import numpy
import os
import warnings
import xarray


def tail(filename, n=1, bsize=2048):
    """
    Returns a generator with the last n lines of a file.

    Thanks to Martijn Pieters for this solution:
    https://stackoverflow.com/a/12295054/6513183

    Parameters
    ----------
    filename : string
    n: int
        number of lines
    bsize: int
        seek step size
    Returns
    -------
    line : generator
    """

    # get newlines type, open in universal mode to find it
    with open(filename, 'r', newline=None) as hfile:
        if not hfile.readline():
            return  # empty, no point
        sep = hfile.newlines  # After reading a line, python gives us this
    assert isinstance(sep, str), 'multiple newline types found, aborting'

    # find a suitable seek position in binary mode
    with open(filename, 'rb') as hfile:
        hfile.seek(0, os.SEEK_END)
        linecount = 0
        pos = 0

        while linecount <= n + 1:
            # read at least n lines + 1 more; we need to skip a partial line later on
            try:
                hfile.seek(-bsize, os.SEEK_CUR)           # go backwards
                linecount += hfile.read(bsize).count(sep.encode())  # count newlines
                hfile.seek(-bsize, os.SEEK_CUR)           # go back again
            except IOError as e:
                if e.errno == errno.EINVAL:
                    # Attempted to seek past the start, can't go further
                    bsize = hfile.tell()
                    hfile.seek(0, os.SEEK_SET)
                    pos = 0
                    linecount += hfile.read(bsize).count(sep.encode())
                    break
                raise  # Some other I/O exception, re-raise
            pos = hfile.tell()

    # Re-open in text mode
    with open(filename, 'r') as hfile:
        hfile.seek(pos, os.SEEK_SET)  # our file position from above
        for line in hfile:
            # We've located n lines *or more*, so skip if needed
            if linecount > n:
                linecount -= 1
                continue
            # The rest we yield
            yield line.rstrip()


def to_event_pages(get_event_cursor, page_size):
    """
    Decorator that changes get_event_cursor to get_event_pages.

    get_event_cursor yields events, get_event_pages yields event_pages.

    Parameters
    ----------
    get_event_cursor : function

    Returns
    -------
    get_event_pages : function
    """
    @functools.wraps(get_event_cursor)
    def get_event_pages(*args, **kwargs):
        event_cursor = get_event_cursor(*args, **kwargs)
        while True:
            result = list(itertools.islice(event_cursor, page_size))
            if result:
                yield event_model.pack_event_page(*result)
            else:
                break
    return get_event_pages


def to_datum_pages(get_datum_cursor, page_size):
    """
    Decorator that changes get_datum_cursor to get_datum_pages.

    get_datum_cursor yields datum, get_datum_pages yields datum_pages.

    Parameters
    ----------
    get_datum_cursor : function

    Returns
    -------
    get_datum_pages : function
    """
    @functools.wraps(get_datum_cursor)
    def get_datum_pages(*args, **kwargs):
        datum_cursor = get_datum_cursor(*args, **kwargs)
        while True:
            result = list(itertools.islice(datum_cursor, page_size))
            if result:
                yield event_model.pack_datum_page(*result)
            else:
                break
    return get_datum_pages


def flatten_event_page_gen(gen):
    """
    Converts an event_page generator to an event generator.

    Parameters
    ----------
    gen : generator

    Returns
    -------
    event_generator : generator
    """
    for page in gen:
        yield from event_model.unpack_event_page(page)


def interlace_event_pages(*gens):
    """
    Take event_page generators and interlace their results by timestamp.
    This is a modification of https://github.com/bluesky/databroker/pull/378/

    Parameters
    ----------
    gens : generators
        Generators of (name, dict) pairs where the dict contains a 'time' key.
    Yields
    ------
    val : tuple
        The next (name, dict) pair in time order

    """
    iters = [iter(flatten_event_page_gen(g)) for g in gens]
    heap = []

    def safe_next(indx):
        try:
            val = next(iters[indx])
        except StopIteration:
            return
        heapq.heappush(heap, (val['time'], indx, val))
    for i in range(len(iters)):
        safe_next(i)
    while heap:
        _, indx, val = heapq.heappop(heap)
        yield val
        safe_next(indx)


def interlace_event_page_chunks(*gens, chunk_size):
    """
    Take event_page generators and interlace their results by timestamp.

    This is a modification of https://github.com/bluesky/databroker/pull/378/

    Parameters
    ----------
    gens : generators
        Generators of (name, dict) pairs where the dict contains a 'time' key.
    chunk_size : integer
        Size of pages to yield
    Yields
    ------
    val : tuple
        The next (name, dict) pair in time order

    """
    iters = [iter(event_model.rechunk_event_pages(g, chunk_size)) for g in gens]
    heap = []

    def safe_next(indx):
        try:
            val = next(iters[indx])
        except StopIteration:
            return
        heapq.heappush(heap, (val['time'][0], indx, val))
    for i in range(len(iters)):
        safe_next(i)
    while heap:
        _, indx, val = heapq.heappop(heap)
        yield val


def documents_to_xarray(*, start_doc, stop_doc, descriptor_docs,
                        get_event_pages, filler, get_resource,
                        lookup_resource_for_datum, get_datum_pages,
                        include=None, exclude=None):
    """
    Represent the data in one Event stream as an xarray.

    Parameters
    ----------
    start_doc: dict
        RunStart Document
    stop_doc : dict
        RunStop Document
    descriptor_docs : list
        EventDescriptor Documents
    filler : event_model.Filler
    get_resource : callable
        Expected signature ``get_resource(resource_uid) -> Resource``
    lookup_resource_for_datum : callable
        Expected signature ``lookup_resource_for_datum(datum_id) -> resource_uid``
    get_datum_pages : callable
        Expected signature ``get_datum_pages(resource_uid) -> generator``
        where ``generator`` yields datum_page documents
    get_event_pages : callable
        Expected signature ``get_event_pages(descriptor_uid) -> generator``
        where ``generator`` yields event_page documents
    include : list, optional
        Fields ('data keys') to include. By default all are included. This
        parameter is mutually exclusive with ``exclude``.
    exclude : list, optional
        Fields ('data keys') to exclude. By default none are excluded. This
        parameter is mutually exclusive with ``include``.

    Returns
    -------
    dataset : xarray.Dataset
    """
    if include is None:
        include = []
    if exclude is None:
        exclude = []
    if include and exclude:
        raise ValueError(
            "The parameters `include` and `exclude` are mutually exclusive.")

    # Data keys must not change within one stream, so we can safely sample
    # just the first Event Descriptor.
    if descriptor_docs:
        data_keys = descriptor_docs[0]['data_keys']
        if include:
            keys = list(set(data_keys) & set(include))
        elif exclude:
            keys = list(set(data_keys) - set(exclude))
        else:
            keys = list(data_keys)

    # Collect a Dataset for each descriptor. Merge at the end.
    datasets = []
    for descriptor in descriptor_docs:
        events = list(flatten_event_page_gen(get_event_pages(descriptor['uid'])))
        if not events:
            continue
        if any(data_keys[key].get('external') for key in keys):
            filler('descriptor', descriptor)
            for event in events:
                try:
                    filler('event', event)
                except event_model.UnresolvableForeignKeyError as err:
                    datum_id = err.key
                    resource_uid = lookup_resource_for_datum(datum_id)
                    resource = get_resource(resource_uid)
                    filler('resource', resource)
                    # Pre-fetch all datum for this resource.
                    for datum_page in get_datum_pages(resource_uid):
                        filler('datum_page', datum_page)
                    # TODO -- When to clear the datum cache in filler?
                    filler('event', event)
        times = [ev['time'] for ev in events]
        seq_nums = [ev['seq_num'] for ev in events]
        uids = [ev['uid'] for ev in events]
        data_table = _transpose(events, keys, 'data')
        # external_keys = [k for k in data_keys if 'external' in data_keys[k]]

        # Collect a DataArray for each field in Event, each field in
        # configuration, and 'seq_num'. The Event 'time' will be the
        # default coordinate.
        data_arrays = {}

        # Make DataArrays for Event data.
        for key in keys:
            field_metadata = data_keys[key]
            # Verify the actual ndim by looking at the data.
            ndim = numpy.asarray(data_table[key][0]).ndim
            dims = None
            if 'dims' in field_metadata:
                # As of this writing no Devices report dimension names ('dims')
                # but they could in the future.
                reported_ndim = len(field_metadata['dims'])
                if reported_ndim == ndim:
                    dims = tuple(field_metadata['dims'])
                else:
                    # TODO Warn
                    ...
            if dims is None:
                # Construct the same default dimension names xarray would.
                dims = tuple(f'dim_{i}' for i in range(ndim))
            data_arrays[key] = xarray.DataArray(
                data=data_table[key],
                dims=('time',) + dims,
                coords={'time': times},
                name=key)

        # Make DataArrays for configuration data.
        for object_name, config in descriptor.get('configuration', {}).items():
            data_keys = config['data_keys']
            # For configuration, label the dimension specially to
            # avoid key collisions.
            scoped_data_keys = {key: f'{object_name}:{key}'
                                for key in data_keys}
            if include:
                keys = {k: v for k, v in scoped_data_keys.items()
                        if v in include}
            elif exclude:
                keys = {k: v for k, v in scoped_data_keys.items()
                        if v not in include}
            else:
                keys = scoped_data_keys
            for key, scoped_key in keys.items():
                field_metadata = data_keys[key]
                # Verify the actual ndim by looking at the data.
                ndim = numpy.asarray(config['data'][key]).ndim
                dims = None
                if 'dims' in field_metadata:
                    # As of this writing no Devices report dimension names ('dims')
                    # but they could in the future.
                    reported_ndim = len(field_metadata['dims'])
                    if reported_ndim == ndim:
                        dims = tuple(field_metadata['dims'])
                    else:
                        # TODO Warn
                        ...
                if dims is None:
                    # Construct the same default dimension names xarray would.
                    dims = tuple(f'dim_{i}' for i in range(ndim))
                data_arrays[scoped_key] = xarray.DataArray(
                    # TODO Once we know we have one Event Descriptor
                    # per stream we can be more efficient about this.
                    data=numpy.tile(config['data'][key],
                                    (len(times),) + ndim * (1,)),
                    dims=('time',) + dims,
                    coords={'time': times},
                    name=key)

        # Finally, make DataArrays for 'seq_num' and 'uid'.
        data_arrays['seq_num'] = xarray.DataArray(
            data=seq_nums,
            dims=('time',),
            coords={'time': times},
            name='seq_num')
        data_arrays['uid'] = xarray.DataArray(
            data=uids,
            dims=('time',),
            coords={'time': times},
            name='uid')

        datasets.append(xarray.Dataset(data_vars=data_arrays))
    # Merge Datasets from all Event Descriptors into one representing the
    # whole stream. (In the future we may simplify to one Event Descriptor
    # per stream, but as of this writing we must account for the
    # possibility of multiple.)
    return xarray.merge(datasets)


class RemoteBlueskyRun(intake.catalog.base.RemoteCatalog):
    """
    Catalog representing one Run.

    This is a client-side proxy to a BlueskyRun stored on a remote server.

    Parameters
    ----------
    url: str
        Address of the server
    headers: dict
        HTTP headers to sue in calls
    name: str
        handle to reference this data
    parameters: dict
        To pass to the server when it instantiates the data source
    metadata: dict
        Additional info
    kwargs: ignored
    """
    name = 'bluesky-run'

    def __init__(self, url, http_args, name, parameters, metadata=None, **kwargs):
        super().__init__(url=url, http_args=http_args, name=name,
                         metadata=metadata)
        self.url = url
        self.name = name
        self.parameters = parameters
        self.http_args = http_args
        self._source_id = None
        self.metadata = metadata or {}
        self._get_source_id()
        self.bag = None

    def _get_source_id(self):
        if self._source_id is None:
            payload = dict(action='open', name=self.name,
                           parameters=self.parameters)
            req = requests.post(urljoin(self.url, '/v1/source'),
                                data=msgpack.packb(payload, use_bin_type=True),
                                **self.http_args)
            req.raise_for_status()
            response = msgpack.unpackb(req.content, **unpack_kwargs)
            self._parse_open_response(response)

    def _parse_open_response(self, response):
        self.npartitions = response['npartitions']
        self.metadata = response['metadata']
        self._schema = intake.source.base.Schema(
            datashape=None, dtype=None,
            shape=self.shape,
            npartitions=self.npartitions,
            metadata=self.metadata)
        self._source_id = response['source_id']

    def _load_metadata(self):
        if self.bag is None:
            self.raw_parts = [dask.delayed(intake.container.base.get_partition)(
                self.url, self.http_args, self._source_id, self.container, (i, True)
            )
                          for i in range(self.npartitions)]
            self.parts = [dask.delayed(intake.container.base.get_partition)(
                self.url, self.http_args, self._source_id, self.container, (i, False)
            )
                          for i in range(self.npartitions)]
            self.bag = dask.bag.from_delayed(self.parts)
        return self._schema

    def _get_partition(self, index):
        self._load_metadata()
        i, raw = index
        if raw:
            parts = self.raw_parts
        else:
            parts = self.parts
        return parts[i].compute()

    def read(self):
        raise NotImplementedError(
            "Reading the BlueskyRun itself is not supported. Instead read one "
            "its entries, representing individual Event Streams.")

    def to_dask(self):
        raise NotImplementedError(
            "Reading the BlueskyRun itself is not supported. Instead read one "
            "its entries, representing individual Event Streams.")

    def _close(self):
        self.bag = None

    def canonical(self):
        for i in range(self.npartitions):
            for name, doc in self._get_partition((i, False)):
                yield name, doc

    def read_canonical(self):
        warnings.warn(
            "The method read_canonical has been renamed canonical. This alias "
            "may be removed in a future release.")
        yield from self.canonical()

    def canonical_unfilled(self):
        for i in range(self.npartitions):
            for name, doc in self._get_partition((i, True)):
                yield name, doc

    def __repr__(self):
        self._load()
        try:
            start = self.metadata['start']
            stop = self.metadata['stop']
            out = (f"Run Catalog\n"
                   f"  uid={start['uid']!r}\n"
                   f"  exit_status={stop.get('exit_status')!r}\n"
                   f"  {_ft(start['time'])} -- {_ft(stop.get('time', '?'))}\n"
                   f"  Streams:\n")
            for stream_name in self:
                out += f"    * {stream_name}\n"
        except Exception as exc:
            out = f"<Intake catalog: Run *REPR_RENDERING_FAILURE* {exc!r}>"
        return out

    def search(self):
        raise NotImplementedError("Cannot search within one run.")


class BlueskyRun(intake.catalog.Catalog):
    """
    Catalog representing one Run.

    Parameters
    ----------
    get_run_start: callable
        Expected signature ``get_run_start() -> RunStart``
    get_run_stop : callable
        Expected signature ``get_run_stop() -> RunStop``
    get_event_descriptors : callable
        Expected signature ``get_event_descriptors() -> List[EventDescriptors]``
    get_event_pages : callable
        Expected signature ``get_event_pages(descriptor_uid) -> generator``
        where ``generator`` yields Event documents
    get_event_count : callable
        Expected signature ``get_event_count(descriptor_uid) -> int``
    get_resource : callable
        Expected signature ``get_resource(resource_uid) -> Resource``
    lookup_resource_for_datum : callable
        Expected signature ``lookup_resource_for_datum(datum_id) -> resource_uid``
    get_datum_pages : callable
        Expected signature ``get_datum_pages(resource_uid) -> generator``
        where ``generator`` yields Datum documents
    filler : event_model.Filler
    **kwargs :
        Additional keyword arguments are passed through to the base class,
        Catalog.
    """
    container = 'bluesky-run'
    version = '0.0.1'
    partition_access = True
    PARTITION_SIZE = 100

    def __init__(self,
                 get_run_start,
                 get_run_stop,
                 get_event_descriptors,
                 get_event_pages,
                 get_event_count,
                 get_resource,
                 lookup_resource_for_datum,
                 get_datum_pages,
                 filler,
                 **kwargs):
        # All **kwargs are passed up to base class. TODO: spell them out
        # explicitly.
        self.urlpath = ''  # TODO Not sure why I had to add this.

        self._get_run_start = get_run_start
        self._get_run_stop = get_run_stop
        self._get_event_descriptors = get_event_descriptors
        self._get_event_pages = get_event_pages
        self._get_event_count = get_event_count
        self._get_resource = get_resource
        self._lookup_resource_for_datum = lookup_resource_for_datum
        self._get_datum_pages = get_datum_pages
        self.filler = filler
        super().__init__(**kwargs)

    def __repr__(self):
        try:
            start = self._run_start_doc
            stop = self._run_stop_doc or {}
            out = (f"Run Catalog\n"
                   f"  uid={start['uid']!r}\n"
                   f"  exit_status={stop.get('exit_status')!r}\n"
                   f"  {_ft(start['time'])} -- {_ft(stop.get('time', '?'))}\n"
                   f"  Streams:\n")
            for stream_name in self:
                out += f"    * {stream_name}\n"
        except Exception as exc:
            out = f"<Intake catalog: Run *REPR_RENDERING_FAILURE* {exc!r}>"
        return out

    def _load(self):
        # Count the total number of documents in this run.
        self._run_stop_doc = self._get_run_stop()
        self._run_start_doc = self._get_run_start()
        self._descriptors = self._get_event_descriptors()
        self._offset = len(self._descriptors) + 1
        self.metadata.update({'start': self._run_start_doc})
        self.metadata.update({'stop': self._run_stop_doc})

        count = 1
        descriptor_uids = [doc['uid'] for doc in self._descriptors]
        count += len(descriptor_uids)
        for doc in self._descriptors:
            count += self._get_event_count(doc['uid'])
        count += (self._run_stop_doc is not None)
        self.npartitions = int(numpy.ceil(count / self.PARTITION_SIZE))

        self._schema = intake.source.base.Schema(
            datashape=None,
            dtype=None,
            shape=(count,),
            npartitions=self.npartitions,
            metadata=self.metadata)

        # Make a BlueskyEventStream for each stream_name.
        for doc in self._descriptors:
            if 'name' not in doc:
                warnings.warn(
                    f"EventDescriptor {doc['uid']!r} has no 'name', likely "
                    f"because it was generated using an old version of "
                    f"bluesky. The name 'primary' will be used.")
        descriptors_by_name = collections.defaultdict(list)
        for doc in self._descriptors:
            descriptors_by_name[doc.get('name', 'primary')].append(doc)
        for stream_name, descriptors in descriptors_by_name.items():
            args = dict(
                get_run_start=self._get_run_start,
                stream_name=stream_name,
                get_run_stop=self._get_run_stop,
                get_event_descriptors=self._get_event_descriptors,
                get_event_pages=self._get_event_pages,
                get_event_count=self._get_event_count,
                get_resource=self._get_resource,
                lookup_resource_for_datum=self._lookup_resource_for_datum,
                get_datum_pages=self._get_datum_pages,
                filler=self.filler,
                metadata={'descriptors': descriptors})
            self._entries[stream_name] = intake.catalog.local.LocalCatalogEntry(
                name=stream_name,
                description={},  # TODO
                driver='intake_bluesky.core.BlueskyEventStream',
                direct_access='forbid',
                args=args,
                cache=None,  # ???
                metadata={'descriptors': descriptors},
                catalog_dir=None,
                getenv=True,
                getshell=True,
                catalog=self)

    def read_canonical(self):
        warnings.warn(
            "The method read_canonical has been renamed canonical. This alias "
            "may be removed in a future release.")
        yield from self.canonical()

    def canonical(self):
        for i in range(self.npartitions):
            for name, doc in self.read_partition((i, False)):
                yield name, doc

    def canonical_unfilled(self):
        for i in range(self.npartitions):
            for name, doc in self.read_partition((i, True)):
                yield name, doc

    def read_partition_unfilled(self, i):
        """Fetch one chunk of documents.
        """
        self._load()
        payload = []
        start = i * self.PARTITION_SIZE
        stop = (1 + i) * self.PARTITION_SIZE
        if start < self._offset:
            payload.extend(
                itertools.islice(
                    itertools.chain(
                        (('start', self._get_run_start()),),
                        (('descriptor', doc) for doc in self._descriptors)),
                    start,
                    stop))
        descriptor_uids = [doc['uid'] for doc in self._descriptors]
        skip = max(0, start - len(payload))
        limit = stop - start - len(payload)
        # print('start, stop, skip, limit', start, stop, skip, limit)
        datum_ids = set()
        if limit > 0:

            events = itertools.islice(interlace_event_pages(
                    *(self._get_event_pages(descriptor_uid=descriptor_uid)
                      for descriptor_uid in descriptor_uids)), skip, limit)

            for event in events:
                for key, is_filled in event['filled'].items():
                    if not is_filled:
                        datum_id = event['data'][key]
                        if datum_id not in datum_ids:
                            if '/' in datum_id:
                                resource_uid, _ = datum_id.split('/', 1)
                            else:
                                resource_uid = self._lookup_resource_for_datum(datum_id)
                            resource = self._get_resource(uid=resource_uid)
                            payload.append(('resource', resource))
                            for datum_page in self._get_datum_pages(resource_uid):
                                # TODO Greedily cache but lazily emit.
                                payload.append(('datum_page', datum_page))
                                datum_ids |= set(datum_page['datum_id'])
                payload.append(('event', event))
            if i == self.npartitions - 1 and self._run_stop_doc is not None:
                payload.append(('stop', self._run_stop_doc))
        for _, doc in payload:
            doc.pop('_id', None)
        return payload

    def read_partition(self, index):
        """Fetch one chunk of documents.
        """
        i, raw = index
        if raw:
            return self.read_partition_unfilled(i)
        self._load()
        payload = []
        start = i * self.PARTITION_SIZE
        stop = (1 + i) * self.PARTITION_SIZE
        if start < self._offset:
            payload.extend(
                itertools.islice(
                    itertools.chain(
                        (('start', self._get_run_start()),),
                        (('descriptor', doc) for doc in self._descriptors)),
                    start,
                    stop))
        descriptor_uids = [doc['uid'] for doc in self._descriptors]
        skip = max(0, start - len(payload))
        limit = stop - start - len(payload)
        if limit > 0:

            events = itertools.islice(interlace_event_pages(
                    *(self._get_event_pages(descriptor_uid=descriptor_uid)
                      for descriptor_uid in descriptor_uids)), skip, limit)

            for descriptor in self._descriptors:
                self.filler('descriptor', descriptor)
            for event in events:
                self._fill(event)  # in place (for now)
                payload.append(('event', event))
            if i == self.npartitions - 1 and self._run_stop_doc is not None:
                payload.append(('stop', self._run_stop_doc))
        for _, doc in payload:
            doc.pop('_id', None)
        return payload

    def _fill(self, event, last_datum_id=None):
        try:
            self.filler('event', event)
        except event_model.UnresolvableForeignKeyError as err:
            datum_id = err.key
            if datum_id == last_datum_id:
                # We tried to fetch this Datum on the last trip
                # trip through this method, and apparently it did not
                # work. We are in an infinite loop. Bail!
                raise

            if '/' in datum_id:
                resource_uid, _ = datum_id.split('/', 1)
            else:
                resource_uid = self._lookup_resource_for_datum(datum_id)

            resource = self._get_resource(uid=resource_uid)
            self.filler('resource', resource)
            # Pre-fetch all datum for this resource.
            for datum_page in self._get_datum_pages(
                    resource_uid=resource_uid):
                self.filler('datum_page', datum_page)
            # TODO -- When to clear the datum cache in filler?

            # Re-enter and try again now that the Filler has consumed the
            # missing Datum. There might be another missing Datum in this same
            # Event document (hence this re-entrant structure) or might be good
            # to go.
            self._fill(event, last_datum_id=datum_id)

    def read(self):
        raise NotImplementedError(
            "Reading the BlueskyRun itself is not supported. Instead read one "
            "its entries, representing individual Event Streams.")

    def to_dask(self):
        raise NotImplementedError(
            "Reading the BlueskyRun itself is not supported. Instead read one "
            "its entries, representing individual Event Streams.")


class BlueskyEventStream(intake_xarray.base.DataSourceMixin):
    """
    Catalog representing one Event Stream from one Run.

    Parameters
    ----------
    get_run_start: callable
        Expected signature ``get_run_start() -> RunStart``
    stream_name : string
        Stream name, such as 'primary'.
    get_run_stop : callable
        Expected signature ``get_run_stop() -> RunStop``
    get_event_descriptors : callable
        Expected signature ``get_event_descriptors() -> List[EventDescriptors]``
    get_event_pages : callable
        Expected signature ``get_event_pages(descriptor_uid) -> generator``
        where ``generator`` yields event_page documents
    get_event_count : callable
        Expected signature ``get_event_count(descriptor_uid) -> int``
    get_resource : callable
        Expected signature ``get_resource(resource_uid) -> Resource``
    lookup_resource_for_datum : callable
        Expected signature ``lookup_resource_for_datum(datum_id) -> resource_uid``
    get_datum_pages : callable
        Expected signature ``get_datum_pages(resource_uid) -> generator``
        where ``generator`` yields datum_page documents
    filler : event_model.Filler
    metadata : dict
        passed through to base class
    include : list, optional
        Fields ('data keys') to include. By default all are included. This
        parameter is mutually exclusive with ``exclude``.
    exclude : list, optional
        Fields ('data keys') to exclude. By default none are excluded. This
        parameter is mutually exclusive with ``include``.
    **kwargs :
        Additional keyword arguments are passed through to the base class.
    """
    container = 'xarray'
    name = 'bluesky-event-stream'
    version = '0.0.1'
    partition_access = True

    def __init__(self,
                 get_run_start,
                 stream_name,
                 get_run_stop,
                 get_event_descriptors,
                 get_event_pages,
                 get_event_count,
                 get_resource,
                 lookup_resource_for_datum,
                 get_datum_pages,
                 filler,
                 metadata,
                 include=None,
                 exclude=None,
                 **kwargs):
        # self._partition_size = 10
        # self._default_chunks = 10
        self._get_run_start = get_run_start
        self._stream_name = stream_name
        self._get_event_descriptors = get_event_descriptors
        self._get_run_stop = get_run_stop
        self._get_event_pages = get_event_pages
        self._get_event_count = get_event_count
        self._get_resource = get_resource
        self._lookup_resource_for_datum = lookup_resource_for_datum
        self._get_datum_pages = get_datum_pages
        self.filler = filler
        self.urlpath = ''  # TODO Not sure why I had to add this.
        self._ds = None  # set by _open_dataset below
        self.include = include
        self.exclude = exclude
        super().__init__(metadata=metadata)

    def __repr__(self):
        try:
            out = (f"<Intake catalog: Stream {self._stream_name!r} "
                   f"from Run {self._get_run_start()['uid'][:8]}...>")
        except Exception as exc:
            out = f"<Intake catalog: Stream *REPR_RENDERING_FAILURE* {exc!r}>"
        return out

    def _open_dataset(self):
        self._run_stop_doc = self._get_run_stop()
        self._run_start_doc = self._get_run_start()
        self.metadata.update({'start': self._run_start_doc})
        self.metadata.update({'stop': self._run_stop_doc})
        descriptor_docs = [doc for doc in self._get_event_descriptors()
                           if doc.get('name') == self._stream_name]
        self._ds = documents_to_xarray(
            start_doc=self._run_start_doc,
            stop_doc=self._run_stop_doc,
            descriptor_docs=descriptor_docs,
            get_event_pages=self._get_event_pages,
            filler=self.filler,
            get_resource=self._get_resource,
            lookup_resource_for_datum=self._lookup_resource_for_datum,
            get_datum_pages=self._get_datum_pages,
            include=self.include,
            exclude=self.exclude)


class DocumentCache(event_model.DocumentRouter):
    def __init__(self):
        self.descriptors = {}
        self.resources = {}
        self.event_pages = collections.defaultdict(list)
        self.datum_pages_by_resource = collections.defaultdict(list)
        self.resource_uid_by_datum_id = {}
        self.start_doc = None
        self.stop_doc = None

    def start(self, doc):
        self.start_doc = doc

    def stop(self, doc):
        self.stop_doc = doc

    def event_page(self, doc):
        self.event_pages[doc['descriptor']].append(doc)

    def datum_page(self, doc):
        self.datum_pages_by_resource[doc['resource']].append(doc)
        for datum_id in doc['datum_id']:
            self.resource_uid_by_datum_id[datum_id] = doc['resource']

    def descriptor(self, doc):
        self.descriptors[doc['uid']] = doc

    def resource(self, doc):
        self.resources[doc['uid']] = doc


class BlueskyRunFromGenerator(BlueskyRun):

    def __init__(self, gen_func, gen_args, gen_kwargs, filler=None, **kwargs):

        if filler is None:
            filler = event_model.Filler({}, inplace=True)

        document_cache = DocumentCache()

        for item in gen_func(*gen_args, **gen_kwargs):
            document_cache(*item)

        assert document_cache.start_doc is not None

        def get_run_start():
            return document_cache.start_doc

        def get_run_stop():
            return document_cache.stop_doc

        def get_event_descriptors():
            return document_cache.descriptors.values()

        def get_event_pages(descriptor_uid, skip=0, limit=None):
            if skip != 0 and limit is not None:
                raise NotImplementedError
            return document_cache.event_pages[descriptor_uid]

        def get_event_count(descriptor_uid):
            return sum(len(page['seq_num'])
                       for page in (document_cache.event_pages[descriptor_uid]))

        def get_resource(uid):
            return document_cache.resources[uid]

        def lookup_resource_for_datum(datum_id):
            return document_cache.resource_uid_by_datum_id[datum_id]

        def get_datum_pages(resource_uid, skip=0, limit=None):
            if skip != 0 and limit is not None:
                raise NotImplementedError
            return document_cache.datum_pages_by_resource[resource_uid]

        super().__init__(
            get_run_start=get_run_start,
            get_run_stop=get_run_stop,
            get_event_descriptors=get_event_descriptors,
            get_event_pages=get_event_pages,
            get_event_count=get_event_count,
            get_resource=get_resource,
            lookup_resource_for_datum=lookup_resource_for_datum,
            get_datum_pages=get_datum_pages,
            filler=filler,
            **kwargs)


def _transpose(in_data, keys, field):
    """Turn a list of dicts into dict of lists

    Parameters
    ----------
    in_data : list
        A list of dicts which contain at least one dict.
        All of the inner dicts must have at least the keys
        in `keys`

    keys : list
        The list of keys to extract

    field : str
        The field in the outer dict to use

    Returns
    -------
    transpose : dict
        The transpose of the data
    """
    out = {k: [None] * len(in_data) for k in keys}
    for j, ev in enumerate(in_data):
        dd = ev[field]
        for k in keys:
            out[k][j] = dd[k]
    return out


def _ft(timestamp):
    "format timestamp"
    if isinstance(timestamp, str):
        return timestamp
    # Truncate microseconds to miliseconds. Do not bother to round.
    return (datetime.fromtimestamp(timestamp)
            .strftime('%Y-%m-%d %H:%M:%S.%f'))[:-3]


def xarray_to_event_gen(data_xarr, ts_xarr, page_size):
    for start_idx in range(0, len(data_xarr['time']), page_size):
        stop_idx = start_idx + page_size
        data = {name: variable.values
                for name, variable in
                data_xarr.isel({'time': slice(start_idx, stop_idx)}).items()
                if ':' not in name}
        ts = {name: variable.values
              for name, variable in
              ts_xarr.isel({'time': slice(start_idx, stop_idx)}).items()
              if ':' not in name}
        event_page = {}
        seq_num = data.pop('seq_num')
        ts.pop('seq_num')
        uids = data.pop('uid')
        ts.pop('uid')
        event_page['data'] = data
        event_page['timestamps'] = ts
        event_page['time'] = data_xarr['time'][start_idx:stop_idx].values
        event_page['uid'] = uids
        event_page['seq_num'] = seq_num
        event_page['filled'] = {}

        yield event_page


def parse_handler_registry(handler_registry):
    """
    Parse mapping of spec name to 'import path' into mapping to class itself.

    Parameters
    ----------
    handler_registry : dict
        Values may be string 'import paths' to classes or actual classes.

    Examples
    --------
    Pass in name; get back actual class.

    >>> parse_handler_registry({'my_spec': 'package.module.ClassName'})
    {'my_spec': <package.module.ClassName>}

    """
    result = {}
    for spec, handler_str in handler_registry.items():
        if isinstance(handler_str, str):
            module_name, _, class_name = handler_str.rpartition('.')
            class_ = getattr(importlib.import_module(module_name), class_name)
        else:
            class_ = handler_str
        result[spec] = class_
    return result


intake.registry['remote-bluesky-run'] = RemoteBlueskyRun
intake.container.container_map['bluesky-run'] = RemoteBlueskyRun


def concat_dataarray_pages(dataarray_pages):
    """
    Combines a iterable of dataarray_pages to a single dataarray_page.

    Parameters
    ----------
    dataarray_pages: Iterabile
        An iterable of event_pages with xarray.dataArrays in the data,
        timestamp, and filled fields.
    Returns
    ------
    event_page : dict
        A single event_pages with xarray.dataArrays in the data,
        timestamp, and filled fields.
    """
    pages = list(dataarray_pages)
    if len(pages) == 1:
        return pages[0]

    array_keys = ['seq_num', 'time', 'uid']
    data_keys = dataarray_pages[0]['data'].keys()

    return {'descriptor': pages[0]['descriptor'],
            **{key: list(itertools.chain.from_iterable(
                    [page[key] for page in pages])) for key in array_keys},
            'data': {key: xarray.concat([page['data'][key] for page in pages])
                     for key in data_keys},
            'timestamps': {key: xarray.concat([page['timestamps'][key] for page in pages])
                           for key in data_keys},
            'filled': {key: xarray.concat([page['filled'][key] for page in pages])
                       for key in data_keys}}


def event_page_to_dataarray_page(event_page, dims=None, coords=None):
    """
    Converts the event_page's data, timestamps, and filled to xarray.DataArray.

    Parameters
    ----------
    event_page: dict
        A EventPage document
    dims: tuple
        Tuple of dimension names associated with the array
    coords: dict-like
        Dictionary-like container of coordinate arrays
    Returns
    ------
    event_page : dict
        An event_pages with xarray.dataArrays in the data,
        timestamp, and filled fields.
    """
    if coords is None:
        coords = {'time': event_page['time']}
    if dims is None:
        dims = ('time',)

    array_keys = ['seq_num', 'time', 'uid']
    data_keys = event_page['data'].keys()

    return {'descriptor': event_page['descriptor'],
            **{key: event_page[key] for key in array_keys},
            'data': {key: xarray.DataArray(
                            event_page['data'][key], dims=dims, coords=coords, name=key)
                     for key in data_keys},
            'timestamps': {key: xarray.DataArray(
                            event_page['timestamps'][key], dims=dims, coords=coords, name=key)
                           for key in data_keys},
            'filled': {key: xarray.DataArray(
                            event_page['filled'][key], dims=dims, coords=coords, name=key)
                       for key in data_keys}}


def dataarray_page_to_dataset_page(dataarray_page):

    """
    Converts the dataarray_page's data, timestamps, and filled to xarray.DataSet.

    Parameters
    ----------
    dataarray_page: dict
    Returns
    ------
    dataset_page : dict
    """
    array_keys = ['seq_num', 'time', 'uid']

    return {'descriptor': dataarray_page['descriptor'],
            **{key: dataarray_page[key] for key in array_keys},
            'data': xarray.merge(dataarray_page['data'].values()),
            'timestamps': xarray.merge(dataarray_page['timestamps'].values()),
            'filled': xarray.merge(dataarray_page['filled'].values())}


class DaskFiller(event_model.Filler):

    def __init__(self, *args, inplace=False, **kwargs):
        if inplace:
            raise NotImplementedError("DaskFiller inplace is not supported.")
        # The DaskFiller will not mutate documents pass in by the user, but it
        # will ask the base class to mutate *internal* copies in place, so we
        # set inplace=True here, even though the user documents will never be
        # modified in place.
        super().__init__(*args, inplace=True, **kwargs)

    def event_page(self, doc):

        @dask.delayed
        def delayed_fill(event_page, key):
            self.fill_event_page(event_page, include=key)
            return numpy.asarray(event_page['data'][key])

        descriptor = self._descriptor_cache[doc['descriptor']]
        needs_filling = {key for key, val in descriptor['data_keys'].items()
                         if 'external' in val}
        filled_doc = copy.deepcopy(doc)

        for key in needs_filling:
            shape = extract_shape(descriptor, key)
            dtype = extract_dtype(descriptor, key)
            filled_doc['data'][key] = array.from_delayed(
                delayed_fill(filled_doc, key), shape=shape, dtype=dtype)
        return filled_doc

    def event(self, doc):

        @dask.delayed
        def delayed_fill(event, key):
            self.fill_event(event, include=key)
            return numpy.asarray(event['data'][key])

        descriptor = self._descriptor_cache[doc['descriptor']]
        needs_filling = {key for key, val in descriptor['data_keys'].items()
                         if 'external' in val}
        filled_doc = copy.deepcopy(doc)

        for key in needs_filling:
            shape = extract_shape(descriptor, key)
            dtype = extract_dtype(descriptor, key)
            filled_doc['data'][key] = array.from_delayed(
                delayed_fill(filled_doc, key), shape=shape, dtype=dtype)
        return filled_doc


def extract_shape(descriptor, key):
    """
    Work around bug in https://github.com/bluesky/ophyd/pull/746
    """
    # Ideally this code would just be
    # descriptor['data_keys'][key]['shape']
    # but we have to do some heuristics to make up for errors in the reporting.

    # Broken ophyd reports (x, y, 0). We want (num_images, y, x).
    data_key = descriptor['data_keys'][key]
    if len(data_key['shape']) == 3 and data_key['shape'][-1] == 0:
        object_keys = descriptor.get('object_keys', {})
        for object_name, data_keys in object_keys.items():
            if key in data_keys:
                break
        else:
            raise RuntimeError(f"Could not figure out shape of {key}")
        num_images = descriptor['configuration'][object_name]['data'].get('num_images', -1)
        x, y, _ = data_key['shape']
        shape = (num_images, y, x)
    else:
        shape = descriptor['data_keys'][key]['shape']
    return shape


def extract_dtype(descriptor, key):
    """
    Work around the fact that we currently report jsonschema data types.
    """
    reported = descriptor['data_keys'][key]['dtype']
    if reported == 'array':
        return float  # guess!
    else:
        return reported

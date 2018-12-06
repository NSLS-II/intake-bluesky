import collections
from datetime import datetime
import intake.catalog
import intake.catalog.base
import intake.catalog.local
import intake.catalog.remote
import intake.source.base
import intake_xarray.base
import itertools
import msgpack
import numpy
import pandas
import pymongo
import pymongo.errors
import requests
from requests.compat import urljoin, urlparse
import time
import xarray


class FacilityCatalog(intake.catalog.Catalog):
    "spans multiple MongoDB instances"
    ...


class MongoMetadataStoreCatalog(intake.catalog.Catalog):
    "represents one MongoDB instance"
    name = 'mongo_metadatastore'

    def __init__(self, uri, *, query=None, **kwargs):
        """
        A Catalog backed by a MongoDB with metadatastore v1 collections.

        Parameters
        ----------
        uri : string
            This URI must include a database name.
            Example: ``mongodb://localhost:27107/database_name``.
        query : dict
            Mongo query. This is used internally by the ``search`` method.
        **kwargs :
            passed up to the Catalog base class
        """
        # All **kwargs are passed up to base class. TODO: spell them out
        # explicitly.
        self._uri = uri
        self._query = query
        self._client = pymongo.MongoClient(uri)
        kwargs.setdefault('name', uri)
        try:
            # Called with no args, get_database() returns the database
            # specified in the uri --- or raises if there was none. There is no
            # public method for checking this in advance, so we just catch the
            # error.
            db = self._client.get_database()
        except pymongo.errors.ConfigurationError as err:
            raise ValueError(
                "Invalid uri. Did you forget to include a database?") from err
        self._run_start_collection = db.get_collection('run_start')
        self._run_stop_collection = db.get_collection('run_stop')
        self._event_descriptor_collection = db.get_collection('event_descriptor')
        self._event_collection = db.get_collection('event')
        self._query = query or {}
        super().__init__(**kwargs)
        if self.metadata is None:
            self.metadata = {}

        catalog = self

        class Entries:
            "Mock the dict interface around a MongoDB query result."
            def _doc_to_entry(self, run_start_doc):
                uid = run_start_doc['uid']
                run_stop_doc = catalog._run_stop_collection.find_one({'run_start': uid})
                if run_stop_doc is not None:
                    del run_stop_doc['_id']  # Drop internal Mongo detail.
                entry_metadata = {'start': run_start_doc,
                                  'stop': run_stop_doc}
                args = dict(
                    run_start_doc=run_start_doc,
                    run_stop_collection=catalog._run_stop_collection,
                    event_descriptor_collection=catalog._event_descriptor_collection,
                    event_collection=catalog._event_collection)
                return intake.catalog.local.LocalCatalogEntry(
                    name=run_start_doc['uid'],
                    description={},  # TODO
                    driver='intake_bluesky.RunCatalog',
                    direct_access='forbid',  # ???
                    args=args,
                    cache=None,  # ???
                    parameters={},
                    metadata=entry_metadata,
                    catalog_dir=None,
                    getenv=True,
                    getshell=True,
                    catalog=catalog)

            def __iter__(self):
                yield from self.keys()

            def keys(self):
                cursor = catalog._run_start_collection.find(
                    catalog._query, sort=[('time', pymongo.DESCENDING)])
                for run_start_doc in cursor:
                    yield run_start_doc['uid']

            def values(self):
                cursor = catalog._run_start_collection.find(
                    catalog._query, sort=[('time', pymongo.DESCENDING)])
                for run_start_doc in cursor:
                    del run_start_doc['_id']  # Drop internal Mongo detail.
                    yield self._doc_to_entry(run_start_doc)

            def items(self):
                cursor = catalog._run_start_collection.find(
                    catalog._query, sort=[('time', pymongo.DESCENDING)])
                for run_start_doc in cursor:
                    del run_start_doc['_id']  # Drop internal Mongo detail.
                    yield run_start_doc['uid'], self._doc_to_entry(run_start_doc)

            def __getitem__(self, name):
                # If this came from a client, we might be getting '-1'.
                try:
                    name = int(name)
                except ValueError:
                    pass
                if isinstance(name, int):
                    if name < 0:
                        # Interpret negative N as "the Nth from last entry".
                        query = catalog._query
                        cursor = (catalog._run_start_collection.find(query)
                                .sort('time', pymongo.DESCENDING) .limit(name))
                        *_, run_start_doc = cursor
                    else:
                        # Interpret positive N as
                        # "most recent entry with scan_id == N".
                        query = {'$and': [catalog._query, {'scan_id': name}]}
                        cursor = (catalog._run_start_collection.find(query)
                                .sort('time', pymongo.DESCENDING)
                                .limit(1))
                        run_start_doc, = cursor
                else:
                    query = {'$and': [catalog._query, {'uid': name}]}
                    run_start_doc = catalog._run_start_collection.find_one(query)
                if run_start_doc is None:
                    raise KeyError(name)
                del run_start_doc['_id']  # Drop internal Mongo detail.
                return self._doc_to_entry(run_start_doc)

        self._entries = Entries()
        self._schema = {}  # TODO This is cheating, I think.

    def _close(self):
        self._client.close()

    def search(self, query):
        """
        Return a new Catalog with a subset of the entries in this Catalog.

        Parameters
        ----------
        query : dict
            MongoDB query.
        """
        if self._query:
            query = {'$and': [self._query, query]}
        cat = MongoMetadataStoreCatalog(
            uri=self._uri,
            query=query,
            getenv=self.getenv,
            getshell=self.getshell,
            auth=self.auth,
            metadata=(self.metadata or {}).copy(),
            storage_options=self.storage_options)
        cat.metadata['search'] = {'query': query, 'upstream': self.name}
        return cat


class RunCatalog(intake.catalog.Catalog):
    "represents one Run"
    container = 'catalog'
    name = 'run'
    version = '0.0.1'
    partition_access = True

    def __init__(self,
                 run_start_doc,
                 run_stop_collection,
                 event_descriptor_collection,
                 event_collection,
                 **kwargs):
        # All **kwargs are passed up to base class. TODO: spell them out
        # explicitly.
        self.urlpath = ''  # TODO Not sure why I had to add this.

        self._run_start_doc = run_start_doc
        self._run_stop_doc = None  # loaded in _load below
        self._run_stop_collection = run_stop_collection
        self._event_descriptor_collection = event_descriptor_collection
        self._event_collection = event_collection

        super().__init__(**kwargs)
        self._schema = {}  # TODO This is cheating, I think.

    def __repr__(self):
        try:
            start = self._run_start_doc
            stop = self._run_stop_doc or {}
            out = (f"<Intake catalog: Run {start['uid'][:8]}...>\n"
                   f"  {_ft(start['time'])} -- {_ft(stop.get('time', '?'))}\n")
                   # f"  Streams:\n")
            # for stream_name in self:
            #     out += f"    * {stream_name}\n"
        except Exception as exc:
            out = f"<Intake catalog: Run *REPR_RENDERING_FAILURE* {exc}>"
        return out

    def _load(self):
        uid = self._run_start_doc['uid']
        run_stop_doc = self._run_stop_collection.find_one({'run_start': uid})
        self._run_stop_doc = run_stop_doc
        if run_stop_doc is not None:
            del run_stop_doc['_id']  # Drop internal Mongo detail.
        self.metadata.update({'start': self._run_start_doc})
        self.metadata.update({'stop': run_stop_doc})

        cursor = self._event_descriptor_collection.find(
            {'run_start': uid},
            sort=[('time', pymongo.ASCENDING)])
        # Sort descriptors like
        # {'stream_name': [descriptor1, descriptor2, ...], ...}
        streams = itertools.groupby(cursor,
                                    key=lambda d: d.get('name'))

        # Make a MongoEventStreamSource for each stream_name.
        for stream_name, event_descriptor_docs in streams:
            args = {'run_start_doc': self._run_start_doc,
                    'event_descriptor_docs': list(event_descriptor_docs),
                    'event_collection': self._event_collection,
                    'run_stop_collection': self._run_stop_collection}
            self._entries[stream_name] = intake.catalog.local.LocalCatalogEntry(
                name=stream_name,
                description={},  # TODO
                driver='intake_bluesky.MongoEventStream',
                direct_access='forbid',  # ???
                args=args,
                cache=None,  # ???
                parameters={},
                metadata=self.metadata,
                catalog_dir=None,
                getenv=True,
                getshell=True,
                catalog=self)


class MongoEventStream(intake_xarray.base.DataSourceMixin):
    container = 'xarray'
    name = 'event-stream'
    version = '0.0.1'
    partition_access = True

    def __init__(self, run_start_doc, event_descriptor_docs, event_collection,
                 run_stop_collection, metadata=None):
        # self._partition_size = 10
        # self._default_chunks = 10
        self.urlpath = ''  # TODO Not sure why I had to add this.
        self._run_start_doc = run_start_doc
        self._run_stop_doc  = None
        self._event_collection = event_collection
        self._event_descriptor_docs = event_descriptor_docs
        self._stream_name = event_descriptor_docs[0].get('name')
        self._run_stop_collection = run_stop_collection
        self._ds = None  # set by _open_dataset below
        super().__init__(
            metadata=metadata
        )
        if self.metadata is None:
            self.metadata = {}

    def __repr__(self):
        try:
            out = (f"<Intake catalog: Stream {self._stream_name!r} "
                   f"from Run {self._run_start_doc['uid'][:8]}...>")
        except Exception as exc:
            out = f"<Intake catalog: Stream *REPR_RENDERING_FAILURE* {exc}>"
        return out

    def _open_dataset(self):
        uid = self._run_start_doc['uid']
        run_stop_doc = self._run_stop_collection.find_one({'run_start': uid})
        self._run_stop_doc = run_stop_doc
        if run_stop_doc is not None:
            del run_stop_doc['_id']  # Drop internal Mongo detail.
        self.metadata.update({'start': self._run_start_doc})
        self.metadata.update({'stop': run_stop_doc})
        # TODO pass this in from BlueskyEntry
        slice_ = slice(None)
        include = []
        exclude = []

        if isinstance(slice_, collections.Iterable):
            first_axis = slice_[0]
        else:
            first_axis = slice_
        seq_num_filter = {}
        if first_axis.start is not None:
            seq_num_filter['$gte'] = first_axis.start
        if first_axis.stop is not None:
            seq_num_filter['$lt'] = first_axis.stop
        if first_axis.step is not None:
            # Have to think about how this interacts with repeated seq_num.
            raise NotImplementedError

        # Data keys must not change within one stream, so we can safely sample
        # just the first Event Descriptor.
        data_keys = self._event_descriptor_docs[0]['data_keys']
        if include:
            keys = list(set(data_keys) & set(include))
        elif exclude:
            keys = list(set(data_keys) - set(exclude))
        else:
            keys = list(data_keys)

        # Collect a Dataset for each descriptor. Merge at the end.
        datasets = []
        for descriptor in self._event_descriptor_docs:
            # Fetch (relevant range of) Event data and transpose rows -> cols.
            query = {'descriptor': descriptor['uid']}
            if seq_num_filter:
                query['seq_num'] = seq_num_filter
            cursor = self._event_collection.find(
                query,
                sort=[('descriptor', pymongo.DESCENDING),
                    ('time', pymongo.ASCENDING)])
            events = list(cursor)
            times = [ev['time'] for ev in events]
            seq_nums = [ev['seq_num'] for ev in events]
            data_table = _transpose(events, keys, 'data')
            # timestamps_table = _transpose(all_events, keys, 'timestamps')
            # external_keys = [k for k in data_keys if 'external' in data_keys[k]]

            # Collect a DataArray for each field in Event, each field in
            # configuration, and 'seq_num'. The Event 'time' will be the
            # default coordinate.
            data_arrays = {}

            # Make DataArrays for Event data.
            for key in keys:
                if data_keys[key].get('external'):
                    # Modify data_table in place to replace datum_ids with
                    # actual data.
                    actual_data = []
                    datum_documents = []
                    for datum_id in data_table[key]:
                        datum_documents.append(get_datum(datum_id))
                    for datum_document in datum_documents:
                        resource = get_resource(datum_id)
                        catalog = get_catalog(resource)
                        datum_data = catalog[datum_id]
                        actual_data.append(datum_data)
                data_table[key] = actual_data
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
                else:
                    data_arrays[key] = xarray.DataArray(
                        data=data_table[key],
                        dims=('time',) + dims,
                        coords={'time': times},
                        name=key)

            # Make DataArrays for configuration data.
            for object_name, config in descriptor['configuration'].items():
                data_keys = config['data_keys']
                if include:
                    keys = list(set(data_keys) & set(include))
                elif exclude:
                    keys = list(set(data_keys) - set(exclude))
                else:
                    keys = list(data_keys)
                for key in keys:
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
                    if data_keys[key].get('external'):
                        raise NotImplementedError
                    else:
                        # For configuration, label the dimension specially to
                        # avoid key collisions.
                        dim = f'{object_name}:{key}'
                        data_arrays[dim] = xarray.DataArray(
                            # TODO Once we know we have one Event Descriptor
                            # per stream we can be more efficient about this.
                            data=numpy.tile(config['data'][key],
                                            (len(times),) + ndim * (1,)),
                            dims=('time',) + dims,
                            coords={'time': times},
                            name=key)

            # Finally, make a DataArray for 'seq_num'.
            data_arrays['seq_num'] = xarray.DataArray(
                data=seq_nums,
                dims=('time',),
                coords={'time': times},
                name='seq_num')

            datasets.append(xarray.Dataset(data_vars=data_arrays))
        # Merge Datasets from all Event Descriptors into one representing the
        # whole stream. (In the future we may simplify to one Event Descriptor
        # per stream, but as of this writing we must account for the
        # possibility of multiple.)
        self._ds = xarray.merge(datasets)


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


class MongoInsertCallback:
    """
    This is a replacmenet for db.insert.
    """
    def __init__(self, metadatastore_uri, asset_registry_uri):
        self._metadatastore_client = pymongo.MongoClient(metadatastore_uri)
        self._asset_registry_client = pymongo.MongoClient(asset_registry_uri)

        try:
            # Called with no args, get_database() returns the database
            # specified in the uri --- or raises if there was none. There is no
            # public method for checking this in advance, so we just catch the
            # error.
            mds_db = self._metadatastore_client.get_database()
        except pymongo.errors.ConfigurationError as err:
            raise ValueError(
                f"Invalid metadatastore_uri: {metadatastore_uri} "
                f"Did you forget to include a database?") from err
        try:
            assets_db = self._asset_registry_client.get_database()
        except pymongo.errors.ConfigurationError as err:
            raise ValueError(
                f"Invalid asset_registry_uri: {asset_registry_uri} "
                f"Did you forget to include a database?") from err

        self._run_start_collection = mds_db.get_collection('run_start')
        self._run_stop_collection = mds_db.get_collection('run_stop')
        self._event_descriptor_collection = mds_db.get_collection('event_descriptor')
        self._event_collection = mds_db.get_collection('event')

        self._resource_collection = assets_db.get_collection('resource')
        self._datum_collection = assets_db.get_collection('datum')

    def __call__(self, name, doc):
        # Before inserting into mongo, convert any numpy objects into built-in
        # Python types compatible with pymongo.
        sanitized_doc = doc.copy()
        _apply_to_dict_recursively(sanitized_doc, _sanitize_numpy)
        getattr(self, name)(sanitized_doc)

    def start(self, doc):
        self._run_start_collection.insert_one(doc)

    def descriptor(self, doc):
        self._event_descriptor_collection.insert_one(doc)

    def resource(self, doc):
        self._resource_collection.insert_one(doc)

    def event(self, doc):
        self._event_collection.insert_one(doc)

    def datum(self, doc):
        self._datum_collection.insert_one(doc)

    def stop(self, doc):
        self._run_stop_collection.insert_one(doc)


def _sanitize_numpy(val):
    "Convert any numpy objects into built-in Python types."
    if isinstance(val, (numpy.generic, numpy.ndarray)):
        if numpy.isscalar(val):
            return val.item()
        return val.tolist()
    return val


def _apply_to_dict_recursively(d, f):
    for key, val in d.items():
        if hasattr(val, 'items'):
            d[key] = _apply_to_dict_recursively(val, f)
        d[key] = f(val)

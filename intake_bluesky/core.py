import dask
import dask.bag
import intake.catalog.base
from intake.compat import unpack_kwargs
import msgpack
import requests
from requests.compat import urljoin, urlparse
import numpy
import xarray


def documents_to_xarray(start_doc, stop_doc, descriptor_docs, event_docs,
                        include=None, exclude=None):
    if include is None:
        include = []
    if exclude is None:
        exclude = []
    if include and exclude:
        raise ValueError(
            "The parameters `include` and `exclude` are mutually exclusive.")
    uid = start_doc['uid']

    # Data keys must not change within one stream, so we can safely sample
    # just the first Event Descriptor.
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
        events = [doc for doc in event_docs
                  if doc['descriptor'] == descriptor['uid']]
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
            if data_keys[key].get('external'):
                raise NotImplementedError
            else:
                data_arrays[key] = xarray.DataArray(
                    data=data_table[key],
                    dims=('time',) + dims,
                    coords={'time': times},
                    name=key)

        # Make DataArrays for configuration data.
        for object_name, config in descriptor['configuration'].items():
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
                if data_keys[key].get('external'):
                    raise NotImplementedError
                else:
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


class RemoteRunCatalog(intake.catalog.base.RemoteCatalog):
    """
    Client-side proxy to a RunCatalog on the server.
    """
    name = 'bluesky-run-catalog'

    def __init__(self, url, headers, name, parameters, metadata=None, **kwargs):
        """

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
        super().__init__(url=url, headers=headers, name=name,
                metadata=metadata, **kwargs)
        self.url = url
        self.name = name
        self.parameters = parameters
        self.headers = headers
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
                                **self.headers)
            req.raise_for_status()
            response = msgpack.unpackb(req.content, **unpack_kwargs)
            self._parse_open_response(response)

    def _parse_open_response(self, response):
        self.npartitions = response['npartitions']
        self.metadata = response['metadata']
        self._schema = intake.source.base.Schema(datashape=None, dtype=None,
                              shape=self.shape,
                              npartitions=self.npartitions,
                              metadata=self.metadata)
        self._source_id = response['source_id']

    def _load_metadata(self):
        if self.bag is None:
            self.parts = [dask.delayed(intake.container.base.get_partition)(
                self.url, self.headers, self._source_id, self.container, i
            )
                          for i in range(self.npartitions)]
            self.bag = dask.bag.from_delayed(self.parts)
        return self._schema

    def _get_partition(self, i):
        self._load_metadata()
        return self.parts[i].compute()

    def read(self):
        self._load_metadata()
        return self.bag.compute()

    def to_dask(self):
        self._load_metadata()
        return self.bag

    def _close(self):
        self.bag = None

    def read_canonical(self):
        for i in range(self.npartitions):
            for name, doc in self._get_partition(i):
                yield name, doc


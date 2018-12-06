from bluesky import RunEngine
from bluesky.plans import scan
from bluesky.preprocessors import SupplementalData
import intake
from intake.conftest import intake_server
from intake_bluesky import MongoInsertCallback
from ophyd.sim import motor, det, img, direct_img
import os
import pymongo
import pytest
import shutil
import tempfile
import time
import types
import uuid


TMP_DIR = tempfile.mkdtemp()
TEST_CATALOG_PATH = [TMP_DIR]

YAML_FILENAME = 'intake_test_catalog.yml'


def teardown_module(module):
    try:
        shutil.rmtree(TMP_DIR)
    except:
        pass


@pytest.fixture
def bundle(intake_server):
    "A SimpleNamespace with an intake_server and some uids of sample data."
    fullname = os.path.join(TMP_DIR, YAML_FILENAME)

    metadatastore_uri = f'mongodb://localhost:27017/test-{str(uuid.uuid4())}'
    asset_registry_uri = f'mongodb://localhost:27017/test-{str(uuid.uuid4())}'
    RE = RunEngine({})
    sd = SupplementalData(baseline=[motor])
    RE.preprocessors.append(sd)
    RE.subscribe(MongoInsertCallback(metadatastore_uri, asset_registry_uri))
    # Simulate data with a scalar detector.
    det_scan_uid, = RE(scan([det], motor, -1, 1, 20))
    # Simulate data with an array detector.
    direct_img_scan_uid, = RE(scan([direct_img], motor, -1, 1, 20))
    # Simulate data with an array detector that stores its data externally.
    img_scan_uid, = RE(scan([img], motor, -1, 1, 20))

    with open(fullname, 'w') as f:
        f.write(f'''
plugins:
  source:
    - module: intake_bluesky
sources:
  xyz:
    description: Some imaginary beamline
    driver: mongo_metadatastore
    container: catalog
    args:
      uri: {metadatastore_uri}
    metadata:
      beamline: "00-ID"
        ''')

    time.sleep(2)

    yield types.SimpleNamespace(intake_server=intake_server,
                                det_scan_uid=det_scan_uid,
                                direct_img_scan_uid=direct_img_scan_uid,
                                img_scan_uid=img_scan_uid)

    os.remove(fullname)
    for uri in (metadatastore_uri, asset_registry_uri):
        cli = pymongo.MongoClient(uri)
        cli.drop_database(cli.get_database().name)


def test_fixture(bundle):
    "Simply open the Catalog created by the fixture."
    intake.Catalog(bundle.intake_server, page_size=10)


def test_search(bundle):
    "Test search and progressive (nested) search with Mongo queries."
    cat = intake.open_catalog(bundle.intake_server, page_size=10)
    # Make sure the Catalog is nonempty.
    assert list(cat['xyz']())
    # Null serach should return full Catalog.
    assert list(cat['xyz']()) == list(cat['xyz'].search({}))
    # Progressive (i.e. nested) search:
    name, = (cat['xyz']
                .search({'plan_name': 'scan'})
                .search({'detectors': 'det'}))
    assert name == bundle.det_scan_uid


def test_run_metadata(bundle):
    "Find 'start' and 'stop' in the Entry metadata."
    cat = intake.open_catalog(bundle.intake_server, page_size=10)
    run = cat['xyz']()[bundle.det_scan_uid]
    for key in ('start', 'stop'):
        assert key in run.metadata  # entry
        assert key in run().metadata  # datasource

def test_access_scalar_data(bundle):
    "Access simple scalar data that is stored directly in Event documents."
    cat = intake.open_catalog(bundle.intake_server, page_size=10)
    run = cat['xyz']()[bundle.det_scan_uid]()
    entry = run['primary']
    entry.read()
    entry().to_dask()
    entry().to_dask().load()


def test_access_nonscalar_data(bundle):
    "Access nonscalar data that is stored directly in Event documents."
    cat = intake.open_catalog(bundle.intake_server, page_size=10)
    run = cat['xyz']()[bundle.direct_img_scan_uid]()
    entry = run['primary']
    entry.read()
    entry().to_dask()
    entry().to_dask().load()


def test_access_external_data(bundle):
    "Access nonscalar data that is stored externally using asset registry."
    cat = intake.open_catalog(bundle.intake_server, page_size=10)
    run = cat['xyz']()[bundle.img_scan_uid]()
    entry = run['primary']
    entry.read()
    entry().to_dask()
    entry().to_dask().load()

#!/usr/bin/env python3

import atexit
from concurrent.futures import ThreadPoolExecutor
import contextlib
import docker
import duckdb
import os
import numpy as np
import math
import pandas as pd
from pathlib import Path
import requests
import threading
import time
import zipfile


DATASET_SIZE_TRAIN = int(1e7)
DATASET_SIZE_VALID = int(5e5)

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SQL = ROOT / "sql"
TRANSIT_DB = DATA / "transit.db"
WALKD_TRAIN_DB = DATA / "walks-train.db"
WALKD_VALID_DB = DATA / "walks-valid.db"
TMP_DB = DATA / "tmp.db"
GTFS_ZIP = DATA / "gtfs.zip"
GTFS_DIR = DATA / "gtfs"
OSRM_FOLDER = DATA / "osrm"
MAP_NAME = "map"
OSM_FILE = OSRM_FOLDER / f"{MAP_NAME}.osm.pbf"

GTFS_URL = "https://www.ztm.poznan.pl/pl/dla-deweloperow/getGTFSFile"
OSM_URL = "http://download.geofabrik.de/europe/poland/wielkopolskie-latest.osm.pbf"

OSRM_IMAGE = "ghcr.io/project-osrm/osrm-backend"
OSRM_PORT = 53909
OSRM_TABLE_BATCH = 100
OSRM_TABLE_OUTPUT = OSRM_TABLE_BATCH * (OSRM_TABLE_BATCH - 1) / 2

osrm_container = None


def download_if_missing(url, path):
    if path.exists():
        return

    path.parent.mkdir(exist_ok=True)
    print(f"Downloading '{url}' to '{path.relative_to(Path.cwd())}'")
    content = requests.get(url).content

    with open(path, "wb") as file:
        file.write(content)


def get_gtfs():
    if GTFS_DIR.exists():
        return

    download_if_missing(GTFS_URL, GTFS_ZIP)

    with zipfile.ZipFile(GTFS_ZIP, "r") as zip:
        zip.extractall(GTFS_DIR)


def prepare_osrm():
    if (OSRM_FOLDER / f"{MAP_NAME}.osrm.fileIndex").exists():
        return

    download_if_missing(OSM_URL, OSM_FILE)

    volume = {str(DATA / "osrm"): {"bind": "/data", "mode": "rw"}}
    source = f"/data/{OSM_FILE.relative_to(OSRM_FOLDER)}"
    dc = docker.from_env()

    def osrm_backend(cmd):
        print(f"Running '{cmd}' in OSRM containter")

        dc.containers.run(
            image=OSRM_IMAGE,
            command=cmd,
            volumes=volume,
            remove=True,
        )

    osrm_backend(f"osrm-extract -p /opt/foot.lua {source}")
    osrm_backend(f"osrm-partition /data/{MAP_NAME}.osrm")
    osrm_backend(f"osrm-customize /data/{MAP_NAME}.osrm")


def start_osrm():
    global osrm_container

    if osrm_container is not None:
        return

    prepare_osrm()
    print(f"Starting osrm-routed on port {OSRM_PORT}")

    osrm_container = docker.from_env().containers.run(
        image=OSRM_IMAGE,
        command=f"osrm-routed --algorithm mld /data/{MAP_NAME}.osrm",
        volumes={str(DATA / "osrm"): {"bind": "/data", "mode": "ro"}},
        ports={"5000/tcp": OSRM_PORT},
        detach=True,
        remove=True,
    )

    atexit.register(stop_osrm)


def stop_osrm():
    global osrm_container

    if osrm_container is not None:
        print("Stopping osrm-routed")
        osrm_container.stop()
        osrm_container = None


def make_transit_db():
    if TRANSIT_DB.exists():
        return

    TMP_DB.parent.mkdir(exist_ok=True)

    with duckdb.connect(TMP_DB) as db:

        def scalar(query):
            return db.sql(query).fetchone()[0]

        try:
            if not scalar("select 'connection' in (show tables)"):
                print("Creating tables")
                db.sql(read(SQL / "transit" / "create-tables.sql"))

            if scalar("select count(*) from agency") == 0:
                get_gtfs()
                print("Importing GTFS")
                with working_dir(ROOT):
                    db.sql(read(SQL / "transit" / "import-gtfs.sql"))
        except:
            db.close()
            TMP_DB.unlink(missing_ok=True)
            raise

        if scalar("select count(*) from connection") == 0:
            print("Generating connections")
            db.sql(read(SQL / "transit" / "generate-connections.sql"))

        if scalar("select count(*) from walk") == 0:
            print("Calculating walking distances")
            walk_calc(db)

        db.sql(read(SQL / "transit" / "index.sql"))

    TMP_DB.rename(TRANSIT_DB)


def walk_calc(db: duckdb.DuckDBPyConnection):
    walk_calc_init(db)
    print("Calculating walking distances")
    start_osrm()

    # Repeating in loop to handle intermittent OSRM failures
    while True:
        calcs = db.sql(
            "select id, coords from walk_calc where distances is null"
        ).fetchall()

        if len(calcs) == 0:
            break

        executor = ThreadPoolExecutor(max_workers=os.cpu_count())

        for id, coords in calcs:
            executor.submit(walk_calc_one, id, coords, db.cursor())

        executor.shutdown(wait=True)

    db.sql(read(SQL / "transit" / "walk-calc-finish.sql"))


def walk_calc_one(id, coords, db):
    query = f"""
      update walk_calc set distances = (
        select distances[1][2:] from read_json(
          'http://localhost:{OSRM_PORT}/table/v1/foot/{coords}?sources=0&annotations=distance'
        )
      ) where id = {id}
    """

    db.sql(query)


def walk_calc_init(db: duckdb.DuckDBPyConnection):
    try:
        if db.sql("select count(*) from walk_calc").fetchone()[0] > 0:
            return
    except:
        pass

    print("Creating temp table walk_calc")
    db.sql(read(SQL / "transit" / "walk-calc-init.sql"))


def get_dataset_metadata():
    make_transit_db()

    with duckdb.connect(TRANSIT_DB) as db:
        return db.sql(read(SQL / "transit" / "get-dataset-metadata.sql")).fetchone()[0]


def make_walks_db(path, size):
    meta = get_dataset_metadata()

    with duckdb.connect(path) as db:
        db.sql(read(SQL / "walks" / "init.sql"))
        cur_size = db.table("walk").count("*").fetchone()[0]

        if cur_size >= size:
            return

        print(f"Generating {path.relative_to(Path.cwd())} ({size} rows)")
        start_osrm()

        with ThreadPoolExecutor(max_workers=os.cpu_count()) as tp:
            batches = math.ceil((size-cur_size)/OSRM_TABLE_OUTPUT)
            futures = [tp.submit(gen_walks_batch, meta) for _ in range(0, batches)]
            insert = read(SQL / "walks" / "insert.sql")

            for f in futures:
                # SQL inputs
                coords, osrm_response = f.result()
                db.sql(insert)


def gen_walks_batch(meta):
    n = OSRM_TABLE_BATCH
    c = meta["centroid"]
    d = meta["max_dev"]

    lat = np.random.normal(c["lat"], d["lat"] / 3, n).astype('float32')
    lon = np.random.normal(c["lon"], d["lon"] / 3, n).astype('float32')
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in zip(lat, lon))

    osrm_response = np.array([osrm_table(coords_str)])
    coords = pd.DataFrame({'i': np.arange(1, n+1), 'lat': lat, 'lon': lon})
    return (coords, osrm_response)


def osrm_table(coords, sources=None):
    srcs = f"&sources={sources}" if sources else ""
    url = f"http://localhost:{OSRM_PORT}/table/v1/foot/{coords}?annotations=distance{srcs}"

    while True:
        response = requests.get(url).text

        if response.find('"code":"Ok"') == -1:
            print(f"OSRM error: {res}")
            time.sleep(3)
        else:
            return response


def read(path):
    with open(path, "r") as file:
        return file.read()


@contextlib.contextmanager
def working_dir(path):
    current = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(current)


if __name__ == "__main__":
    make_walks_db(WALKD_TRAIN_DB, DATASET_SIZE_TRAIN)
    make_walks_db(WALKD_VALID_DB, DATASET_SIZE_VALID)

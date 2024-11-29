#!/usr/bin/env python3

import asyncio
import docker
import json
import os
from pathlib import Path
import pyarrow
import requests
import sys
import time
from typing import Iterable

sys.path.append(str(Path(__file__).parents[1] / "ebus"))
sys.path.append(str(Path(__file__).parent))

from common import *
from transit.osrm import *
from transit.transitdb import *


def download_if_missing(url, path):
  if path.exists():
    return

  path.parent.mkdir(exist_ok=True, parents=True)
  print(f"Downloading '{url}' to '{fpath(path)}'")
  content = requests.get(url).content

  with open(path, "wb") as file:
    file.write(content)


def import_gtfs(tdb, source_name: str, gtfs_folder: Path):
  print(f"Importing GTFS '{source_name}' from '{fpath(gtfs_folder)}'")
  tdb.set_variable('SOURCE_NAME', source_name)
  tdb.set_variable('GTFS_FOLDER', str(gtfs_folder.absolute()))

  t0 = time.time()
  tdb.script("gtfs/init")
  tdb.script("gtfs/import/required")

  for opt_gtfs in OPTIONAL_GTFS_FILES:
    if (gtfs_folder / opt_gtfs).exists():
      tdb.script(f"gtfs/import/{opt_gtfs[:-4].replace('_', '-')}")

  t1 = time.time()
  tdb.script("gtfs/process/assign-id")
  tdb.script("gtfs/process/services")
  tdb.script("gtfs/process/shapes")
  tdb.script("gtfs/process/trips")

  t2 = time.time()
  tdb.script("gtfs/insert")
  tdb.script("gtfs/clean-up")

  t3 = time.time()
  print(f"Time: {_t(t3, t0)} "
    f"(parsing: {_t(t1, t0)}, processing: {_t(t2, t1)}, inserting: {_t(t3, t2)})")


async def calculate_stop_walks(tdb, osrm: OsrmClient):
  t0 = time.time()
  print("Calculating walking distances between stops")
  tdb.script("project-stop-coords")
  inputs = tdb.script("init-stop-walk").arrow()
  sem = asyncio.Semaphore(os.cpu_count())

  async def task(row):
    async with sem:
      from_id = row["from_stop"]
      from_lat = row["lat"]
      from_lon = row["lon"]
      to_stops = row["to_stops"].values
      to_ids = to_stops.field("id").to_numpy()
      to_lats = to_stops.field("lat").to_numpy()
      to_lons = to_stops.field("lon").to_numpy()

      distances = await osrm.distance_to_many_async(
        Coords(from_lat, from_lon),
        (Coords(lat, lon) for lat, lon in zip(to_lats, to_lons)),
      )

      return from_id, to_ids, distances

  for task in [asyncio.create_task(task(row)) for row in inputs]:
    from_id, to_ids, distances = await task
    from_ids = np.repeat(from_id, len(to_ids))

    tdb.sql(
      "insert into stop_walk from result",
      views = {
        "result": pyarrow.table(
          [from_ids, to_ids, distances],
          ["from_stop", "to_stop", "distance"],
        )
      },
    )

  t1 = time.time()
  print(f"Time: {_t(t1, t0)}")


def osrm_data(region: str):
  if (DATA_REGIONS / region / "map.osrm.mldgr").exists():
    return

  osm_file = TMP_REGIONS / region / "map.osm.pbf"
  download_if_missing(REGIONS[region], osm_file)
  dc = docker.from_env()

  def osrm_backend(cmd):
    print(f"Running '{cmd}' in OSRM containter")

    dc.containers.run(
      image=OSRM_IMAGE,
      command=cmd,
      volumes={str((TMP_REGIONS / region).absolute()): {"bind": "/data", "mode": "rw"}},
      remove=True,
    )

  osrm_backend(f"osrm-extract -p /opt/foot.lua /data/map.osm.pbf")
  osrm_backend(f"osrm-partition /data/map.osrm")
  osrm_backend(f"osrm-customize /data/map.osrm")

  (DATA_REGIONS / region).mkdir(parents=True, exist_ok=True)

  for file in (TMP_REGIONS / region).iterdir():
    if file.name != "map.osm.pbf":
      file.rename(DATA_REGIONS / region / file.name)


def prepare_city(city):
  target = DATA_CITIES / f"{city["id"]}.db"
  tmp = TMP_CITIES / f"{city["id"]}.db"
  tmp_dir = TMP_CITIES / city["id"]

  if target.exists():
    print(f"Database {fpath(target)} exists, skipping")
    return

  for k, url in city["gtfs"].items():
    download_if_missing(url, tmp_dir / f"{k}.zip")

  try:
    with TransitDb(tmp, write=True) as tdb:
      tdb.set_variable("PROJECTION", city["projection"])
      tdb.set_variable("CITY", city["name"])
      tdb.set_variable("REGION", city["region"])
      tdb.script("init")

      for gtfs in city["gtfs"].keys():
        gtfs_zip = tmp_dir / f"{gtfs}.zip"
        gtfs_dir = gtfs_zip.parent / gtfs_zip.name.replace(".zip", "")
        unzip(gtfs_zip, gtfs_dir)
        import_gtfs(tdb, gtfs, gtfs_dir)

      osrm_data(city["region"])

      with start_osrm(city["region"]) as osrm:
        asyncio.run(calculate_stop_walks(tdb, osrm))

      t0 = time.time()
      print("Finalizing")
      realtime = np.array(city.get("realtime", []), dtype=str)
      tdb.script("finalize", views={"city_realtime": realtime})

      t1 = time.time()
      print(f"Time: {_t(t1, t0)}")

  except:
    tmp.unlink(missing_ok=True)
    raise

  tmp.rename(target)


def main():
  args = sys.argv
  DATA_CITIES.mkdir(parents=True, exist_ok=True)

  if len(args) == 1:
    print(f"Usage: {args[0]} CITY")
  elif args[1] == "all":
    for city in CITIES:
      prepare_city(city)
    return
  else:
    city_name_or_db = " ".join(args[1:])
    city = get_city(city_name_or_db)

    if city is not None:
      prepare_city(city)
      return

    print(f"Unknown city '{city_name}'")

  print("Available cities:\n  all")

  for city in CITIES:
    print(f"  {city["name"]} | {city["id"]}")



def _t(t_to, t_from):
  return f"{round(t_to - t_from, 3)}s"


if __name__ == "__main__":
  main()

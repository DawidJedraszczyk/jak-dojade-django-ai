import asyncio
import contextlib
import docker
from pathlib import Path
import time

from bimba.osrm import OsrmClient


PIPELINE = Path(__file__).parent
DATA_FOLDER = PIPELINE.parent / "data" / "main"
OSRM_FOLDER = DATA_FOLDER / "osrm"

OSRM_IMAGE = "ghcr.io/project-osrm/osrm-backend"
OSRM_PORT = 53909


@contextlib.contextmanager
def start_osrm():
  print(f"Starting osrm-routed on port {OSRM_PORT}")

  container = docker.from_env().containers.run(
    image=OSRM_IMAGE,
    command=f"osrm-routed --algorithm mld /data/map.osrm",
    volumes={str(OSRM_FOLDER.absolute()): {"bind": "/data", "mode": "ro"}},
    ports={"5000/tcp": OSRM_PORT},
    detach=True,
    remove=True,
  )

  try:
    asyncio.run(osrm_healthcheck())
    yield
  finally:
    print("Stopping osrm-routed")
    container.stop()


async def osrm_healthcheck():
  osrm = OsrmClient(f"http://localhost:{OSRM_PORT}")

  # Wait for up to 30 seconds, check every 0.25 seconds
  for _ in range(30 * 4):
    if await osrm.healthcheck():
      return
    else:
      time.sleep(0.25)

import asyncio
import cProfile
from dataclasses import dataclass
import datetime
from haversine.haversine import get_avg_earth_radius, _haversine_kernel, Unit # type: ignore
import heapq
from itertools import chain
import numba as nb # type: ignore
from numba.experimental import jitclass # type: ignore
import pyarrow
import time as timer
from typing import Optional

from .data.common import *
from .data.stops import Stops
from .data.trips import Trips
from .osrm import *
from .params import *
from .transitdb import *


EARTH_RADIUS = get_avg_earth_radius(Unit.METERS)


@nb.jit
def nb_haversine(a_lat, a_lon, b_lat, b_lon):
  return EARTH_RADIUS * _haversine_kernel(a_lat, a_lon, b_lat, b_lon)


class Router:
  tdb: TransitDb
  osrm: OsrmClient
  debug: bool
  max_stop_id: int
  stop_coords: NDArray
  stops: Stops
  trips: Trips

  def __init__(self, tdb: TransitDb, osrm: OsrmClient, debug: bool = False):
    self.tdb = tdb
    self.osrm = osrm
    self.debug = debug
    self.max_stop_id = tdb.sql("select max(id) from stop").scalar()
    self.stop_coords = get_stop_coords(self.max_stop_id, tdb)
    self.stops = tdb.get_stops()
    self.trips = tdb.get_trips()

  async def find_route(
      self,
      from_lat: float,
      from_lon: float,
      to_lat: float,
      to_lon: float,
      date: datetime.date,
      time: datetime.time,
  ):
    if self.debug:
      print(f"Routing ({from_lat}, {from_lon}) -> ({to_lat}, {to_lon}) on {date} {time}")

    start_time = time.hour * 60*60 + time.minute * 60 + time.second
    services = self.tdb.get_services(date)
    from_stops = self.tdb.nearest_stops(from_lat, from_lon)
    to_stops = self.tdb.nearest_stops(to_lat, to_lon)

    walks_from, walks_to = await asyncio.gather(
      self.osrm.distance_to_many(
        from_lat, from_lon,
        chain(stop_coords(from_stops), [(to_lat, to_lon)]),
      ),
      self.osrm.distance_to_many(
        to_lat, to_lon,
        stop_coords(to_stops),
      ),
    )

    walk_distance = walks_from[-1]

    if self.debug:
      print(f"  {start_time=}")
      print(f"  {services=}")
      print(f"  {walk_distance=}")
      print(f"  from: {list(zip(stop_ids(from_stops), walks_from))}")
      print(f"  to: {list(zip(stop_ids(to_stops), walks_to))}")

    RouterTask(
      self.debug,
      self.stop_coords.astype(np.float32),
      self.stops,
      self.trips,
      services,
      NearStops(stop_ids(from_stops), stop_coords(from_stops), walks_from[:-1]),
      NearStops(stop_ids(to_stops), stop_coords(to_stops), walks_to),
      walk_distance,
      start_time,
    ).solve()


@jitclass([
  ("from_stop", nb.int32),
  ("trip_id", nb.int32),
  ("details", nb.int32), # departure if trip_id != -1, walk distance otherwise
])
class PathSegment:
  def __init__(self, from_stop, trip_id, details):
    self.from_stop = from_stop
    self.trip_id = trip_id
    self.details = details

  def __str__(self):
    return f"({self.from_stop}, {self.trip_id}, {self.details})"


@jitclass([
  ("stop_id", nb.int32),
  ("estimate", nb.int32),
  ("arrival", nb.int32),
  ("destination_arrival", nb.int32),
  ("walk_distance", nb.int32), # to destination, -1 if too far
  ("came_from", nb.optional(PathSegment.class_type.instance_type)),
  ("is_candidate", nb.boolean),
])
class Node:
  def __init__(
    self,
    stop_id: int,
    estimate: int,
    walk_distance: int,
  ):
    self.stop_id = stop_id
    self.estimate = estimate
    self.arrival = INF_TIME
    self.destination_arrival = INF_TIME
    self.walk_distance = walk_distance
    self.came_from = None
    self.is_candidate = False

  def __lt__(self, other):
    return self.destination_arrival < other.destination_arrival


@jitclass([
  ("ids", nb.int32[:]),
  ("coords", nb.float32[:, :]),
  ("distances", nb.float32[:]),
])
class NearStops:
  def __init__(self, ids, coords, distances):
    self.ids = ids
    self.coords = coords.astype(np.float32)
    self.distances = distances.astype(np.float32)


node_type = Node.class_type.instance_type

@jitclass([
  ("debug", nb.boolean),
  ("stop_coords", nb.float32[:, :]),
  ("stops", Stops.class_type.instance_type),
  ("trips", Trips.class_type.instance_type),
  ("services", Services.class_type.instance_type),
  ("to_stops", NearStops.class_type.instance_type),

  ("arrival", nb.int32),
  ("came_from", nb.optional(PathSegment.class_type.instance_type)),
  ("improved", nb.boolean),

  ("nodes", nb.types.ListType(node_type)),
  ("candidates", nb.types.ListType(node_type)),

  ("candidate_improved", nb.boolean),
  ("new_candidates", nb.types.ListType(node_type)),
  ("arrival_to_beat", nb.int32),
  ("iteration", nb.int32),
])
class RouterTask:
  def __init__(
    self,
    debug: bool,
    stop_coords: NDArray,
    stops: Stops,
    trips: Trips,
    services: Services,
    from_stops: NearStops,
    to_stops: NearStops,
    walk_distance: float,
    start_time: int,
  ):
    self.debug = debug
    self.stop_coords = stop_coords
    self.stops = stops
    self.trips = trips
    self.services = services
    self.to_stops = to_stops

    self.arrival = INF_TIME
    self.came_from = None
    self.improved = False

    self.nodes = nb.typed.List.empty_list(node_type)

    for _ in range(0, len(stop_coords)):
      self.nodes.append(Node(-1, -1, -1))

    self.candidates = nb.typed.List.empty_list(node_type)
    self.candidate_improved = False
    self.new_candidates = nb.typed.List.empty_list(node_type)
    self.arrival_to_beat = INF_TIME
    self.iteration = 0

    for id, dst in zip(to_stops.ids, to_stops.distances):
      self.nodes[id] = Node(id, int(dst / WALK_SPEED), int(dst))

    for id, dst in zip(from_stops.ids, from_stops.distances):
      n = self.get_node(id)
      arrival = start_time + int(dst / WALK_SPEED)
      n.arrival = arrival
      n.destination_arrival = arrival + n.estimate
      n.came_from = PathSegment(-1, -1, int(dst))
      n.is_candidate = True
      self.candidates.append(n)

    if walk_distance <= MAX_DESTINATION_WALK:
      self.arrival = start_time + int(walk_distance / WALK_SPEED)
      self.came_from = PathSegment(-1, -1, int(walk_distance))

    heapq.heapify(self.candidates)


  def get_node(self, stop_id: int) -> Node:
    node = self.nodes[stop_id]

    if node.stop_id == -1:
      node.stop_id = stop_id
      node.estimate = self.estimate(stop_id)

    return node


  def estimate(self, stop_id: int) -> int:
    coords = self.stop_coords[stop_id]
    result = INF_TIME

    for target_coords, target_walk in zip(self.to_stops.coords, self.to_stops.distances):
      distance = nb_haversine(coords[0], coords[1], target_coords[0], target_coords[1])
      result = min(result, int(distance / TRAM_SPEED + target_walk / WALK_SPEED))

    return result


  def update_node(self, node: Node, came_from: PathSegment):
    arrival = self.arrival_to_beat
    destination_arrival = arrival + node.estimate

    if destination_arrival >= self.arrival:
      return

    node.arrival = arrival
    node.came_from = came_from
    node.destination_arrival = destination_arrival

    if node.is_candidate:
      self.candidate_improved = True
    else:
      self.new_candidates.append(node)

    node.is_candidate = True

    # Earlier arrival check ensures this solution is better than the last one
    if node.walk_distance != -1:
      self.improved = True
      self.arrival = destination_arrival
      self.came_from = PathSegment(node.stop_id, -1, node.walk_distance)


  def solve(self):
    while len(self.candidates) > 0:
      self.iteration += 1
      candidate = heapq.heappop(self.candidates)
      self.candidate_improved = False

      if candidate.destination_arrival >= self.arrival: # type: ignore
        break

      for stop_walk in self.stops.get_stop_walks(candidate.stop_id):
        node = self.get_node(stop_walk.stop_id)

        if node.arrival is None or node.arrival > self.arrival:
          self.arrival_to_beat = self.arrival
        else:
          self.arrival_to_beat = node.arrival

        walk_time = int(stop_walk.distance / WALK_SPEED)

        if candidate.arrival + walk_time < self.arrival_to_beat:
          self.arrival_to_beat = candidate.arrival + walk_time
          came_from = PathSegment(candidate.stop_id, -1, stop_walk.distance)
          self.update_node(node, came_from)

      for trip_id, stop_seq, departure in self.stops.get_stop_trips(candidate.stop_id):
        time = candidate.arrival + TRANSFER_TIME - departure
        start_time = self.trips.get_next_start(trip_id, self.services, time)

        if start_time == INF_TIME:
          continue

        for to_stop, stop_arrival in self.trips.get_stops_after(trip_id, stop_seq):
          node = self.get_node(to_stop)

          if node.arrival is None or node.arrival > self.arrival:
            self.arrival_to_beat = self.arrival
          else:
            self.arrival_to_beat = node.arrival
          
          arrival = start_time + stop_arrival

          if arrival < self.arrival_to_beat:
            self.arrival_to_beat = arrival
            path = PathSegment(candidate.stop_id, trip_id, start_time + departure)
            self.update_node(node, path)

      candidate.is_candidate = False
      self.add_candidates()

    print(f"arrival: {self.arrival}, iterations: {self.iteration}")

    if self.came_from is not None:
      self.print_path(self.came_from)


  def add_candidates(self):
    if self.candidate_improved:
      if self.improved:
        self.improved = False
        old = self.candidates
        self.candidates = nb.typed.List.empty_list(node_type)

        for c in old:
          if c.destination_arrival < self.arrival:
            self.candidates.append(c)
          else:
            c.is_candidate = False

        for c in self.new_candidates:
          if c.destination_arrival < self.arrival:
            self.candidates.append(c)
          else:
            c.is_candidate = False

      else:
        self.candidates.extend(self.new_candidates)

      heapq.heapify(self.candidates)
    else:
      for nc in self.new_candidates:
        heapq.heappush(self.candidates, nc)

    self.new_candidates.clear()


  def print_path(self, came_from: PathSegment):
    while True:
      if came_from.from_stop == -1:
        print(f"Walk from start ({came_from.details}m)")
        return
      elif came_from.trip_id == -1:
        print(f"Walk from stop {self.stop_name(came_from.from_stop)} ({came_from.details}m)")
        came_from = self.nodes[came_from.from_stop].came_from
      else:
        print(f"Take trip {came_from.trip_id} at {came_from.details} from stop {self.stop_name(came_from.from_stop)}")
        came_from = self.nodes[came_from.from_stop].came_from


  def stop_name(self, id):
    return self.stops[id].name


def get_stop_coords(max_stop_id: int, tdb: TransitDb) -> NDArray:
  res = tdb.sql("select unnest(coords) from stop order by id").arrow()
  return np.stack([res.field(0), res.field(1)], axis=-1)

def stop_ids(arr: pyarrow.StructArray) -> NDArray:
  return arr.field("id").to_numpy()

def stop_coords(arr: pyarrow.StructArray) -> NDArray:
  lat = arr.field("lat").to_numpy()
  lon = arr.field("lon").to_numpy()
  return np.stack([lat, lon], axis=-1)

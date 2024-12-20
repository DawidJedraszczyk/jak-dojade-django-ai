from abc import abstractmethod
from datetime import datetime
import os
from time import time
from typing import NamedTuple

from algorithm.data import Data
from algorithm.estimator import Estimator
from algorithm.utils import time_to_seconds, seconds_to_time, custom_print, plans_to_string
from algorithm.astar_planner import AStarPlanner
from transit.data.misc import Coords

class PlannerResult(NamedTuple):
    found_plans: list
    metrics: dict


class BenchmarkStrategy():
    data: Data
    estimator: Estimator
    benchmark_type = None
    alternative_routes = 3
    total_times = []
    planners = []
    sample_routes = None

    def __init__(self, data, estimator=None):
        self.data = data
        self.estimator = estimator or data.default_estimator

    def run(self):
        self.total_times = []
        self.planners = []
        for route in self.sample_routes:
            start = Coords(*route.start_cords)
            destination = Coords(*route.destination_cords)
            start_time = time_to_seconds(route.start_time)
            start_date = route.date
            weekday = route.week_day
            total_time = 0

            planner = AStarPlanner(
                self.data,
                start,
                destination,
                start_date,
                start_time,
                self.estimator,
            )

            custom_print(
                f'Searching for route from: {route.start_name} {start} '
                f'to: {route.destination_name} {destination} '
                f'at time: {route.start_time} {route.date} '
                f'({weekday})', 'BENCHMARK')

            for _ in range(self.alternative_routes):
                t0 = time()
                _ = planner.find_next_plan()
                custom_print(f'(AStarPlannerStraight.find_next_plan = {time()-t0:.4f}s)', 'BENCHMARK')
                total_time += time()-t0

            self.total_times.append(total_time)
            self.planners.append(PlannerResult(planner.found_plans, planner.metrics))

    def print_found_routes(self):
        for i, route in enumerate(self.sample_routes):
            print('##################################################')
            print('Route from: ', route.start_name, 'to: ', route.destination_name, 'at time: ', route.start_time, ' on: ', route.week_day)
            print('##################################################')
            print(plans_to_string(self.planners[i].found_plans, self.data))
            route.print_comparison_plans()

    @abstractmethod
    def print_results_to_csv(self):
        pass

    #private methods:
    def compute_travel_duration(self, start_time, end_time):
        start_time_in_seconds = time_to_seconds(start_time)
        end_time_in_seconds = time_to_seconds(end_time)
        if end_time_in_seconds < start_time_in_seconds:
            end_time_in_seconds += time_to_seconds("24:00:00")
        duration = end_time_in_seconds - start_time_in_seconds
        return seconds_to_time(duration)

    def get_csv_filename(self):
        current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        directory = os.path.join("benchmark_results", current_time)
        os.makedirs(directory, exist_ok=True)
        filename = os.path.join(directory, f"{self.benchmark_type}_results_{current_time}.csv")
        return filename
    # There are different types of benchmark, but some metrics are common for all of them
    # and here they are computed and returned as dictionary
    def get_common_metrics_csv_row_dict(self, route, route_index, algorithm_metrics_dict):
        row_dictinary = {
            'Time searching': round(self.total_times[route_index], 3),
            'Start name': route.start_name,
            'Destination Name': route.destination_name,
            'Start Time': route.start_time,
            'Day of week': route.week_day,
            'found route': plans_to_string(self.planners[route_index].found_plans, self.data),
            'found route duration': self.compute_travel_duration(
                route.start_time,
                seconds_to_time(self.planners[route_index].found_plans[0].time_at_destination)) if self.planners[route_index].found_plans else 'NA',
        }
        row_dictinary.update(algorithm_metrics_dict)
        return row_dictinary

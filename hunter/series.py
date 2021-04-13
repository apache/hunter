import logging
from dataclasses import dataclass
from itertools import groupby
from typing import Dict, List, Optional, Iterable

from hunter.analysis import (
    fill_missing,
    compute_change_points,
    ComparativeStats,
    TTestSignificanceTester,
)

import numpy as np


@dataclass
class AnalysisOptions:
    window_len: int
    max_pvalue: float
    min_magnitude: float

    def __init__(self):
        self.window_len = 50
        self.max_pvalue = 0.001
        self.min_magnitude = 0.0


@dataclass
class ChangePoint:
    """A change-point for a single metric"""

    metric: str
    index: int
    time: int
    stats: ComparativeStats

    def forward_change_percent(self) -> float:
        return self.stats.forward_rel_change() * 100.0

    def backward_change_percent(self) -> float:
        return self.stats.backward_rel_change() * 100.0

    def magnitude(self):
        return self.stats.change_magnitude()


@dataclass
class ChangePointGroup:
    """A group of change points on multiple metrics, at the same time"""

    index: int
    time: int
    prev_time: int
    attributes: Dict[str, str]
    prev_attributes: Dict[str, str]
    changes: List[ChangePoint]


class Series:
    """
    Stores values of interesting metrics of all runs of
    a fallout test indexed by a single time variable.
    Provides utilities to analyze data e.g. find change points.
    """

    test_name: str
    time: List[int]
    attributes: Dict[str, List[str]]
    data: Dict[str, List[float]]

    def __init__(
        self,
        test_name: str,
        time: List[int],
        data: Dict[str, List[float]],
        metadata: Dict[str, List[str]],
    ):
        self.test_name = test_name
        self.time = time
        self.attributes = metadata
        self.data = data
        assert all(len(x) == len(time) for x in data.values())
        assert all(len(x) == len(time) for x in metadata.values())

    def attributes_at(self, index: int) -> Dict[str, str]:
        result = {}
        for (k, v) in self.attributes.items():
            result[k] = v[index]
        return result

    def analyze(self, options: AnalysisOptions = AnalysisOptions()) -> "AnalyzedSeries":
        logging.info(f"Computing change points for test {self.test_name}...")
        return AnalyzedSeries(self, options)


class AnalyzedSeries:
    """
    Time series data with computed change points.
    """

    __series: Series
    options: AnalysisOptions
    change_points: Dict[str, List[ChangePoint]]
    change_points_by_time: List[ChangePointGroup]

    def __init__(self, series: Series, options: AnalysisOptions):
        self.__series = series
        self.options = options
        self.change_points = self.__compute_change_points(series, options)
        self.change_points_by_time = self.__group_change_points_by_time(series, self.change_points)

    @staticmethod
    def __compute_change_points(
        series: Series, options: AnalysisOptions
    ) -> Dict[str, List[ChangePoint]]:
        result = {}
        for metric in series.data.keys():
            values = series.data[metric].copy()
            fill_missing(values)
            change_points = compute_change_points(
                values,
                window_len=options.window_len,
                max_pvalue=options.max_pvalue,
                min_magnitude=options.min_magnitude,
            )
            result[metric] = []
            for c in change_points:
                result[metric].append(
                    ChangePoint(
                        index=c.index, time=series.time[c.index], metric=metric, stats=c.stats
                    )
                )
        return result

    @staticmethod
    def __group_change_points_by_time(
        series: Series, change_points: Dict[str, List[ChangePoint]]
    ) -> List[ChangePointGroup]:
        changes: List[ChangePoint] = []
        for metric in change_points.keys():
            changes += change_points[metric]

        changes.sort(key=lambda c: c.index)
        points = []
        for k, g in groupby(changes, key=lambda c: c.index):
            cp = ChangePointGroup(
                index=k,
                time=series.time[k],
                prev_time=series.time[k - 1],
                attributes=series.attributes_at(k),
                prev_attributes=series.attributes_at(k - 1),
                changes=list(g),
            )
            points.append(cp)

        return points

    def get_stable_range(self, metric: str, index: int) -> (int, int):
        """
        Returns a range of indexes (A, B) such that:
          - A is the nearest change point index of the `metric` before or equal given `index`,
            or 0 if not found
          - B is the nearest change point index of the `metric` after given `index,
            or len(self.time) if not found

        It follows that there are no change points between A and B.
        """
        begin = 0
        for cp in self.change_points[metric]:
            if cp.index > index:
                break
            begin = cp.index

        end = len(self.time())
        for cp in reversed(self.change_points[metric]):
            if cp.index <= index:
                break
            end = cp.index

        return begin, end

    def test_name(self):
        return self.__series.test_name

    def time(self) -> List[int]:
        return self.__series.time

    def data(self, metric: str) -> List[float]:
        return self.__series.data[metric]

    def attributes(self) -> Iterable[str]:
        return self.__series.attributes.keys()

    def attribute_values(self, attribute: str) -> List[str]:
        return self.__series.attributes[attribute]

    def metrics(self) -> Iterable[str]:
        return self.__series.data.keys()


@dataclass
class SeriesComparison:
    series_1: AnalyzedSeries
    series_2: AnalyzedSeries
    index_1: int
    index_2: int
    stats: Dict[str, ComparativeStats]  # keys: metric name


def compare(
    series_1: AnalyzedSeries,
    index_1: Optional[int],
    series_2: AnalyzedSeries,
    index_2: Optional[int],
) -> SeriesComparison:

    # if index not specified, we want to take the most recent performance
    index_1 = index_1 if index_1 is not None else len(series_1.time())
    index_2 = index_2 if index_2 is not None else len(series_2.time())
    metrics = set(series_1.metrics()).intersection(series_2.metrics())

    tester = TTestSignificanceTester(series_1.options.max_pvalue)
    stats = {}

    for metric in metrics:
        (begin_1, end_1) = series_1.get_stable_range(metric, index_1)
        data_1 = series_1.data(metric)[begin_1:end_1]

        (begin_2, end_2) = series_2.get_stable_range(metric, index_2)
        data_2 = series_2.data(metric)[begin_2:end_2]

        stats[metric] = tester.compare(np.array(data_1), np.array(data_2))

    return SeriesComparison(series_1, series_2, index_1, index_2, stats)

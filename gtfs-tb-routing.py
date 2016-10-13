#!/usr/bin/env python3

import itertools as it, operator as op, functools as ft
from collections import namedtuple, defaultdict
from pathlib import Path
import os, sys, re, csv, math, time

import tb_routing as tb


class Conf:
	stop_linger_time_default = 5*60 # used if departure-time is missing
	footpath_dt_base = 1*60 # footpath_dt = dt_base + km / speed_kmh
	footpath_speed_kmh = 5 / 3600
	footpath_dt_max = 20*60 # all footpaths longer than that are discarded as invalid

conf = Conf() # XXX: placeholder


def iter_gtfs_tuples(gtfs_dir, filename):
	if filename.endswith('.txt'): filename = filename[:-4]
	tuple_t = ''.join(' '.join(filename.rstrip('s').split('_')).title().split())
	with (gtfs_dir / '{}.txt'.format(filename)).open(encoding='utf-8-sig') as src:
		src_csv = csv.reader(src)
		tuple_t = namedtuple(tuple_t, list(v.strip() for v in next(src_csv)))
		for line in src_csv: yield tuple_t(*line)

def parse_gtfs_dts(ts_str):
	if ':' not in ts_str: return
	return sum((mul * int(v)) for mul, v in zip([3600, 60, 1], ts_str.split(':')))

def footpath_dt(stop_a, stop_b, math=math):
	'''Calculate footpath time-delta (dt) between two stops,
		based on their lon/lat distance (using Haversine Formula) and walking-speed constant.'''
	# Alternative: use UTM coordinates and KDTree (e.g. scipy) or spatial dbs
	lon1, lat1, lon2, lat2 = (
		math.radians(float(v)) for v in
		[stop_a.lon, stop_a.lat, stop_b.lon, stop_b.lat] )
	km = 6367 * 2 * math.asin(math.sqrt(
		math.sin((lat2 - lat1)/2)**2 +
		math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1)/2)**2 ))
	return conf.footpath_dt_base + km / conf.footpath_speed_kmh

def parse_gtfs_timetable(gtfs_dir):
	'Parse Timetable from GTFS data directory.'
	types = tb.t.input

	stops = types.Stops()
	for t in iter_gtfs_tuples(gtfs_dir, 'stops'):
		stops.add(types.Stop(t.stop_id, t.stop_name, t.stop_lon, t.stop_lat))

	footpaths = types.Footpaths()
	for stop_a, stop_b in it.combinations(list(stops), 2):
		footpaths.add(stop_a, stop_b, footpath_dt(stop_a, stop_b))

	trip_stops = defaultdict(list)
	for t in iter_gtfs_tuples(gtfs_dir, 'stop_times'): trip_stops[t.trip_id].append(t)

	trips = types.Trips()
	for t in iter_gtfs_tuples(gtfs_dir, 'trips'):
		trip = types.Trip()
		for ts in sorted(trip_stops[t.trip_id], key=lambda t: int(t.stop_sequence)):
			dts_arr, dts_dep = map(parse_gtfs_dts, [ts.arrival_time, ts.departure_time])
			if not dts_arr:
				if not trip: # first stop of the trip - arrival ~ departure
					if dts_dep: dts_arr = dts_dep - conf.stop_linger_time_default
					else: continue
				else: dts_arr = trip[-1].dts_dep # "scheduled based on the nearest preceding timed stop"
			if not dts_dep: dts_dep = dts_arr + conf.stop_linger_time_default
			trip.append(
				types.TripStop(stop=stops[ts.stop_id], dts_arr=dts_arr, dts_dep=dts_dep) )
		if trip: trips.add(trip)

	log.debug(
		'Parsed timetable: stops={} footpaths={} trips={} trip_stops={}',
		len(stops), len(footpaths), len(trips), len(trip_stops) )
	return types.Timetable(stops, footpaths, trips)


def calc_timer(func, *args, log=tb.u.get_logger('timer'), **kws):
	func_id = '.'.join([func.__module__.strip('__'), func.__name__])
	log.debug('[{}] Starting...', func_id)
	td = time.monotonic()
	data = func(*args, **kws)
	td = time.monotonic() - td
	log.debug('[{}] Finished in: {:.1f}s', func_id, td)
	return data

def main(args=None):
	import argparse
	parser = argparse.ArgumentParser(
		description='Simple implementation of graph-db and algos on top of that.')
	parser.add_argument('gtfs_dir', help='Path to gtfs data directory to build graph from.')
	parser.add_argument('-d', '--debug', action='store_true', help='Verbose operation mode.')

	opts = parser.parse_args(sys.argv[1:] if args is None else args)

	global log
	tb.u.logging.basicConfig(
		format='%(asctime)s :: %(name)s %(levelname)s :: %(message)s',
		datefmt='%Y-%m-%d %H:%M:%S',
		level=tb.u.logging.DEBUG if opts.debug else tb.u.logging.WARNING )
	log = tb.u.get_logger('main')

	timetable = calc_timer(parse_gtfs_timetable, Path(opts.gtfs_dir))
	router = tb.engine.TBRoutingEngine(timetable, timer=calc_timer)

if __name__ == '__main__': sys.exit(main())

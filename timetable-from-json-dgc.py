#!/usr/bin/env python3

import itertools as it, operator as op, functools as ft
import os, sys, pathlib, random, re, pickle

import tb_routing as tb, test._common as c


class Conf:
	# All time values are in minutes
	line_trip_max_count = 50
	line_trip_interval = 30, 150, 30 # min, max, step
	line_stop_linger = 5, 30, 5
	line_kmh = 50, 100, 10
	fp_kmh = 5
	fp_dt_base = 2
	fp_dt_max = 8
	dt_ch = 2
	reroll_max = 2**10

# How to pick these - find 3 stop-pairs, where these should hold:
#  1: dt_line >> dt_fp (too far to walk), 2: dt_line ~ dt_fp (rather close),
#  3: dt_line < dt_fp (right next to each other)
# Run p_dt_stats(stop_id1, stop_id2) on all 3 pairs, tweak formulas based on output.
calc_dt_line = lambda km, kmh: (km / kmh) * 5 * 60
calc_dt_fp = lambda km, kmh, dt_base: (km**2.5 / kmh) / 40 + dt_base * 60


rand_int = random.randint
rand_int_align = lambda a,b,step: random.randint(a//step, b//step)*step

def line_dts_start_end():
	if random.random() < 0.7:
		line_dts_start, line_dts_end = rand_int(0, 10), rand_int(18, 24)
	elif random.random() < 0.5:
		line_dts_start = rand_int(5, 17)
		line_dts_end = rand_int(line_dts_start, 23)
		while line_dts_end - line_dts_start < 2:
			line_dts_start = rand_int(5, line_dts_end)
	else:
		line_dts_start, line_dts_end = rand_int(12, 18), rand_int(20, 24)
	line_dts_start = 3600 * line_dts_start + rand_int_align(0, 55, 5)*60
	line_dts_end = 3600 * line_dts_end
	return line_dts_start, line_dts_end

def dist(stop_a, stop_b):
	return (abs(stop_a.lon - stop_b.lon)**2 + abs(stop_a.lat - stop_b.lat)**2)**0.5

def print_dt_stats( stop_a, stop_b,
		stops=None, line_kmh=70, fp_kmh=5, fp_dt_base=2*60 ):
	stop_a, stop_b = (( stops[v]
		if isinstance(v, (str, int)) else v ) for v in [stop_a, stop_b])
	km = dist(stop_a, stop_b)
	print('\n'.join([
			'Path: {a} -> {b}',
			'  distance: {d:.1f} km',
			'  dt_fp: {dt_fp:.1f} min',
			'  dt_line: {dt_line:.0f} min' ]).format(
		a=stop_a, b=stop_b, d=km,
		dt_fp=calc_dt_fp(km, fp_kmh, fp_dt_base) / 60,
		dt_line=calc_dt_line(km, line_kmh) // 60 ))


def main(args=None):
	import argparse
	parser = argparse.ArgumentParser(
		description='Generate mock transport network timetable'
				' from a json-dgc (https://github.com/eimink/json-dgc/) graph.'
			' Graph node name format must be: [stop_id ":"] "L" x1 "-" y1 ["/L" x2 "-" y2] ...,'
				' where x values are line-ids and y values are sortable to connect stops of same line.'
			' Examples: L4-b, L1-a/L3-j.'
			' Edge arrows do not matter at all, only node names/positions do.')
	parser.add_argument('dag_json', help='DAG JSON/YAML file saved from json-dgc app.')
	parser.add_argument('tt_pickle', help='Path to store pickled Timetable object to.')
	parser.add_argument('-s', '--seed',
		help='Randomness seed (any string) for generated stuff.'
			' Generated by ordered nodes/edges concatenation by default.')
	opts = parser.parse_args(sys.argv[1:] if args is None else args)

	conf = Conf()

	with pathlib.Path(opts.dag_json).open() as src: dag = c.yaml_load(src)
	dag = c.dmap(dag)
	dag.edges = list(c.dmap(e) for e in dag.edges)
	dag.nodes = list(c.dmap(n) for n in dag.nodes)

	seed = opts.seed
	if not seed:
		seed = list()
		for n in sorted(dag.nodes, key=op.itemgetter('id')):
			seed.extend([n.id, n.title])
		for e in sorted(dag.edges, key=op.itemgetter('source', 'target')):
			seed.extend([e.source, e.target])
		seed = '\0'.join(map(str, seed))
	random.seed(seed)

	types = tb.t.public
	trips, stops, footpaths = types.Trips(), types.Stops(), types.Footpaths()
	p_dt_stats = ft.partial( print_dt_stats, stops=stops,
		fp_kmh=conf.fp_kmh, fp_dt_base=conf.fp_dt_base )

	lines = dict()
	for node in dag.nodes:
		node_lines = node.title.split('/')
		for line_node in node_lines:
			m = re.search('^(.*?:)?L(.+?)-(.*)$', line_node)
			if not m: raise ValueError(line_node)
			stop_id, line_id, line_seq = m.groups()
			stop = stops.add(types.Stop(
				stop_id or node.title, node.title, node.x, node.y ))
			lines.setdefault(line_id, list()).append((line_seq, stop))

	for line_id, line in sorted(lines.items()):
		line_dts_start, line_dts_end = line_dts_start_end()
		line_dts_interval = rand_int_align(*conf.line_trip_interval)*60
		line_kmh = rand_int_align(*conf.line_kmh)
		line_stops = list(map(op.itemgetter(1), sorted(line)))

		trip_prev = None
		for trip_seq in range(conf.line_trip_max_count):
			trip_dts_start = line_dts_start + line_dts_interval * trip_seq
			if trip_dts_start > line_dts_end: break
			for n in range(conf.reroll_max):
				trip = types.Trip(line_id_hint='L{}'.format(line_id))
				for stopidx, stop in enumerate(line_stops):
					if not trip.stops: dts_arr = trip_dts_start
					else:
						ts = trip.stops[-1]
						dts_arr = ts.dts_dep + divmod(
							calc_dt_line(dist(ts.stop, stop), line_kmh), 60 )[0] * 60
					dts_dep = dts_arr + rand_int_align(*conf.line_stop_linger)*60
					if trip_prev and trip_prev[stopidx].dts_arr >= dts_arr: break # avoid overtaking trips
					trip.add(types.TripStop(trip, stopidx, stop, dts_arr, dts_dep))
				else: break
			else:
				raise RuntimeError( 'Failed to generate'
					' non-overtaking trips in {:,} tries'.format(conf.reroll_max) )
			trips.add(trip)
			trip_prev = trip

	with footpaths.populate() as fp_add:
		fp_delta_max = conf.fp_dt_max * 60
		for stop in stops: fp_add(stop, stop, conf.dt_ch*60)
		for stop_a, stop_b in it.permutations(stops, 2):
			fp_delta = int(calc_dt_fp(dist(stop_a, stop_b), conf.fp_kmh, conf.fp_dt_base))
			if fp_delta <= fp_delta_max: fp_add( stop_a, stop_b, fp_delta)

	timetable = types.Timetable(stops, footpaths, trips)
	with pathlib.Path(opts.tt_pickle).open('wb') as dst: pickle.dump(timetable, dst)

if __name__ == '__main__': sys.exit(main())

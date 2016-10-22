import itertools as it, operator as op, functools as ft
from collections import ChainMap, Mapping, OrderedDict
from pathlib import Path
from pprint import pprint
import os, sys, unittest, types, datetime, re
import runpy, tempfile, warnings, shutil, zipfile

import yaml # PyYAML module is required for tests

path_project = Path(__file__).parent.parent
sys.path.insert(1, str(path_project))
import tb_routing as tb
gtfs_cli = type( 'FakeModule', (object,),
	runpy.run_path(str(path_project / 'gtfs-tb-routing.py')) )

if os.environ.get('TB_DEBUG'):
	tb.u.logging.basicConfig(
		format='%(asctime)s :: %(name)s %(levelname)s :: %(message)s',
		datefmt='%Y-%m-%d %H:%M:%S', level=tb.u.logging.DEBUG )



class dmap(ChainMap):

	maps = None

	def __init__(self, *maps, **map0):
		maps = list((v if not isinstance( v,
			(types.GeneratorType, list, tuple) ) else OrderedDict(v)) for v in maps)
		if map0 or not maps: maps = [map0] + maps
		super(dmap, self).__init__(*maps)

	def __repr__(self):
		return '<{} {:x} {}>'.format(
			self.__class__.__name__, id(self), repr(self._asdict()) )

	def _asdict(self):
		items = dict()
		for k, v in self.items():
			if isinstance(v, self.__class__): v = v._asdict()
			items[k] = v
		return items

	def _set_attr(self, k, v):
		self.__dict__[k] = v

	def __iter__(self):
		key_set = dict.fromkeys(set().union(*self.maps), True)
		return filter(lambda k: key_set.pop(k, False), it.chain.from_iterable(self.maps))

	def __getitem__(self, k):
		k_maps = list()
		for m in self.maps:
			if k in m:
				if isinstance(m[k], Mapping): k_maps.append(m[k])
				elif not (m[k] is None and k_maps): return m[k]
		if not k_maps: raise KeyError(k)
		return self.__class__(*k_maps)

	def __getattr__(self, k):
		try: return self[k]
		except KeyError: raise AttributeError(k)

	def __setattr__(self, k, v):
		for m in map(op.attrgetter('__dict__'), [self] + self.__class__.mro()):
			if k in m:
				self._set_attr(k, v)
				break
		else: self[k] = v

	def __delitem__(self, k):
		for m in self.maps:
			if k in m: del m[k]


class FixedOffsetTZ(datetime.tzinfo):
	_offset = _name = None
	@classmethod
	def from_offset(cls, name=None, delta=None, hh=None, mm=None):
		self = cls()
		if delta is None: delta = datetime.timedelta(hours=hh or 0, minutes=mm or 0)
		self._name, self._offset = name, delta
		return self
	def utcoffset(self, dt): return self._offset
	def tzname(self, dt): return self._name
	def dst(self, dt, ZERO=datetime.timedelta(0)): return ZERO
	def __repr__(self): return '<FixedOffset {!r}>'.format(self._name)

TZ_UTC = FixedOffsetTZ.from_offset('UTC')

def parse_iso8601( spec, tz_default=TZ_UTC,
		_re=re.compile(
			r'(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})'
			r'(?::(?P<s>\d{2}(\.\d+)?))?\s*(?P<tz>Z|[-+]\d{2}:\d{2})?' ) ):
	m = _re.search(spec)
	if not m: raise ValueError(m)
	if m.group('tz'):
		tz = m.group('tz')
		if tz == 'Z': tz = TZ_UTC
		else:
			k = {'+':1,'-':-1}[tz[0]]
			hh, mm = ((int(n) * k) for n in tz[1:].split(':', 1))
			tz = FixedOffsetTZ.from_offset(hh=hh, mm=mm)
	else: tz = tz_default
	ts_list = list(map(int, m.groups()[:5]))
	ts_list.append(
		0 if not m.group('s') else int(m.group('s').split('.', 1)[0]) )
	ts = datetime.datetime.strptime(
		'{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}'.format(*ts_list),
		'%Y-%m-%d %H:%M:%S' )
	assert tz
	ts = ts.replace(tzinfo=tz)
	return ts

def yaml_load(stream, dict_cls=OrderedDict, loader_cls=yaml.SafeLoader):
	if not hasattr(yaml_load, '_cls'):
		class CustomLoader(loader_cls): pass
		def construct_mapping(loader, node):
			loader.flatten_mapping(node)
			return dict_cls(loader.construct_pairs(node))
		CustomLoader.add_constructor(
			yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping )
		# Do not auto-resolve dates/timestamps, as PyYAML does that badly
		res_map = CustomLoader.yaml_implicit_resolvers = CustomLoader.yaml_implicit_resolvers.copy()
		res_int = list('-+0123456789')
		for c in res_int: del res_map[c]
		CustomLoader.add_implicit_resolver(
			'tag:yaml.org,2002:int',
			re.compile(r'''^(?:[-+]?0b[0-1_]+
				|[-+]?0[0-7_]+
				|[-+]?(?:0|[1-9][0-9_]*)
				|[-+]?0x[0-9a-fA-F_]+)$''', re.X), res_int )
		yaml_load._cls = CustomLoader
	return yaml.load(stream, yaml_load._cls)

def load_test_data(path_dir, path_stem, name):
	'Load test data from specified YAML file and return as dmap object.'
	with (path_dir / '{}.test.{}.yaml'.format(path_stem, name)).open() as src:
		return dmap(yaml_load(src))


def struct_from_val(val, cls, as_tuple=False):
	if isinstance(val, (tuple, list)): val = cls(*val)
	elif isinstance(val, (dmap, dict, OrderedDict)): val = cls(**val)
	else: raise ValueError(val)
	return val if not as_tuple else tb.u.attr.astuple(val)

@tb.utils.attr_struct
class JourneyStats: keys = 'start end'

@tb.utils.attr_struct
class JourneySeg: keys = 'type src dst'

@tb.utils.attr_struct
class TestGoal: keys = 'src dst dts_start'


def dts_parse(dts_str):
	if ':' not in dts_str: return float(dts_str)
	dts_vals = dts_str.split(':')
	if len(dts_vals) == 2: dts_vals.append('00')
	assert len(dts_vals) == 3, dts_vals
	return sum(int(n)*k for k, n in zip([3600, 60, 1], dts_vals))

def dts_format(dts):
	dts = int(dts)
	return datetime.time(dts // 3600, (dts % 3600) // 60, dts % 60, dts % 1)



class GTFSTestFixture:

	def __init__(self, path_gtfs_zip, path_file):
		self.path_gtfs_zip = Path(path_gtfs_zip)
		self.path_file = Path(path_file)
		self.path_test = self.path_file.parent
		self.path_project = self.path_test.parent
		self.path_tmp_base = '{}.test.{}'.format(
			self.path_project.parent.resolve().name, self.path_file.stem )

	def load_test_data(self, name):
		return load_test_data(self.path_test, self.path_file.stem, name)


	_path_cache = ...
	@property
	def path_cache(self):
		if self._path_cache is not ...: return self._path_cache
		self._path_cache = None

		paths_src = [Path(tb.__file__).parent, Path(gtfs_cli.__file__)]
		paths_cache = [ self.path_test / '{}.cache.pickle'.format(self.path_file.stem),
			Path(tempfile.gettempdir()) / '{}.cache.pickle'.format(self.path_tmp_base) ]

		for p in paths_cache:
			if not p.exists():
				try:
					p.touch()
					p.unlink()
				except OSError: continue
			self._path_cache = p
			break
		else:
			warnings.warn('Failed to find writable cache-path, disabling cache')
			warnings.warn(
				'Cache paths checked: {}'.format(' '.join(repr(str(p)) for p in paths_cache)) )

		def paths_src_mtimes():
			for root, dirs, files in it.chain.from_iterable(os.walk(str(p)) for p in paths_src):
				p = Path(root)
				for name in files: yield (p / name).stat().st_mtime
		mtime_src = max(paths_src_mtimes())
		mtime_cache = 0 if not self._path_cache.exists() else self._path_cache.stat().st_mtime
		if mtime_src > mtime_cache:
			warnings.warn( 'Existing timetable/transfer cache'
				' file is older than code, but using it anyway: {}'.format(self._path_cache) )
		return self._path_cache


	_path_unzip = None
	@property
	def path_unzip(self):
		if self._path_unzip: return self._path_unzip

		paths_unzip = [ self.path_test / '{}.data.unzip'.format(self.path_file.stem),
			Path(tempfile.gettempdir()) / '{}.data.unzip'.format(self.path_tmp_base) ]
		for p in paths_unzip:
			if not p.exists():
				try: p.mkdir(parents=True)
				except OSError: continue
			path_unzip = p
			break
		else:
			raise OSError( 'Failed to find/create path to unzip data to.'
				' Paths checked: {}'.format(' '.join(repr(str(p)) for p in paths_unzip)) )

		path_done = path_unzip / '.unzip-done.check'
		mtime_src = self.path_gtfs_zip.stat().st_mtime
		mtime_done = path_done.stat().st_mtime if path_done.exists() else 0
		if mtime_done < mtime_src:
			shutil.rmtree(str(path_unzip))
			path_unzip.mkdir(parents=True)
			mtime_done = None

		if not mtime_done:
			with zipfile.ZipFile(str(self.path_gtfs_zip)) as src: src.extractall(str(path_unzip))
			path_done.touch()

		self._path_unzip = path_unzip
		return self._path_unzip



class GraphAssertions:

	dts_slack = 10 * 60

	def __init__(self, graph=None): self.graph = graph


	def assert_journey_components(self, test, graph=None):
		'''Check that lines, trips, footpaths
			and transfers for all test journeys can be found individually.'''
		graph = graph or self.graph
		goal = struct_from_val(test.goal, TestGoal)
		goal_src, goal_dst = op.itemgetter(goal.src, goal.dst)(graph.timetable.stops)
		assert goal_src and goal_dst

		def raise_error(tpl, *args, **kws):
			raise AssertionError('[{}:{}] {}'.format(jn_name, seg_name, tpl).format(*args, **kws))

		for jn_name, jn_info in (test.journey_set or dict()).items():
			jn_stats = struct_from_val(jn_info.stats, JourneyStats)
			jn_start, jn_end = map(dts_parse, [jn_stats.start, jn_stats.end])
			ts_first, ts_last, ts_transfer = set(), set(), set()

			for seg_name, seg in jn_info.segments.items():
				seg = struct_from_val(seg, JourneySeg)
				a, b = op.itemgetter(seg.src, seg.dst)(graph.timetable.stops)
				ts_transfer_chk, ts_transfer_found, line_found = list(ts_transfer), False, False
				ts_transfer.clear()

				if seg.type == 'trip':
					for n, line in graph.lines.lines_with_stop(a):
						for m, stop in enumerate(line.stops[n:], n):
							if stop is b: break
						else: continue
						for trip in line:
							for ts in ts_transfer_chk:
								for k, (t1, n1, t2, n2) in graph.transfers.from_trip_stop(ts.trip, ts.stopidx):
									if t2[n2].stop is trip[n].stop: break
								else: continue
								ts_transfer_found = True
								ts_transfer_chk.clear()
								break
							if a is goal_src: ts_first.update(trip)
							if b is goal_dst: ts_last.update(trip)
							ts_transfer.add(trip[m])
						line_found = True
					if not line_found: raise_error('No Lines/Trips found for trip-segment')

				elif seg.type == 'fp': raise NotImplementedError
				else: raise NotImplementedError

				if not ts_transfer_found and a is not goal_src:
					raise_error( 'No transfers found from'
						' previous segment (checked: {})', len(ts_transfer_chk) )
				if not ts_transfer and b is not goal_dst:
					raise_error('No transfers found from segment (type={}) end ({!r})', seg.type, seg.dst)

			assert min(abs(jn_start - ts.dts_dep) for ts in ts_first) < self.dts_slack
			assert min(abs(jn_end - ts.dts_arr) for ts in ts_last) < self.dts_slack


	def assert_journey_results(self, test, journeys, graph=None, verbose=False):
		'Assert that all journeys described by test-data (from YAML) match journeys (JourneySet).'
		graph = graph or self.graph
		jn_matched = set()
		for jn_name, jn_info in (test.journey_set or dict()).items():
			for journey in journeys:
				if verbose: print('\n--- journey:', journey)
				jn_stats = struct_from_val(jn_info.stats, JourneyStats)
				dts_dep_test, dts_arr_test = map(dts_parse, [jn_stats.start, jn_stats.end])
				dts_dep_jn, dts_arr_jn = journey.dts_dep, journey.dts_arr
				if verbose:
					print(' ', 'time: {} == {} and {} == {}'.format(*map(
						dts_format, [dts_dep_test, dts_dep_jn, dts_arr_test, dts_arr_jn] )))
				if max(
					abs(dts_dep_test - dts_dep_jn),
					abs(dts_arr_test - dts_arr_jn) ) > self.dts_slack: break
				for seg_jn, seg_test in it.zip_longest(journey, jn_info.segments.items()):
					seg_test_name, seg_test = seg_test
					if not (seg_jn and seg_test): break
					seg_test = struct_from_val(seg_test, JourneySeg)
					a_test, b_test = op.itemgetter(seg_test.src, seg_test.dst)(graph.timetable.stops)
					type_test = seg_test.type
					if isinstance(seg_jn, tb.t.public.JourneyTrip):
						type_jn, a_jn, b_jn = 'trip', seg_jn.ts_from.stop, seg_jn.ts_to.stop
					elif isinstance(seg_jn, tb.t.public.JourneyFp):
						type_jn, a_jn, b_jn = 'fp', seg_jn.stop_from, seg_jn.stop_to
					else: raise ValueError(seg_jn)
					if verbose:
						print(' ', seg_test_name, type_test == type_jn, a_test is a_jn, b_test is b_jn)
					if not (type_test == type_jn and a_test is a_jn and b_test is b_jn): break
					jn_matched.add(id(journey))
				else: break
			else: raise AssertionError('No journeys to match test-data for: {}'.format(jn_name))
			if verbose: print()
		for journey in journeys:
			if id(journey) not in jn_matched:
				raise AssertionError('Unmatched journey found: {}'.format(journey))
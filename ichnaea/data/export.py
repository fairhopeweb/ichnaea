from collections import defaultdict
from contextlib import closing
from datetime import datetime
import re
import time
import uuid

import boto
import boto.exception
import redis.exceptions
import requests
import requests.exceptions
import pytz
import simplejson
from six.moves.urllib.parse import urlparse
import sqlalchemy.exc

from ichnaea.models import (
    ApiKey,
    BlueObservation,
    BlueReport,
    BlueShard,
    CellObservation,
    CellReport,
    CellShard,
    DataMap,
    Report,
    ScoreKey,
    User,
    WifiObservation,
    WifiReport,
    WifiShard,
)
from ichnaea.models.content import encode_datamap_grid
from ichnaea.queue import DataQueue
from ichnaea import util

WHITESPACE = re.compile('\s', flags=re.UNICODE)


class IncomingQueue(object):
    """
    The incoming queue contains the data collected in the web application
    tier. It is the single entrypoint from which all other data pipelines
    get their data.

    It distributes the data into the configured export queues.
    """

    def __init__(self, task):
        self.task = task

    def __call__(self):
        data_queue = self.task.app.data_queues['update_incoming']
        data = data_queue.dequeue()

        grouped = defaultdict(list)
        for item in data:
            grouped[item['api_key']].append({
                'api_key': item['api_key'],
                'nickname': item['nickname'],
                'report': item['report'],
            })

        export_queues = self.task.app.export_queues
        with self.task.redis_pipeline() as pipe:
            for api_key, items in grouped.items():
                for queue in export_queues.values():
                    if queue.export_allowed(api_key):
                        queue_key = queue.queue_key(api_key)
                        queue.enqueue(items, queue_key, pipe=pipe)

        if data_queue.ready():  # pragma: no cover
            self.task.apply_countdown()


class ExportScheduler(object):
    """
    The export scheduler is periodically called, checks all export queues
    and if they contain enough or old enough data schedules an async
    export task to process the data in the export queue.
    """

    def __init__(self, task):
        self.task = task

    def __call__(self, export_task):
        for export_queue in self.task.app.export_queues.values():
            for queue_key in export_queue.partitions():
                if export_queue.ready(queue_key):
                    export_task.delay(export_queue.name,
                                      queue_key=queue_key)


class ExportQueue(object):
    """
    A Redis based queue which stores binary or JSON encoded items
    in lists. The queue supports dynamic queue keys and partitioned
    queues with a common queue key prefix.

    The lists maintain a TTL value corresponding to the time data has
    been last put into the queue.
    """

    metadata = False

    def __init__(self, name, redis_client,
                 url=None, batch=0, skip_keys=(),
                 uploader_type=None, compress=False):
        self.name = name
        self.redis_client = redis_client
        self.batch = batch
        self.url = url
        self.skip_keys = skip_keys
        self.uploader_type = uploader_type
        self.compress = compress

    def _data_queue(self, queue_key):
        return DataQueue(queue_key, self.redis_client,
                         batch=self.batch, compress=self.compress, json=True)

    @classmethod
    def configure_queue(cls, key, redis_client, settings, compress=False):
        url = settings.get('url', '') or ''
        scheme = urlparse(url).scheme
        batch = int(settings.get('batch', 0))

        skip_keys = WHITESPACE.split(settings.get('skip_keys', ''))
        skip_keys = tuple([skip_key for skip_key in skip_keys if skip_key])

        queue_types = {
            'dummy': (DummyExportQueue, DummyUploader),
            'http': (HTTPSExportQueue, GeosubmitUploader),
            'https': (HTTPSExportQueue, GeosubmitUploader),
            'internal': (InternalExportQueue, InternalUploader),
            's3': (S3ExportQueue, S3Uploader),
        }
        klass, uploader_type = queue_types.get(scheme, (cls, None))

        return klass(key, redis_client,
                     url=url, batch=batch, skip_keys=skip_keys,
                     uploader_type=uploader_type, compress=compress)

    def dequeue(self, queue_key):
        return self._data_queue(queue_key).dequeue()

    def enqueue(self, items, queue_key, pipe=None):
        self._data_queue(queue_key).enqueue(items, pipe=pipe)

    def export_allowed(self, api_key):
        return (api_key not in self.skip_keys)

    def metric_tag(self):
        # strip away queue_export_ prefix
        return self.name[13:]

    @property
    def monitor_name(self):
        return self.name

    def partitions(self):
        return [self.name]

    def queue_key(self, api_key):
        return self.name

    def ready(self, queue_key):
        if queue_key is None:  # pragma: no cover
            # BBB
            queue_key = self.name
        return self._data_queue(queue_key).ready()

    def size(self, queue_key):
        if queue_key is None:  # pragma: no cover
            # BBB
            queue_key = self.name
        return self.redis_client.llen(queue_key)


class DummyExportQueue(ExportQueue):
    pass


class HTTPSExportQueue(ExportQueue):
    pass


class InternalExportQueue(ExportQueue):

    metadata = True


class S3ExportQueue(ExportQueue):

    @property
    def monitor_name(self):
        return None

    def partitions(self):
        # e.g. ['queue_export_something:api_key']
        return self.redis_client.scan_iter(match=self.name + ':*', count=100)

    def queue_key(self, api_key=None):
        if not api_key:
            api_key = 'no_key'
        return self.name + ':' + api_key


class ReportExporter(object):  # pragma: no cover
    # BBB

    def __init__(self, task, export_queue_name, queue_key):
        self.task = task
        self.export_queue_name = export_queue_name
        self.export_queue = task.app.export_queues[export_queue_name]
        self.queue_key = queue_key
        if not self.queue_key:  # pragma: no cover
            # BBB
            self.queue_key = self.export_queue.queue_key(None)

    def __call__(self, upload_task):
        items = self.export_queue.dequeue(self.queue_key)
        if not items:  # pragma: no cover
            return

        reports = items
        if not self.export_queue.metadata:
            # ignore metadata
            reports = {'items': [item['report'] for item in items]}

        upload_task.delay(
            self.export_queue_name,
            simplejson.dumps(reports),
            queue_key=self.queue_key)

        # check the queue at the end, if there's still enough to do
        # schedule another job, but give it a second before it runs
        if self.export_queue.ready(self.queue_key):
            self.task.apply_countdown(
                args=[self.export_queue_name],
                kwargs={'queue_key': self.queue_key})


class BaseReportUploader(object):

    _retriable = (IOError, )
    _retries = 3
    _retry_wait = 1.0

    def __init__(self, task, export_queue_name, queue_key):
        self.task = task
        self.export_queue_name = export_queue_name
        self.export_queue = task.app.export_queues[export_queue_name]
        self.stats_tags = ['key:' + self.export_queue.metric_tag()]
        self.queue_key = queue_key
        if not self.queue_key:  # pragma: no cover
            # BBB
            self.queue_key = self.export_queue.queue_key(None)

    def __call__(self):
        items = self.export_queue.dequeue(self.queue_key)
        if not items:  # pragma: no cover
            return

        reports = items
        if not self.export_queue.metadata:
            # ignore metadata
            reports = {'items': [item['report'] for item in items]}

        data = simplejson.dumps(reports)

        success = False
        for i in range(self._retries):
            try:
                self.upload(data)
                success = True
            except self._retriable:
                success = False
                time.sleep(self._retry_wait * (i ** 2 + 1))

            if success:
                break

        if success and self.export_queue.ready(self.queue_key):
            self.task.apply_countdown(
                args=[self.export_queue_name],
                kwargs={'queue_key': self.queue_key})

    def upload(self, data):
        self.send(self.export_queue.url, data)
        self.task.stats_client.incr(
            'data.export.batch', tags=self.stats_tags)

    def send(self, url, data):
        raise NotImplementedError()


class DummyUploader(BaseReportUploader):

    def send(self, url, data):
        pass


class GeosubmitUploader(BaseReportUploader):

    _retriable = (
        IOError,
        requests.exceptions.RequestException,
    )

    def send(self, url, data):
        headers = {
            'Content-Encoding': 'gzip',
            'Content-Type': 'application/json',
            'User-Agent': 'ichnaea',
        }
        with self.task.stats_client.timed('data.export.upload',
                                          tags=self.stats_tags):
            response = requests.post(
                url,
                data=util.encode_gzip(data, compresslevel=5),
                headers=headers,
                timeout=60.0,
            )

        # log upload_status and trigger exception for bad responses
        # this causes the task to be re-tried
        self.task.stats_client.incr(
            'data.export.upload',
            tags=self.stats_tags + ['status:%s' % response.status_code])
        response.raise_for_status()


class S3Uploader(BaseReportUploader):

    _retriable = (
        IOError,
        boto.exception.BotoClientError,
        boto.exception.BotoServerError,
    )

    def send(self, url, data):
        _, self.bucket, path = urlparse(url)[:3]
        # s3 key names start without a leading slash
        path = path.lstrip('/')
        if not path.endswith('/'):
            path += '/'

        year, month, day = util.utcnow().timetuple()[:3]
        # strip away queue prefix again
        api_key = self.queue_key.split(':')[-1]

        key_name = path.format(
            api_key=api_key, year=year, month=month, day=day)
        key_name += uuid.uuid1().hex + '.json.gz'

        try:
            with self.task.stats_client.timed('data.export.upload',
                                              tags=self.stats_tags):
                conn = boto.connect_s3()
                bucket = conn.get_bucket(self.bucket)
                with closing(boto.s3.key.Key(bucket)) as key:
                    key.key = key_name
                    key.content_encoding = 'gzip'
                    key.content_type = 'application/json'
                    key.set_contents_from_string(
                        util.encode_gzip(data, compresslevel=7))

            self.task.stats_client.incr(
                'data.export.upload',
                tags=self.stats_tags + ['status:success'])
        except Exception:  # pragma: no cover
            self.task.stats_client.incr(
                'data.export.upload',
                tags=self.stats_tags + ['status:failure'])
            raise


class InternalTransform(object):
    """
    This maps the geosubmit v2 schema used in view code and external
    transfers (backup, forward to partners) to the internal submit v1
    schema used in our own database models.
    """

    # *_id maps a source section id to a target section id
    # *_map maps fields inside the section from source to target id
    # if the names are equal, a simple string can be specified instead
    # of a two-tuple

    position_id = ('position', None)
    position_map = [
        ('latitude', 'lat'),
        ('longitude', 'lon'),
        'accuracy',
        'altitude',
        ('altitudeAccuracy', 'altitude_accuracy'),
        'age',
        'heading',
        'pressure',
        'speed',
        'source',
    ]

    blue_id = ('bluetoothBeacons', 'blue')
    blue_map = [
        ('macAddress', 'mac'),
        'age',
        ('signalStrength', 'signal'),
    ]

    cell_id = ('cellTowers', 'cell')
    cell_map = [
        ('radioType', 'radio'),
        ('mobileCountryCode', 'mcc'),
        ('mobileNetworkCode', 'mnc'),
        ('locationAreaCode', 'lac'),
        ('cellId', 'cid'),
        'age',
        'asu',
        ('primaryScramblingCode', 'psc'),
        'serving',
        ('signalStrength', 'signal'),
        ('timingAdvance', 'ta'),
    ]

    wifi_id = ('wifiAccessPoints', 'wifi')
    wifi_map = [
        ('macAddress', 'mac'),
        ('radioType', 'radio'),
        'age',
        'channel',
        'frequency',
        'signalToNoiseRatio',
        ('signalStrength', 'signal'),
    ]

    def _map_dict(self, item_source, field_map):
        value = {}
        for spec in field_map:
            if isinstance(spec, tuple):
                source, target = spec
            else:
                source = spec
                target = spec
            source_value = item_source.get(source)
            if source_value is not None:
                value[target] = source_value
        return value

    def _parse_dict(self, item, report, key_map, field_map):
        value = {}
        item_source = item.get(key_map[0])
        if item_source:
            value = self._map_dict(item_source, field_map)
        if value:
            if key_map[1] is None:
                report.update(value)
            else:  # pragma: no cover
                report[key_map[1]] = value
        return value

    def _parse_list(self, item, report, key_map, field_map):
        values = []
        for value_item in item.get(key_map[0], ()):
            value = self._map_dict(value_item, field_map)
            if value:
                values.append(value)
        if values:
            report[key_map[1]] = values
        return values

    def __call__(self, item):
        report = {}
        self._parse_dict(item, report, self.position_id, self.position_map)

        timestamp = item.get('timestamp')
        if timestamp:
            report['timestamp'] = timestamp

        blues = self._parse_list(item, report, self.blue_id, self.blue_map)
        cells = self._parse_list(item, report, self.cell_id, self.cell_map)
        wifis = self._parse_list(item, report, self.wifi_id, self.wifi_map)

        if blues or cells or wifis:
            return report
        return {}


class InternalUploader(BaseReportUploader):

    _retriable = (
        IOError,
        redis.exceptions.RedisError,
        sqlalchemy.exc.InternalError,
    )
    transform = InternalTransform()

    def _format_report(self, item):
        report = self.transform(item)

        timestamp = report.pop('timestamp', None)
        if timestamp:
            dt = datetime.utcfromtimestamp(timestamp / 1000.0)
            report['time'] = dt.replace(microsecond=0, tzinfo=pytz.UTC)

        return report

    def send(self, url, data):
        with self.task.db_session() as session:
            self._send(session, url, data)

    def _send(self, session, url, data):
        groups = defaultdict(list)
        api_keys = set()
        nicknames = set()

        for item in simplejson.loads(data):
            report = self._format_report(item['report'])
            if report:
                groups[(item['api_key'], item['nickname'])].append(report)
                api_keys.add(item['api_key'])
                nicknames.add(item['nickname'])

        scores = {}
        users = {}
        for nickname in nicknames:
            userid = self.process_user(session, nickname)
            users[nickname] = userid
            scores[userid] = 0

        metrics = {}
        for api_key in api_keys:
            metrics[api_key] = {
                'reports': 0,
                'malformed_reports': 0,
                'obs_count': {
                    'blue': {'upload': 0, 'drop': 0},
                    'cell': {'upload': 0, 'drop': 0},
                    'wifi': {'upload': 0, 'drop': 0},
                }
            }

        all_positions = []
        all_queued_obs = {
            'blue': defaultdict(list),
            'cell': defaultdict(list),
            'wifi': defaultdict(list),
        }

        for (api_key, nickname), reports in groups.items():
            userid = users.get(nickname)

            obs_queue, malformed_reports, obs_count, positions = \
                self.process_reports(reports, userid)

            all_positions.extend(positions)
            for datatype, queued_obs in obs_queue.items():
                for queue_id, values in queued_obs.items():
                    all_queued_obs[datatype][queue_id].extend(values)

            metrics[api_key]['reports'] += len(reports)
            metrics[api_key]['malformed_reports'] += malformed_reports
            for datatype, type_stats in obs_count.items():
                for reason, value in type_stats.items():
                    metrics[api_key]['obs_count'][datatype][reason] += value

            if userid is not None:
                scores[userid] += len(positions)

        for userid, score_value in scores.items():
            self.process_score(userid, score_value)

        with self.task.redis_pipeline() as pipe:
            for datatype, queued_obs in all_queued_obs.items():
                for queue_id, values in queued_obs.items():
                    queue = self.task.app.data_queues[queue_id]
                    queue.enqueue(values, pipe=pipe)

            if all_positions:
                self.process_datamap(pipe, all_positions)

        for api_key, values in metrics.items():
            self.emit_stats(
                session,
                values['reports'],
                values['malformed_reports'],
                values['obs_count'],
                api_key=api_key,
            )

    def emit_stats(self, session, reports, malformed_reports, obs_count,
                   api_key=None):
        api_tag = []
        if api_key is not None:
            api_key = ApiKey.get(session, api_key)

        if api_key and api_key.should_log('submit'):
            api_tag = ['key:%s' % api_key.valid_key]

        if reports > 0:
            self.task.stats_client.incr(
                'data.report.upload', reports, tags=api_tag)

        if malformed_reports > 0:
            self.task.stats_client.incr(
                'data.report.drop', malformed_reports,
                tags=['reason:malformed'] + api_tag)

        for name, stats in obs_count.items():
            for action, count in stats.items():
                if count > 0:
                    tags = ['type:%s' % name]
                    if action == 'drop':
                        tags.append('reason:malformed')
                    self.task.stats_client.incr(
                        'data.observation.%s' % action,
                        count,
                        tags=tags + api_tag)

    def process_reports(self, reports, userid):
        malformed_reports = 0
        positions = set()
        observations = {}
        obs_count = {}
        obs_queue = {}

        for name in ('blue', 'cell', 'wifi'):
            observations[name] = []
            obs_count[name] = {'upload': 0, 'drop': 0}
            obs_queue[name] = defaultdict(list)

        for report in reports:
            obs, malformed_obs = self.process_report(report)

            any_data = False
            for name in ('blue', 'cell', 'wifi'):
                if obs.get(name):
                    observations[name].extend(obs[name])
                    obs_count[name]['upload'] += len(obs[name])
                    any_data = True
                obs_count[name]['drop'] += malformed_obs.get(name, 0)

            if any_data:
                positions.add((report['lat'], report['lon']))
            else:
                malformed_reports += 1

        for name, shard_model, shard_key, queue_prefix in (
                ('blue', BlueShard, 'mac', 'update_blue_'),
                ('cell', CellShard, 'cellid', 'update_cell_'),
                ('wifi', WifiShard, 'mac', 'update_wifi_')):

            if observations[name]:
                sharded_obs = defaultdict(list)
                for ob in observations[name]:
                    shard_id = shard_model.shard_id(getattr(ob, shard_key))
                    sharded_obs[shard_id].append(ob)

                for shard_id, values in sharded_obs.items():
                    obs_queue[name][queue_prefix + shard_id].extend(
                        [value.to_json() for value in values])

        return (obs_queue, malformed_reports, obs_count, positions)

    def process_report(self, data):
        report = Report.create(**data)
        if report is None:
            return ({}, {})

        malformed = {}
        observations = {}
        for name, report_cls, obs_cls in (
                ('blue', BlueReport, BlueObservation),
                ('cell', CellReport, CellObservation),
                ('wifi', WifiReport, WifiObservation)):

            malformed[name] = 0
            observations[name] = {}

            if data.get(name):
                for item in data[name]:
                    # validate the blue/cell/wifi specific fields
                    item_report = report_cls.create(**item)
                    if item_report is None:
                        malformed[name] += 1
                        continue

                    # combine general and specific report data into one
                    item_obs = obs_cls.combine(report, item_report)
                    item_key = item_obs.unique_key

                    # if we have better data for the same key, ignore
                    existing = observations[name].get(item_key)
                    if existing is not None and existing.better(item_obs):
                        continue

                    observations[name][item_key] = item_obs

        obs = {
            'blue': observations['blue'].values(),
            'cell': observations['cell'].values(),
            'wifi': observations['wifi'].values(),
        }
        return (obs, malformed)

    def process_datamap(self, pipe, positions):
        grids = set()
        for lat, lon in positions:
            if lat is not None and lon is not None:
                grids.add(DataMap.scale(lat, lon))

        shards = defaultdict(set)
        for lat, lon in grids:
            shards[DataMap.shard_id(lat, lon)].add(
                encode_datamap_grid(lat, lon))

        for shard_id, values in shards.items():
            queue = self.task.app.data_queues['update_datamap_' + shard_id]
            queue.enqueue(list(values), pipe=pipe)

    def process_score(self, userid, pos_count):
        if userid is None or pos_count <= 0:
            return

        scores = [{
            'key': int(ScoreKey.location),
            'userid': userid,
            'value': pos_count,
        }]

        queue = self.task.app.data_queues['update_score']
        queue.enqueue(scores)

    def process_user(self, session, nickname):
        userid = None
        if nickname and (2 <= len(nickname) <= 128):
            # automatically create user objects and update nickname
            rows = session.query(User).filter(User.nickname == nickname)
            old = rows.first()
            if not old:
                user = User(
                    nickname=nickname,
                )
                session.add(user)
                session.flush()
                userid = user.id
            else:  # pragma: no cover
                userid = old.id

        return userid

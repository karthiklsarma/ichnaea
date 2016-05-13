from contextlib import contextmanager
import os
import os.path

from alembic.config import Config
from alembic import command
import pytest
from sqlalchemy import (
    event,
    inspect,
    text,
)

from ichnaea.config import read_config
from ichnaea.db import configure_db
from ichnaea.geocode import GEOCODER
from ichnaea.geoip import CITY_RADII
from ichnaea.models import _Model, ApiKey

TEST_DIRECTORY = os.path.dirname(__file__)
DATA_DIRECTORY = os.path.join(TEST_DIRECTORY, 'data')
GEOIP_TEST_FILE = os.path.join(DATA_DIRECTORY, 'GeoIP2-City-Test.mmdb')
GEOIP_BAD_FILE = os.path.join(
    DATA_DIRECTORY, 'GeoIP2-Connection-Type-Test.mmdb')

SQLURI = os.environ.get('SQLURI')
REDIS_URI = os.environ.get('REDIS_URI')

SESSION = {}

# Some test-data constants

TEST_CONFIG = read_config(filename=os.path.join(DATA_DIRECTORY, 'test.ini'))

GEOIP_DATA = {
    'London': {
        'city': True,
        'region_code': 'GB',
        'region_name': 'United Kingdom',
        'ip': '81.2.69.192',
        'latitude': 51.5142,
        'longitude': -0.0931,
        'radius': CITY_RADII[2643743],
        'region_radius': GEOCODER.region_max_radius('GB'),
        'score': 0.8,
    },
    'Bhutan': {
        'city': False,
        'region_code': 'BT',
        'region_name': 'Bhutan',
        'ip': '67.43.156.1',
        'latitude': 27.5,
        'longitude': 90.5,
        'radius': GEOCODER.region_max_radius('BT'),
        'region_radius': GEOCODER.region_max_radius('BT'),
        'score': 0.9,
    },
}

GB_LAT = 51.5
GB_LON = -0.1
GB_MCC = 234
GB_MNC = 30


def _make_db(uri=SQLURI):
    return configure_db(uri)


@pytest.mark.usefixtures('raven', 'stats')
class LogTestCase(object):

    def find_stats_messages(self, msg_type, msg_name,
                            msg_value=None, msg_tags=(), _client=None):
        data = {
            'counter': [],
            'timer': [],
            'gauge': [],
            'histogram': [],
            'meter': [],
            'set': [],
        }
        if _client is None:
            client = self.stats_client
        else:
            client = _client

        for msg in client.msgs:
            tags = ()
            if '|#' in msg:
                parts = msg.split('|#')
                tags = parts[-1].split(',')
                msg = parts[0]
            suffix = msg.split('|')[-1]
            name, value = msg.split('|')[0].split(':')
            value = int(value)
            if suffix == 'g':
                data['gauge'].append((name, value, tags))
            elif suffix == 'ms':
                data['timer'].append((name, value, tags))
            elif suffix.startswith('c'):
                data['counter'].append((name, value, tags))
            elif suffix == 'h':
                data['histogram'].append((name, value, tags))
            elif suffix == 'm':
                data['meter'].append((name, value, tags))
            elif suffix == 's':
                data['set'].append((name, value, tags))

        result = []
        for msg in data.get(msg_type):
            if msg[0] == msg_name:
                if msg_value is None or msg[1] == msg_value:
                    if not msg_tags or msg[2] == msg_tags:
                        result.append((msg[0], msg[1], msg[2]))
        return result

    def check_raven(self, expected=None):
        self.raven_client.check(expected=expected)

    def check_stats(self, _client=None, total=None, **kw):
        """Checks a partial specification of messages to be found in
        the stats message stream.
        """
        if _client is None:
            client = self.stats_client
        else:
            client = _client
        if total is not None:
            assert total == len(client.msgs)

        for (msg_type, preds) in kw.items():
            for pred in preds:
                match = 1
                value = None
                tags = ()
                if isinstance(pred, str):
                    name = pred
                elif isinstance(pred, tuple):
                    if len(pred) == 2:
                        (name, match) = pred
                        if isinstance(match, list):
                            tags = match
                            match = 1
                    elif len(pred) == 3:
                        (name, match, value) = pred
                        if isinstance(value, list):
                            tags = value
                            value = None
                    elif len(pred) == 4:
                        (name, match, value, tags) = pred
                    else:
                        raise TypeError('wanted 2, 3 or 4 tuple, got %s'
                                        % type(pred))
                else:
                    raise TypeError('wanted str or tuple, got %s'
                                    % type(pred))
                msgs = self.find_stats_messages(
                    msg_type, name, value, tags, _client=client)
                if isinstance(match, int):
                    assert match == len(msgs)


class DBTestCase(LogTestCase):
    # Inspired by a blog post:
    # http://sontek.net/blog/detail/writing-tests-for-pyramid-and-sqlalchemy

    default_session = 'rw_session'
    track_connection_events = False

    @classmethod
    def setup_database(cls):
        db = _make_db()
        engine = db.engine
        cls.cleanup_tables(engine)
        cls.setup_tables(engine)
        db.close()

    @contextmanager
    def db_call_checker(self):
        rw_conn = self.db_rw_session.bind
        ro_conn = self.db_ro_session.bind
        try:
            self.setup_db_event_tracking(rw_conn, ro_conn)
            yield self.check_db_calls
        finally:
            self.teardown_db_event_tracking(rw_conn, ro_conn)

    def check_db_calls(self, rw=None, ro=None):
        if rw is not None:
            events = self.db_events['rw']['calls']
            assert len(events) == rw
        if ro is not None:
            events = self.db_events['ro']['calls']
            assert len(events) == ro

    def reset_db_event_tracking(self):
        self.db_events = {
            'rw': {'calls': [], 'handler': None},
            'ro': {'calls': [], 'handler': None},
        }

    def setup_db_event_tracking(self, rw_conn, ro_conn):
        self.reset_db_event_tracking()

        def scoped_conn_event_handler(calls):
            def conn_event_handler(**kw):
                calls.append((kw['statement'], kw['parameters']))
            return conn_event_handler

        rw_handler = scoped_conn_event_handler(self.db_events['rw']['calls'])
        self.db_events['rw']['handler'] = rw_handler
        event.listen(rw_conn, 'before_cursor_execute',
                     rw_handler, named=True)

        ro_handler = scoped_conn_event_handler(self.db_events['ro']['calls'])
        self.db_events['ro']['handler'] = ro_handler
        event.listen(ro_conn, 'before_cursor_execute',
                     ro_handler, named=True)

    def teardown_db_event_tracking(self, rw_conn, ro_conn):
        event.remove(ro_conn, 'before_cursor_execute',
                     self.db_events['ro']['handler'])
        event.remove(rw_conn, 'before_cursor_execute',
                     self.db_events['rw']['handler'])
        self.reset_db_event_tracking()

    @pytest.yield_fixture(scope='function', autouse=True)
    def session(self, request, db_rw, db_ro):
        rw_conn = db_rw.engine.connect()
        rw_trans = rw_conn.begin()
        db_rw.session_factory.configure(bind=rw_conn)
        request.cls.db_rw_session = rw_session = db_rw.session()
        ro_conn = db_ro.engine.connect()
        ro_trans = ro_conn.begin()
        db_ro.session_factory.configure(bind=ro_conn)
        request.cls.db_ro_session = ro_session = db_ro.session()

        # set up a default session
        default_session = getattr(request.cls, 'default_session', 'rw_session')
        if default_session == 'ro_session':
            session = ro_session
        else:
            session = rw_session
        setattr(request.cls, 'session', session)
        SESSION['default'] = session

        if request.cls.track_connection_events:
            self.setup_db_event_tracking(rw_conn, ro_conn)

        yield session

        if request.cls.track_connection_events:
            self.teardown_db_event_tracking(rw_conn, ro_conn)

        del SESSION['default']
        del request.cls.session

        ro_trans.rollback()
        ro_session.close()
        del request.cls.db_ro_session
        db_ro.session_factory.configure(bind=None)
        ro_trans.close()
        ro_conn.close()

        rw_trans.rollback()
        rw_session.close()
        del request.cls.db_rw_session
        db_rw.session_factory.configure(bind=None)
        rw_trans.close()
        rw_conn.close()

    @classmethod
    def setup_tables(cls, engine):
        with engine.connect() as conn:
            trans = conn.begin()
            _Model.metadata.create_all(engine)
            # Now stamp the latest alembic version
            alembic_cfg = Config()
            alembic_cfg.set_section_option(
                'alembic', 'script_location', 'alembic')
            alembic_cfg.set_section_option(
                'alembic', 'sqlalchemy.url', str(engine.url))
            alembic_cfg.set_section_option(
                'alembic', 'sourceless', 'true')

            command.stamp(alembic_cfg, 'head')

            # always add a test API key
            conn.execute(ApiKey.__table__.delete())

            key1 = ApiKey.__table__.insert().values(
                valid_key='test', allow_fallback=False, allow_locate=True,
                fallback_name='fall',
                fallback_url='http://127.0.0.1:9/?api',
                fallback_ratelimit=10,
                fallback_ratelimit_interval=60,
                fallback_cache_expire=60,
            )
            conn.execute(key1)
            key2 = ApiKey.__table__.insert().values(
                valid_key='export', allow_fallback=False, allow_locate=False)
            conn.execute(key2)

            trans.commit()

    @classmethod
    def cleanup_tables(cls, engine):
        # reflect and delete all tables, not just those known to
        # our current code version / models
        inspector = inspect(engine)
        with engine.connect() as conn:
            trans = conn.begin()
            names = inspector.get_table_names()
            if names:
                tables = '`' + '`, `'.join(names) + '`'
                conn.execute(text('DROP TABLE %s' % tables))
            trans.commit()


@pytest.mark.usefixtures('data_queues', 'geoip_db', 'http_session', 'redis')
class ConnectionTestCase(DBTestCase):
    pass


@pytest.mark.usefixtures('app', 'redis')
class AppTestCase(DBTestCase):
    default_session = 'ro_session'


@pytest.mark.usefixtures('celery', 'data_queues', 'http_session', 'redis')
class CeleryTestCase(DBTestCase):
    pass


@pytest.mark.usefixtures('app', 'celery', 'redis')
class CeleryAppTestCase(DBTestCase):
    default_session = 'rw_session'

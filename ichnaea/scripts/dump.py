"""
Dump/export our own data to a local file.

Script is installed as `location_dump`.
"""

import argparse
import os
import os.path
import sys

from sqlalchemy import text

from ichnaea.config import (
    DB_RW_URI,
    read_config,
)
from ichnaea.db import (
    configure_rw_db,
    db_worker_session,
)
from ichnaea.geocalc import bbox
from ichnaea.log import (
    configure_logging,
    LOGGER,
)
from ichnaea.models import (
    BlueShard,
    CellShard,
    CellShardOCID,
    WifiShard,
)
from ichnaea import util


def where_area(lat, lon, radius):
    # Construct a where clause based on a bounding box around the given
    # center point.
    if lat is None or lon is None or radius is None:
        return None
    max_lat, min_lat, max_lon, min_lon = bbox(lat, lon, radius)

    return '`lat` <= %s and `lat` >= %s and `lon` <= %s and `lon` >= %s' % (
        round(max_lat, 5), round(min_lat, 5),
        round(max_lon, 5), round(min_lon, 5))


def dump_model(shard_model, session, fd, where=None):
    fd.write(shard_model.export_header() + '\n')
    for model in shard_model.shards().values():
        LOGGER.info('Exporting table: %s', model.__tablename__)
        stmt = model.export_stmt()
        if where:
            stmt = stmt.replace(' ORDER BY ', ' WHERE %s ORDER BY ' % where)
        stmt = text(stmt)
        offset = 0
        limit = 25000
        while True:
            rows = session.execute(
                stmt.bindparams(o=offset, l=limit)).fetchall()
            if rows:
                buf = '\n'.join([row.export_value for row in rows])
                if buf:
                    buf += '\n'
                fd.write(buf)
                offset += limit
            else:
                break


def dump_file(datatype, session, filename,
              lat=None, lon=None, radius=None):
    MODEL = {
        'blue': BlueShard,
        'cell': CellShard,
        'ocid': CellShardOCID,
        'wifi': WifiShard,
    }
    where = where_area(lat, lon, radius)
    with util.gzip_open(filename, 'w') as fd:
        dump_model(MODEL[datatype], session, fd, where=where)
    return 0


def main(argv, _db_rw=None, _dump_file=dump_file):
    parser = argparse.ArgumentParser(
        prog=argv[0], description='Dump/export data.')
    parser.add_argument('--datatype', required=True,
                        help='Type of the data file, blue, cell, ocid or wifi')
    parser.add_argument('--filename', required=True,
                        help='Path to the csv.gz export file.')
    parser.add_argument('--lat', default=None,
                        help='The center latitude of the desired area.')
    parser.add_argument('--lon', default=None,
                        help='The center longitude of the desired area.')
    parser.add_argument('--radius', default=None,
                        help='The radius of the desired area.')

    args = parser.parse_args(argv[1:])
    if not args.filename:  # pragma: no cover
        parser.print_help()
        return 1

    filename = os.path.abspath(os.path.expanduser(args.filename))
    if os.path.isfile(filename):  # pragma: no cover
        print('File already exists.')
        return 1

    datatype = args.datatype
    if datatype not in ('blue', 'cell', 'ocid', 'wifi'):  # pragma: no cover
        print('Unknown data type.')
        return 1

    lat, lon, radius = (None, None, None)
    if (args.lat is not None and
            args.lon is not None and args.radius is not None):
        lat = float(args.lat)
        lon = float(args.lon)
        radius = int(args.radius)

    configure_logging()

    if ('ICHNAEA_CFG' not in os.environ and
            'DB_RW_URI' not in os.environ):  # pragma: no cover
        print('You need to specify either ICHNAEA_CFG or DB_RW_URI.')
        return 1

    app_config = read_config()
    if DB_RW_URI:
        db = configure_rw_db(_db=_db_rw)
    else:  # pragma: no cover
        db = configure_rw_db(app_config.get('database', 'rw_url'), _db=_db_rw)

    with db_worker_session(db, commit=False) as session:
        exit_code = _dump_file(
            datatype, session, filename, lat=lat, lon=lon, radius=radius)
    return exit_code


def console_entry():  # pragma: no cover
    sys.exit(main(sys.argv))

import os.path
import urllib
import itertools
import operator
from datetime import datetime
import time
import threading

from simplejson import loads, dumps
from pysqlite2 import dbapi2 as sqlite


LOCAL = threading.local()
if not hasattr(LOCAL, 'connections'):
    LOCAL.connections = {}


class EntryManager(object):
    def __init__(self, location):
        self.location = location
        if not os.path.exists(location):
            self._create_table()

    @property
    def conn(self):
        if self.location not in LOCAL.connections:
            LOCAL.connections[self.location] = sqlite.connect(self.location, 
                    detect_types=sqlite.PARSE_DECLTYPES|sqlite.PARSE_COLNAMES)
        return LOCAL.connections[self.location]

    def _create_table(self):
        curs = self.conn.cursor()
        curs.executescript("""
            CREATE TABLE store (
                id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                entry TEXT,
                updated timestamp
            );

            CREATE TABLE flat (
                id INTEGER,
                position CHAR(255),
                leaf TEXT
            );

            CREATE INDEX position ON flat (position);
        """)
        self.conn.commit()

    def create_entry(self, entry):
        assert isinstance(entry, dict), "Entry must be instance of ``dict``!"

        # __id__ and __updated__ can be overriden.
        id_ = entry.pop('__id__', None)
        updated = entry.pop('__updated__',
                datetime.utcnow())
        if not isinstance(updated, datetime):
            updated = datetime(
                *(time.strptime(updated, '%Y-%m-%dT%H:%M:%SZ')[0:6]))

        # Store entry.
        curs = self.conn.cursor()
        try:
            curs.execute("""
                INSERT INTO store (id, entry, updated)
                VALUES (?, ?, ?);
            """, (id_, dumps(entry), updated))
        except sqlite.IntegrityError:
            # Avoid database lockup.
            self.conn.rollback()
            raise
        id_ = curs.lastrowid

        # Index entry. We add some metadata (id, updated) and
        # put it on the flat table.
        entry['__id__'] = id_
        entry['__updated__'] = updated.isoformat().split('.', 1)[0] + 'Z'
        indices = [(id_, k, v) for (k, v) in flatten(entry)]
        curs.executemany("""
            INSERT INTO flat (id, position, leaf)
            VALUES (?, ?, ?);
        """, indices)
        self.conn.commit()
        
        return self.get_entry(id_)
        
    def get_entry(self, key):
        curs = self.conn.cursor()
        curs.execute("""
            SELECT id, entry, updated FROM store
            WHERE id=?;
        """, (key,))
        id_, entry, updated = curs.fetchone()
        
        entry = loads(entry)
        entry['__id__'] = id_
        entry['__updated__'] = updated.isoformat().split('.', 1)[0] + 'Z'
        return entry

    def get_entries(self, size=None, offset=0): 
        query = ["SELECT id, entry, updated FROM store"]
        query.append("ORDER BY updated DESC")
        if size is not None:
            query.append("LIMIT %s" % size)
        if offset:
            query.append("OFFSET %s" % offset)
        curs = self.conn.cursor()
        curs.execute(' '.join(query))

        return format(curs.fetchall())

    def delete_entry(self, key):
        curs = self.conn.cursor()
        curs.execute("""
            DELETE FROM store
            WHERE id=?;
        """, (key,))

        curs.execute("""
            DELETE FROM flat
            WHERE id=?;
        """, (key,))
        self.conn.commit()

    def update_entry(self, new_entry): 
        assert isinstance(new_entry, dict), "Entry must be instance of ``dict``!"

        id_ = new_entry['__id__']
        self.delete_entry(id_)
        return self.create_entry(new_entry)

    def search(self, obj, mode=0, size=None, offset=0):
        """
        Search database using a JSON object.
        
	The idea is here is to flatten the JSON object (the "key"),
	and search the index table for each leaf of the key using
	an OR. We then get those ids where the number of results
	is equal to the number of leaves in the key, since these
	objects match the whole key.
        
        """
        # Flatten the JSON key object.
        pairs = list(flatten(obj))
        pairs.sort()
        groups = itertools.groupby(pairs, operator.itemgetter(0))

        query = ["SELECT store.id, store.entry, store.updated FROM store LEFT JOIN flat ON store.id=flat.id"]
        condition = []
        params = []

        # Check groups from groupby, they should be joined within
        # using an OR.
        count = 0
        for (key, group) in groups:
            group = list(group)
            leaves = [params.extend(t) for t in group]

            # Search mode.
            if mode == 0:    # plain match
                subquery = ["(position=? AND leaf=?)" for t in group]
            elif mode == 1:  # LIKE search
                subquery = ["(position=? AND leaf LIKE ?)" for t in group]
            else:
                raise Exception("Search mode %d not supported!" % mode)

            condition.append(' OR '.join(subquery))
            count += len(leaves)
        # Join all conditions with an OR.
        if condition:
            query.append("WHERE")
            query.append(" OR ".join(condition))
        if count:
            query.append("GROUP BY store.id HAVING count(*)=%d" % count)
        query.append("ORDER BY store.updated DESC")
        if size is not None:
            query.append("LIMIT %s" % size)
        if offset:
            query.append("OFFSET %s" % offset)

        curs = self.conn.cursor()
        curs.execute(' '.join(query), tuple(params))
        results = curs.fetchall()

        return format(results)

    def close(self):
        self.conn.close()
        del LOCAL.connections[self.location]


def format(results):
    entries = []
    for id_, entry, updated in results:
        entry = loads(entry)
        entry['__id__'] = id_
        entry['__updated__'] = updated.isoformat().split('.', 1)[0] + 'Z'
        entries.append(entry)

    return entries


def flatten(obj, keys=[]):
    key = '.'.join(keys)
    if isinstance(obj, (int, float, long, basestring)):
        yield key, obj
    else:
        if isinstance(obj, list):
            for item in obj:
                for pair in flatten(item, keys):
                    yield pair
        elif isinstance(obj, dict):
            for k, v in obj.items():
                for pair in flatten(v, keys + [k]):
                    yield pair